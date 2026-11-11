import itertools
import json
import re
import sys
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import config

N_FFT = 400
HOP = 160
WINDOW_SAMPLES = 30 * config.SAMPLE_RATE_HZ
MEL_FRAMES = WINDOW_SAMPLES // HOP

# GPT-2's pre-tokenizer, with \p{L} and \p{N} spelled for the stdlib's re.
_PIECES = re.compile(
    r"""'s|'t|'re|'ve|'m|'ll|'d| ?[^\W\d_]+| ?\d+| ?(?:[^\s\w]|_)+|\s+(?!\S)|\s+"""
)


def exported(model_dir: Path) -> bool:
    names = ("encoder.xml", "decoder.xml", "step_export.json", "tokenizer.json")
    return all((model_dir / name).is_file() for name in names)


def log_mel(samples: np.ndarray, filters: np.ndarray) -> np.ndarray:
    audio = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
    clip = samples[:WINDOW_SAMPLES].astype(np.float32, copy=False)
    audio[: clip.size] = clip
    padded = np.pad(audio, N_FFT // 2, mode="reflect")
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float64)
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP].astype(np.float64)
    power = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
    mel = filters.astype(np.float64) @ power.T
    spec = np.log10(np.maximum(mel, 1e-10))[:, :-1]
    spec = np.maximum(spec, spec.max() - 8.0)
    return ((spec + 4.0) / 4.0).astype(np.float32)


@lru_cache(maxsize=1)
def _bytes_to_unicode() -> dict[int, str]:
    keep = [*range(ord("!"), ord("~") + 1), *range(ord("¡"), ord("¬") + 1)]
    keep += range(ord("®"), ord("ÿ") + 1)
    table = {b: chr(b) for b in keep}
    extra = 0
    for b in range(256):
        if b not in table:
            table[b] = chr(256 + extra)
            extra += 1
    return table


class Bpe:
    def __init__(self, tokenizer_json: Path) -> None:
        raw = json.loads(tokenizer_json.read_text(encoding="utf-8"))
        model = raw["model"]
        self.vocab: dict[str, int] = dict(model["vocab"])
        for added in raw.get("added_tokens", []):
            self.vocab.setdefault(added["content"], int(added["id"]))
        self.pieces = {index: piece for piece, index in self.vocab.items()}
        merges = [
            tuple(m.split(" ", 1)) if isinstance(m, str) else tuple(m) for m in model["merges"]
        ]
        self._ranks = {pair: rank for rank, pair in enumerate(merges)}
        self._byte = _bytes_to_unicode()
        self._unbyte = {char: b for b, char in self._byte.items()}
        self._cache: dict[str, list[int]] = {}

    def id(self, piece: str) -> int:
        return self.vocab[piece]

    def encode(self, text: str) -> list[int]:
        ids: list[int] = []
        for match in _PIECES.findall(text):
            word = "".join(self._byte[b] for b in match.encode("utf-8"))
            if word not in self._cache:
                self._cache[word] = [self.vocab[part] for part in self._merge(word)]
            ids += self._cache[word]
        return ids

    def _merge(self, word: str) -> list[str]:
        parts = list(word)
        while len(parts) > 1:
            pairs = [
                (self._ranks.get(pair, 1 << 30), i)
                for i, pair in enumerate(itertools.pairwise(parts))
            ]
            rank, i = min(pairs)
            if rank == 1 << 30:
                break
            parts[i : i + 2] = [parts[i] + parts[i + 1]]
        return parts

    def decode(self, ids: Iterable[int]) -> str:
        text = "".join(self.pieces[i] for i in ids if not self.pieces[i].startswith("<|"))
        return bytes(self._unbyte[c] for c in text).decode("utf-8", errors="replace")


class Generation:
    def __init__(self, model_dir: Path) -> None:
        raw = json.loads((model_dir / "generation_config.json").read_text(encoding="utf-8"))
        self.eos = int(raw["eos_token_id"])
        self.sot = int(raw["decoder_start_token_id"])
        self.prev = int(raw["prev_sot_token_id"])
        self.no_timestamps = int(raw["no_timestamps_token_id"])
        self.suppress = [int(t) for t in raw.get("suppress_tokens") or []]
        self.begin_suppress = [int(t) for t in raw.get("begin_suppress_tokens") or []]
        self.multilingual = bool(raw.get("is_multilingual"))
        self.lang_to_id = {k: int(v) for k, v in (raw.get("lang_to_id") or {}).items()}
        self.task_to_id = {k: int(v) for k, v in (raw.get("task_to_id") or {}).items()}

    # What WhisperPipeline builds (context_tokens.cpp): the hotwords as previous text.
    def prompt(
        self, hotwords: list[int], language: str | None = None, task: str = "transcribe"
    ) -> list[int]:
        ids = [self.prev, *hotwords] if hotwords else []
        ids.append(self.sot)
        if self.multilingual:
            if language is None:
                raise ValueError("the multilingual model needs a language for the prompt")
            ids += [self.lang_to_id[f"<|{language}|>"], self.task_to_id[task]]
        ids.append(self.no_timestamps)
        return ids


def hotword_ids(bpe: Bpe, generation: Generation, text: str) -> list[int]:
    special = {generation.sot, generation.eos, generation.no_timestamps}
    return [i for i in bpe.encode(" " + text) if i not in special]


