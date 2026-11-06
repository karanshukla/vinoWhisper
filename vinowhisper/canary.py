import json
import math
import re
from collections.abc import Callable
from pathlib import Path

import numpy as np

from . import config, devices, ovfetch
from .transcriber import BaseTranscriber

# Fixed by the export (scripts/export_canary.py): the encoder takes exactly this many frames.
SAMPLE_RATE_HZ = 16_000
HOP = 160
N_FFT = 512
WIN = 400
N_MELS = 128
PREEMPHASIS = 0.97
LOG_GUARD = 2.0**-24
NORM_EPSILON = 1e-5
WINDOW_S = 12.0
FRAMES = int(WINDOW_S * SAMPLE_RATE_HZ) // HOP + 1
LAYERS = 8
HEADS = 8
HEAD_DIM = 128
CACHE_SLOTS = 192
EOS = 3
# <|startofcontext|><|startoftranscript|><|emo:undefined|> src tgt <|pnc|><|noitn|><|notimestamp|><|nodiarize|>
PROMPT_HEAD = ("<|startofcontext|>", "<|startoftranscript|>", "<|emo:undefined|>")
PROMPT_TAIL = ("<|pnc|>", "<|noitn|>", "<|notimestamp|>", "<|nodiarize|>")
MASKED = -1e4
# Longer audio (a dictation piece, up to MAX_WINDOW_S) is cut near this into WINDOW_S chunks.
CHUNK_SEARCH_S = 3.0


def _hz_to_mel(hz: np.ndarray) -> np.ndarray:
    # Slaney: linear to 1 kHz, logarithmic above (librosa's default, which Parakeet uses).
    f_sp, min_log_hz = 200.0 / 3, 1000.0
    min_log_mel, logstep = min_log_hz / f_sp, math.log(6.4) / 27.0
    mel = hz / f_sp
    above = hz >= min_log_hz
    mel[above] = min_log_mel + np.log(hz[above] / min_log_hz) / logstep
    return mel


def _mel_to_hz(mel: np.ndarray) -> np.ndarray:
    f_sp, min_log_hz = 200.0 / 3, 1000.0
    min_log_mel, logstep = min_log_hz / f_sp, math.log(6.4) / 27.0
    hz = f_sp * mel
    above = mel >= min_log_mel
    hz[above] = min_log_hz * np.exp(logstep * (mel[above] - min_log_mel))
    return hz


