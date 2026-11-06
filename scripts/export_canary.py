# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "transformers==5.22.2",
#     "torch",
#     "librosa",
#     "huggingface_hub",
#     "openvino==2026.4.0",
#     "nncf",
#     "numpy",
# ]
#
# [[tool.uv.index]]
# name = "pytorch-cpu"
# url = "https://download.pytorch.org/whl/cpu"
# explicit = true
#
# [tool.uv.sources]
# torch = { index = "pytorch-cpu" }
# ///
# Exports nvidia/canary-1b-v2 for vinoWhisper: an INT8 encoder + one-token decoder for the
# NPU, and fp16 for CPU/GPU (the CPU plugin cannot compile the INT8 one). Own environment,
# because Canary needs transformers >= 5.15 and the Whisper NPU export needs < 5.4.
# Write-up: docs/models.md.
import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

MODEL_ID = "nvidia/canary-1b-v2"
SOURCES_URL = (
    "https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/vinowhisper/model_sources.json"
)
FRAMES = 1201
CACHE_SLOTS = 192


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def _cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def _capped(argv: list[str]) -> None:
    # The fp32 weights, the trace and the IR are all in memory at once: ~10 GB at peak.
    if os.environ.get("VINOWHISPER_EXPORT_CAPPED") or not shutil.which("systemd-run"):
        return
    try:
        avail_kb = next(
            int(line.split()[1])
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
    except (OSError, StopIteration, ValueError):
        return
    if subprocess.run(
        ["systemd-run", "--user", "--scope", "--quiet", "true"], capture_output=True
    ).returncode:
        return
    cap = avail_kb * 9 // 10
    print(f"==> capped at {cap // 1024} MiB, 90% of available memory", flush=True)
    env = {**os.environ, "VINOWHISPER_EXPORT_CAPPED": "1"}
    os.execvpe(
        "systemd-run",
        [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "-p",
            f"MemoryMax={cap}K",
            "-p",
            "MemorySwapMax=0",
            sys.executable,
            *argv,
        ],
        env,
    )


def _pins(sources: str | None) -> dict:
    local = Path(__file__).resolve().parent.parent / "vinowhisper/model_sources.json"
    if sources:
        raw = Path(sources).read_text()
    elif local.is_file():
        raw = local.read_text()
    else:
        with urllib.request.urlopen(SOURCES_URL, timeout=30) as response:  # noqa: S310
            raw = response.read().decode()
    return json.loads(raw)["sources"][MODEL_ID]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(pin: dict, root: Path) -> Path:
    from huggingface_hub import hf_hub_download

    target = root / f"{MODEL_ID.replace('/', '--')}@{pin['revision']}"
    for name, meta in pin["files"].items():
        path = target / name
        if not path.is_file() or path.stat().st_size != meta["size"]:
            hf_hub_download(MODEL_ID, name, revision=pin["revision"], local_dir=target)
        if _sha256(path) != meta["sha256"]:
            sys.exit(f"{path} does not match its pinned sha256; delete it and run again")
    return target


def export(source: Path, npu_out: Path, cpu_out: Path) -> None:
    import nncf
    import numpy as np
    import openvino as ov
    import torch
    from torch import nn
    from transformers import AutoProcessor, CanaryForConditionalGeneration

    proc = AutoProcessor.from_pretrained(source)
    full = CanaryForConditionalGeneration.from_pretrained(source, dtype=torch.float32)
    full = full.eval().requires_grad_(False)
    enc, dec, head = full.model.encoder, full.model.decoder, full.proj_out
    layers = len(dec.layers)
    heads = dec.layers[0].self_attn.config.num_attention_heads
    dim = dec.layers[0].self_attn.head_dim

    class Encoder(nn.Module):
        def forward(self, feats, fmask):
            o = enc(feats, attention_mask=fmask.bool())
            h = o.last_hidden_state
            out = [torch.where(o.attention_mask.bool(), 0.0, -1e4)[:, None, None, :].float()]
            for layer in dec.layers:
                a = layer.encoder_attn
                out.append(a.k_proj(h).view(1, -1, heads, dim).transpose(1, 2))
                out.append(a.v_proj(h).view(1, -1, heads, dim).transpose(1, 2))
            return tuple(out)

    def attend(q, k, v, mask, scale):
        return torch.matmul(torch.softmax(torch.matmul(q, k.transpose(2, 3)) * scale + mask, -1), v)

    class Step(nn.Module):
        def forward(self, ids, pos, mask, cmask, *kv):
            past, cross = kv[: 2 * layers], kv[2 * layers :]
            pe = dec.pos_emb.positional_embedding[pos] / math.sqrt(dec.pos_emb.channels)
            x = dec.embedding_layernorm(dec.embed_tokens(ids) + pe)
            new = []
            for i, layer in enumerate(dec.layers):
                sa, ca = layer.self_attn, layer.encoder_attn
                h = layer.input_layernorm(x)
                q, k, v = (
                    p(h).view(1, 1, heads, dim).transpose(1, 2)
                    for p in (sa.q_proj, sa.k_proj, sa.v_proj)
                )
                new += [k, v]
                keys = torch.cat([past[2 * i], k], 2)
                values = torch.cat([past[2 * i + 1], v], 2)
                x = x + sa.o_proj(
                    attend(q, keys, values, mask, sa.scaling).transpose(1, 2).reshape(1, 1, -1)
                )
                h = layer.post_attention_layernorm(x)
                q = ca.q_proj(h).view(1, 1, heads, dim).transpose(1, 2)
                x = x + ca.o_proj(
                    attend(q, cross[2 * i], cross[2 * i + 1], cmask, ca.scaling)
                    .transpose(1, 2)
                    .reshape(1, 1, -1)
                )
                x = x + layer.mlp(layer.final_layernorm(x))
            return (head(dec.norm(x))[:, 0], *new)

    # Self-check on 7 s of noise, padded to 12 s: the wrapper must decode what generate() does.
    rng = np.random.default_rng(0)
    audio = (rng.standard_normal(7 * 16000) * 0.05).astype(np.float32)
    inp = proc.apply_transcription_request(audio, source_language="en", target_language="en")
    prompt = inp["decoder_input_ids"][0].tolist()
    f = proc.feature_extractor(audio, sampling_rate=16000, return_tensors="pt")
    feats = torch.zeros(1, FRAMES, 128)
    feats[0, : f["input_features"].shape[1]] = f["input_features"][0]
    fmask = torch.zeros(1, FRAMES, dtype=torch.long)
    fmask[0, : audio.size // 160] = 1
    with torch.inference_mode():
        ref = full.generate(**inp, max_new_tokens=40)[0, len(prompt) :].tolist()
        ref = [t for t in ref if t not in (full.config.eos_token_id, full.config.pad_token_id)]
        eo = Encoder()(feats, fmask)
        cmask, cross = eo[0], list(eo[1:])
        past = [torch.zeros(1, heads, CACHE_SLOTS, dim) for _ in range(2 * layers)]
        mask = torch.full((1, 1, 1, CACHE_SLOTS + 1), -1e4)
        mask[..., CACHE_SLOTS] = 0
        toks, t = list(prompt), 0
        while len(toks) - len(prompt) < 40:
            out = Step()(torch.tensor([[toks[t]]]), torch.tensor([t]), mask, cmask, *past, *cross)
            for j in range(2 * layers):
                past[j][:, :, t] = out[1 + j][:, :, 0]
            mask[..., t] = 0
            t += 1
            if t >= len(toks):
                nxt = int(out[0][0].argmax())
                if nxt == full.config.eos_token_id:
                    break
                toks.append(nxt)
        logits = full(
            input_features=inp["input_features"],
            attention_mask=inp["attention_mask"],
            decoder_input_ids=inp["decoder_input_ids"],
        ).logits[0]
        past = [torch.zeros(1, heads, CACHE_SLOTS, dim) for _ in range(2 * layers)]
        mask = torch.full((1, 1, 1, CACHE_SLOTS + 1), -1e4)
        mask[..., CACHE_SLOTS] = 0
        drift = 0.0
        for t, token in enumerate(prompt):
            out = Step()(torch.tensor([[token]]), torch.tensor([t]), mask, cmask, *past, *cross)
            for j in range(2 * layers):
                past[j][:, :, t] = out[1 + j][:, :, 0]
            mask[..., t] = 0
            drift = max(drift, float((out[0][0] - logits[t]).abs().max()))
    if toks[len(prompt) :] != ref or drift > 1e-3:
        sys.exit(
            f"the export wrapper does not match transformers (logit drift {drift:.2g}); not writing an export"
        )
    print(f"==> wrapper matches transformers (logit drift {drift:.1g})", flush=True)

    enc_ir = ov.convert_model(
        Encoder(),
        example_input=(feats, fmask),
        input=[ov.PartialShape([1, FRAMES, 128]), ov.PartialShape([1, FRAMES])],
    )
    enc_ir.inputs[0].get_tensor().set_names({"feats"})
    enc_ir.inputs[1].get_tensor().set_names({"fmask"})
    names = ["ids", "pos", "mask", "cmask"]
    names += [f"past_{'kv'[j % 2]}{j // 2}" for j in range(2 * layers)]
    names += [f"cross_{'kv'[j % 2]}{j // 2}" for j in range(2 * layers)]
    example = (
        torch.zeros(1, 1, dtype=torch.long),
        torch.zeros(1, dtype=torch.long),
        torch.zeros(1, 1, 1, CACHE_SLOTS + 1),
        cmask,
        *[torch.zeros(1, heads, CACHE_SLOTS, dim) for _ in range(2 * layers)],
        *cross,
    )
    dec_ir = ov.convert_model(
        Step(), example_input=example, input=[ov.PartialShape(list(e.shape)) for e in example]
    )
    for port, name in zip(dec_ir.inputs, names, strict=True):
        port.get_tensor().set_names({name})

    for out in (cpu_out, npu_out):
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy(source / "tokenizer.json", out / "tokenizer.json")
    ov.save_model(enc_ir, str(cpu_out / "encoder.xml"), compress_to_fp16=True)
    ov.save_model(dec_ir, str(cpu_out / "decoder.xml"), compress_to_fp16=True)
    print(f"==> fp16 (CPU/GPU) export in {cpu_out}", flush=True)
    core = ov.Core()
    for name in ("encoder", "decoder"):
        # Per-channel INT8: INT4 was no faster on the NPU and changed tokens (2026-11-06).
        model = nncf.compress_weights(
            core.read_model(str(cpu_out / f"{name}.xml")), mode=nncf.CompressWeightsMode.INT8_SYM
        )
        ov.save_model(model, str(npu_out / f"{name}.xml"))
    print(f"==> INT8 (NPU) export in {npu_out}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export nvidia/canary-1b-v2 for vinoWhisper.")
    models = _data_home() / "vinowhisper/models"
    parser.add_argument("--npu-out", type=Path, default=models / "canary-1b-v2-ov")
    parser.add_argument("--cpu-out", type=Path, default=models / "canary-1b-v2-ov-fp16")
    parser.add_argument("--source-root", type=Path, default=models / "source")
    parser.add_argument("--sources", help="model_sources.json to read the pins from")
    args = parser.parse_args()
    _capped(sys.argv)
    tmp = _cache_home() / "vinowhisper/export-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(tmp)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    source = fetch(_pins(args.sources), args.source_root)
    os.environ["HF_HUB_OFFLINE"] = "1"
    export(source, args.npu_out, args.cpu_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