def greedy(
    step,
    prompt: list[int],
    generation: Generation,
    max_new_tokens: int,
    prefilled: np.ndarray | None = None,
) -> list[int]:
    logits = prefilled
    if logits is None:
        for position, token in enumerate(prompt):
            logits = step(token, position)
    answer: list[int] = []
    position = len(prompt)
    while logits is not None and len(answer) < max_new_tokens:
        row = np.array(logits, dtype=np.float32, copy=True)
        if not answer:
            row[generation.begin_suppress] = -np.inf
        row[generation.suppress] = -np.inf
        # Never asked for, and the cache has no room for the segments they would start.
        row[generation.no_timestamps :] = -np.inf
        token = int(row.argmax())
        if token == generation.eos:
            break
        answer.append(token)
        logits = step(token, position)
        position += 1
    return answer


# Below this a prompt is the start tokens alone, and two steps cost less than one prefill.
PREFILL_MIN_TOKENS = 8


class StepWhisper:
    def __init__(self, model_dir: Path, device: str) -> None:
        import openvino as ov

        shape = json.loads((model_dir / "step_export.json").read_text(encoding="utf-8"))
        self.layers = int(shape["layers"])
        self.slots = int(shape["cache_slots"])
        self.generation = Generation(model_dir)
        self.bpe = Bpe(model_dir / "tokenizer.json")
        filters = json.loads((model_dir / "preprocessor_config.json").read_text())["mel_filters"]
        self.filters = np.asarray(filters, dtype=np.float32)
        core = ov.Core()
        self._encoder = core.compile_model(
            str(model_dir / "encoder.xml"), device
        ).create_infer_request()
        self._decoder = core.compile_model(
            str(model_dir / "decoder.xml"), device
        ).create_infer_request()
        kv = [f"{'kv'[j % 2]}{j // 2}" for j in range(2 * self.layers)]
        # The request's own tensors, written in place: passing arrays to infer() copies all
        # 24 cross tensors into the NPU every token (28.6 ms a step against 11.7, 2026-11-11).
        self._past = [self._decoder.get_tensor(f"past_{n}").data for n in kv]
        self._new = [f"new_{n}" for n in kv]
        self._cross = [(f"cross_{n}", self._decoder.get_tensor(f"cross_{n}").data) for n in kv]
        self._mask = self._decoder.get_tensor("mask").data
        self._ids = self._decoder.get_tensor("ids").data
        self._pos = self._decoder.get_tensor("pos").data
        self.prefill_tokens = int(shape.get("prefill_tokens") or 0)
        self._prefill = None
        if self.prefill_tokens and (model_dir / "prefill.xml").is_file():
            try:
                self._prefill = core.compile_model(
                    str(model_dir / "prefill.xml"), device
                ).create_infer_request()
            except RuntimeError as exc:
                # Token by token still works, only slower.
                print(
                    f"[vinowhisper-server] prefill.xml did not compile on {device}, "
                    f"feeding the prompt token by token: {str(exc).strip().splitlines()[-1][:160]}",
                    file=sys.stderr,
                    flush=True,
                )
        if self._prefill is not None:
            self._pre_cross = [self._prefill.get_tensor(name).data for name, _ in self._cross]
            self._pre_ids = self._prefill.get_tensor("ids").data
            self._pre_pick = self._prefill.get_tensor("pick").data

    def decode(self, samples: np.ndarray, prompt: list[int], max_new_tokens: int) -> list[int]:
        room = self.slots - len(prompt)
        if room <= 0:
            raise ValueError(f"a {len(prompt)}-token prompt leaves no room in {self.slots} slots")
        mel = log_mel(samples, self.filters)[None]
        out = self._encoder.infer({"mel": mel})
        prefill = self._prefill is not None and (
            PREFILL_MIN_TOKENS <= len(prompt) <= self.prefill_tokens
        )
        for index, (name, target) in enumerate(self._cross):
            target[...] = out[name]
            if prefill:
                self._pre_cross[index][...] = out[name]
        for past in self._past:
            past[...] = 0
        self._mask[...] = -1e4
        self._mask[..., self.slots] = 0

        def step(token: int, position: int) -> np.ndarray:
            self._ids[0, 0] = token
            self._pos[0] = position
            self._decoder.infer()
            for past, name in zip(self._past, self._new, strict=True):
                past[:, :, position] = self._decoder.get_tensor(name).data[:, :, 0]
            self._mask[..., position] = 0
            return self._decoder.get_tensor("logits").data[0]

        first = self._prefill_prompt(prompt) if prefill else None
        return greedy(step, prompt, self.generation, min(max_new_tokens, room - 1), first)

    # The whole prompt in one call, its K/V written into the first len(prompt) slots.
    def _prefill_prompt(self, prompt: list[int]) -> np.ndarray:
        request = self._prefill
        assert request is not None
        count = len(prompt)
        self._pre_ids[...] = 0
        self._pre_ids[0, :count] = prompt
        self._pre_pick[...] = 0
        self._pre_pick[0, count - 1] = 1.0
        request.infer()
        for past, name in zip(self._past, self._new, strict=True):
            past[:, :, :count] = request.get_tensor(name).data[:, :, :count]
        self._mask[..., :count] = 0
        return np.array(request.get_tensor("logits").data[0], copy=True)

    def transcribe(
        self, samples: np.ndarray, hotwords: str, max_new_tokens: int, **prompt_args
    ) -> str:
        ids = hotword_ids(self.bpe, self.generation, hotwords) if hotwords else []
        prompt = self.generation.prompt(ids, **prompt_args)
        return self.bpe.decode(self.decode(samples, prompt, max_new_tokens))