def mel_filters() -> np.ndarray:
    fft_hz = np.linspace(0, SAMPLE_RATE_HZ / 2, N_FFT // 2 + 1)
    mel_hz = _mel_to_hz(
        np.linspace(
            _hz_to_mel(np.array([0.0]))[0],
            _hz_to_mel(np.array([SAMPLE_RATE_HZ / 2]))[0],
            N_MELS + 2,
        )
    )
    fdiff = np.diff(mel_hz)
    ramps = mel_hz[:, None] - fft_hz[None, :]
    lower = -ramps[:-2] / fdiff[:-1, None]
    upper = ramps[2:] / fdiff[1:, None]
    weights = np.maximum(0, np.minimum(lower, upper))
    weights *= (2.0 / (mel_hz[2:] - mel_hz[:-2]))[:, None]
    return weights.astype(np.float32)


_FILTERS: np.ndarray | None = None


# Parakeet's extractor without torch: log-mel normalised per bin over the real frames,
# zero-padded to FRAMES, with the mask the encoder needs (an unmasked pad moves its output).
def features(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    global _FILTERS
    if _FILTERS is None:
        _FILTERS = mel_filters()
    x = np.asarray(samples, dtype=np.float32)[: FRAMES * HOP]
    n = x.size
    x = np.concatenate([x[:1], x[1:] - PREEMPHASIS * x[:-1]])
    x = np.pad(x, (N_FFT // 2, N_FFT // 2))
    frames = 1 + n // HOP
    window = np.zeros(N_FFT, dtype=np.float32)
    left = (N_FFT - WIN) // 2
    window[left : left + WIN] = np.hanning(WIN)
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(frames)[:, None]
    spec = np.fft.rfft(x[idx] * window, axis=-1)
    power = (spec.real**2 + spec.imag**2).astype(np.float32)
    mel = np.log(power @ _FILTERS.T + LOG_GUARD)
    valid = max(1, n // HOP)
    real = mel[:valid]
    mean = real.mean(axis=0)
    std = np.sqrt(((real - mean) ** 2).sum(axis=0) / max(valid - 1, 1))
    mel = (mel - mean) / (std + NORM_EPSILON)
    out = np.zeros((1, FRAMES, N_MELS), dtype=np.float32)
    out[0, :valid] = mel[:valid]
    mask = np.zeros((1, FRAMES), dtype=np.int64)
    mask[0, :valid] = 1
    return out, mask


_BYTE = re.compile(r"^<0x([0-9A-Fa-f]{2})>$")


class Detokenizer:
    def __init__(self, tokenizer_json: Path) -> None:
        raw = json.loads(tokenizer_json.read_text(encoding="utf-8"))
        vocab: dict[str, int] = dict(raw["model"]["vocab"])
        for added in raw.get("added_tokens", []):
            vocab[added["content"]] = added["id"]
        self.ids = vocab
        self.pieces = {i: p for p, i in vocab.items()}
        self.special = {a["id"] for a in raw.get("added_tokens", []) if a.get("special")}
        self.special |= {i for p, i in vocab.items() if p.startswith("<|") and p.endswith("|>")}

    def id(self, piece: str) -> int:
        try:
            return self.ids[piece]
        except KeyError:
            raise ValueError(f"the tokenizer has no {piece}") from None

    def decode(self, ids: list[int]) -> str:
        out = bytearray()
        for i in ids:
            if i in self.special:
                continue
            piece = self.pieces.get(i, "")
            byte = _BYTE.match(piece)
            out += bytes([int(byte.group(1), 16)]) if byte else piece.encode("utf-8")
        return out.decode("utf-8", errors="replace").replace("▁", " ").lstrip(" ")


def chunks(samples: np.ndarray) -> list[np.ndarray]:
    limit = int(WINDOW_S * SAMPLE_RATE_HZ)
    out = []
    rest = samples
    step = SAMPLE_RATE_HZ // 10
    while rest.size > limit:
        lo = limit - int(CHUNK_SEARCH_S * SAMPLE_RATE_HZ)
        energy = [
            float(np.square(rest[s : s + step]).mean()) for s in range(lo, limit - step + 1, step)
        ]
        cut = lo + step * int(np.argmin(energy)) + step // 2
        out.append(rest[:cut])
        rest = rest[cut:]
    out.append(rest)
    return out


class CanaryTranscriber(BaseTranscriber):
    model = config.CANARY_MODEL

    def __init__(
        self,
        model_dir: Path | None = None,
        device: str | None = None,
        language: str = config.DEFAULT_LANGUAGE,
        task: str = config.DEFAULT_TASK,
    ) -> None:
        super().__init__(model_dir, device, language, task)
        self._encoder: object | None = None
        self._decoder: object | None = None
        self._tok: Detokenizer | None = None

    def _default_model_dir(self, kind: str) -> Path:
        return config.model_dir(kind, model=self.model)

    def _check_export(self, kind: str, model_dir: Path) -> None:
        variant = "npu" if kind == "NPU" else "stateful"
        missing = [
            name
            for name in ("encoder.xml", "decoder.xml", "tokenizer.json")
            if not (model_dir / name).is_file()
        ]
        if missing:
            raise FileNotFoundError(
                f"no Canary export at {model_dir} (missing {', '.join(missing)})\n"
                f"  run: {config.export_command(variant, model=self.model)}"
            )

    def _build(self, selection: devices.Selection, model_dir: Path) -> None:
        import openvino as ov

        try:
            ovfetch.preload()
        except ovfetch.OvfetchError:
            if selection.kind == "NPU":
                raise
        core = ov.Core()
        name = selection.device.name
        self._tok = Detokenizer(model_dir / "tokenizer.json")
        self._encoder = core.compile_model(
            str(model_dir / "encoder.xml"), name
        ).create_infer_request()
        self._decoder = core.compile_model(
            str(model_dir / "decoder.xml"), name
        ).create_infer_request()
        self._pipeline = True

    def _unload(self) -> None:
        self._encoder = self._decoder = None
        self._pipeline = None

    def prompt(self) -> list[int]:
        tok = self._tok
        if tok is None:
            raise RuntimeError("call load() before prompt()")
        auto = self.language == config.AUTO_LANGUAGE
        source = config.CANARY_AUTO_SOURCE if auto else self.language
        target = "en" if self.task == "translate" else source
        return [
            *(tok.id(p) for p in PROMPT_HEAD),
            tok.id(f"<|{source}|>"),
            tok.id(f"<|{target}|>"),
            *(tok.id(p) for p in PROMPT_TAIL),
        ]

    def _generate(self, samples: np.ndarray, streamer: Callable[[str], bool]) -> None:
        if self._pipeline is None:
            self.load()
        pieces = chunks(np.asarray(samples, dtype=np.float32))
        for n, piece in enumerate(pieces):
            if n and streamer(" "):
                return
            if self._decode(piece, streamer):
                return

    def _decode(self, samples: np.ndarray, streamer: Callable[[str], bool]) -> bool:
        import openvino as ov

        enc, dec, tok = self._encoder, self._decoder, self._tok
        if enc is None or dec is None or tok is None:
            raise RuntimeError(f"no Canary model on {self.device} after load()")
        feats, fmask = features(samples)
        result = enc.infer({"feats": feats, "fmask": fmask})  # type: ignore[attr-defined]
        outputs = [result[o].copy() for o in enc.model_outputs]  # type: ignore[attr-defined]
        dec.set_tensor("cmask", ov.Tensor(outputs[0]))  # type: ignore[attr-defined]
        for j, cross in enumerate(outputs[1:]):
            dec.set_tensor(f"cross_{'kv'[j % 2]}{j // 2}", ov.Tensor(cross))  # type: ignore[attr-defined]

        past = [np.zeros((1, HEADS, CACHE_SLOTS, HEAD_DIM), np.float32) for _ in range(2 * LAYERS)]
        mask = np.full((1, 1, 1, CACHE_SLOTS + 1), MASKED, np.float32)
        mask[..., CACHE_SLOTS] = 0
        prompt = self.prompt()
        budget = min(
            config.max_new_tokens(samples.size / SAMPLE_RATE_HZ, self.multilingual),
            CACHE_SLOTS - len(prompt),
        )
        tokens, shown, t = list(prompt), "", 0
        while t < CACHE_SLOTS and len(tokens) - len(prompt) < budget:
            dec.set_tensor("ids", ov.Tensor(np.array([[tokens[t]]], np.int64)))  # type: ignore[attr-defined]
            dec.set_tensor("pos", ov.Tensor(np.array([t], np.int64)))  # type: ignore[attr-defined]
            dec.set_tensor("mask", ov.Tensor(mask))  # type: ignore[attr-defined]
            for j in range(2 * LAYERS):
                dec.set_tensor(f"past_{'kv'[j % 2]}{j // 2}", ov.Tensor(past[j]))  # type: ignore[attr-defined]
            dec.infer()  # type: ignore[attr-defined]
            for j in range(2 * LAYERS):
                past[j][:, :, t] = dec.get_output_tensor(1 + j).data[:, :, 0]  # type: ignore[attr-defined]
            mask[..., t] = 0
            t += 1
            if t < len(tokens):
                continue
            nxt = int(np.argmax(dec.get_output_tensor(0).data[0]))  # type: ignore[attr-defined]
            if nxt == EOS:
                break
            tokens.append(nxt)
            text = tok.decode(tokens[len(prompt) :])
            # A byte-fallback character arrives over several tokens; wait until it is whole.
            if text.endswith("�") or not text.startswith(shown):
                continue
            if text != shown:
                stop = streamer(text[len(shown) :])
                shown = text
                if stop:
                    return True
        return False


PINS_PATH = Path(__file__).resolve().parent / "canary_model.json"
NPU_REPO = "arankays/canary-1b-v2-int8-ov"
FP16_REPO = "arankays/canary-1b-v2-fp16-ov"
_EXPORT_FILES = ("encoder.xml", "encoder.bin", "decoder.xml", "decoder.bin", "tokenizer.json")


def _target(kind: str | None = None) -> tuple[str, Path]:
    # The export the server would load here: INT8 on an NPU, fp16 anywhere else.
    if kind is None:
        try:
            kind = devices.select(config.DEFAULT_DEVICE, devices.available()).kind
        except devices.DeviceError:
            kind = "CPU"
    if kind == "NPU":
        return NPU_REPO, config.CANARY_MODEL_DIR
    return FP16_REPO, config.CANARY_FP16_MODEL_DIR


def _exported(directory: Path) -> bool:
    return all((directory / name).is_file() for name in _EXPORT_FILES)


def model_status(kind: str | None = None) -> dict:
    from . import llm

    repo, directory = _target(kind)
    pin = llm.pinned(repo, PINS_PATH)
    if pin is None:
        raise llm.ModelMissing(f"{PINS_PATH} is missing or unreadable; reinstall vinowhisper")
    # A local export (scripts/export_canary.py) counts too; Verify then checks it against the pin.
    present = _exported(directory)
    return {
        "label": "Canary 1B v2",
        "repo": repo,
        "revision": pin.revision,
        "download_bytes": sum(pin.sizes.values()),
        "present": present,
        "verified": llm.is_present(pin, directory),
        "bytes": sum((directory / n).stat().st_size for n in _EXPORT_FILES) if present else 0,
        "cache_bytes": 0,
        "dir": str(directory),
    }


def model_fetch(progress: Callable[[int, int], None], kind: str | None = None) -> Path:
    from . import llm

    repo, directory = _target(kind)
    pin = llm.pinned(repo, PINS_PATH)
    if pin is None:
        raise llm.ModelMissing(f"{PINS_PATH} is missing or unreadable; reinstall vinowhisper")
    return llm.fetch(progress, pin=pin, directory=directory)


def model_delete() -> None:
    import shutil

    for directory in (config.CANARY_MODEL_DIR, config.CANARY_FP16_MODEL_DIR):
        shutil.rmtree(directory, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    import requests

    parser = argparse.ArgumentParser(
        prog="vinowhisper-canary",
        description="The prebuilt Canary 1B v2 export. Each command prints JSON lines.",
    )
    parser.add_argument("what", choices=("model",))
    parser.add_argument("action", choices=("status", "fetch", "delete"))
    action = parser.parse_args(sys.argv[1:] if argv is None else argv).action

    def emit(record: dict) -> None:
        print(json.dumps(record), flush=True)

    if action == "status":
        emit(model_status())
        return 0
    if action == "delete":
        model_delete()
        emit({"event": "deleted"})
        return 0
    last = [0.0]

    def progress(done: int, total: int) -> None:
        now = done / max(total, 1)
        if now - last[0] >= 0.01 or done >= total:
            last[0] = now
            emit({"event": "progress", "done": done, "total": total})

    try:
        directory = model_fetch(progress)
    except (requests.RequestException, OSError, RuntimeError) as exc:
        emit({"event": "error", "message": str(exc).splitlines()[0]})
        return 1
    emit({"event": "done", "bytes": model_status()["bytes"], "dir": str(directory)})
    return 0
