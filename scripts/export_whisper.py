# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "transformers==5.22.2",
#     "torch",
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
# Exports Whisper for vinoWhisper's own decode loop (#177): a static 30 s encoder that also
# emits every decoder layer's cross-attention K/V, and a one-token stateless decoder whose
# self-attention cache the host fills. INT8 for the NPU, fp16 for CPU/GPU. Not optimum:
# WhisperPipeline's NPU path refuses a decoder prompt, which is what this is for (#176).
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

MODELS = ("openai/whisper-small.en", "openai/whisper-small")
SOURCES_URL = (
    "https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/vinowhisper/model_sources.json"
)
MEL_FRAMES = 3000
# Whisper's max_target_positions: the prompt and the answer share them.
CACHE_SLOTS = 448
# The whole prompt in one call: <|startofprev|>, at most 120 vocabulary tokens, the start tokens.
PREFILL_TOKENS = 128
COPIED = ("tokenizer.json", "generation_config.json", "config.json", "preprocessor_config.json")


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def _cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def _capped(argv: list[str]) -> None:
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


def _pins(model_id: str, sources: str | None) -> dict:
    local = Path(__file__).resolve().parent.parent / "vinowhisper/model_sources.json"
    if sources:
        raw = Path(sources).read_text()
    elif local.is_file():
        raw = local.read_text()
    else:
        with urllib.request.urlopen(SOURCES_URL, timeout=30) as response:  # noqa: S310
            raw = response.read().decode()
    return json.loads(raw)["sources"][model_id]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(model_id: str, pin: dict, root: Path) -> Path:
    from huggingface_hub import hf_hub_download

    target = root / f"{model_id.replace('/', '--')}@{pin['revision']}"
    for name, meta in pin["files"].items():
        path = target / name
        if not path.is_file() or path.stat().st_size != meta["size"]:
            hf_hub_download(model_id, name, revision=pin["revision"], local_dir=target)
        if _sha256(path) != meta["sha256"]:
            sys.exit(f"{path} does not match its pinned sha256; delete it and run again")
    return target


def export(source: Path, npu_out: Path, cpu_out: Path, slots: int, prefill: int) -> None:
    import nncf
    import numpy as np
    import openvino as ov
    import torch
    from torch import nn
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    proc = WhisperProcessor.from_pretrained(source)
    full = WhisperForConditionalGeneration.from_pretrained(source, dtype=torch.float32)
    full = full.eval().requires_grad_(False)
    enc, dec = full.model.encoder, full.model.decoder
    layers = len(dec.layers)
    heads = dec.layers[0].self_attn.num_heads
    dim = dec.layers[0].self_attn.head_dim
    if slots > dec.embed_positions.weight.shape[0]:
        sys.exit(f"--cache-slots {slots} is past the model's {dec.embed_positions.weight.shape[0]}")

    def split(x):
        return x.view(1, -1, heads, dim).transpose(1, 2)

    class Encoder(nn.Module):
        def forward(self, mel):
            h = enc(mel).last_hidden_state
            out = []
            for layer in dec.layers:
                a = layer.encoder_attn
                out += [split(a.k_proj(h)), split(a.v_proj(h))]
            return tuple(out)

    def attend(q, k, v, mask=None):
        w = torch.matmul(q, k.transpose(2, 3)) * dim**-0.5
        if mask is not None:
            w = w + mask
        return torch.matmul(torch.softmax(w, -1), v).transpose(1, 2).reshape(1, 1, -1)

    class Step(nn.Module):
        def forward(self, ids, pos, mask, *kv):
            past, cross = kv[: 2 * layers], kv[2 * layers :]
            x = dec.embed_tokens(ids) + dec.embed_positions.weight[pos][:, None]
            new = []
            for i, layer in enumerate(dec.layers):
                sa, ca = layer.self_attn, layer.encoder_attn
                h = layer.self_attn_layer_norm(x)
                q, k, v = split(sa.q_proj(h)), split(sa.k_proj(h)), split(sa.v_proj(h))
                new += [k, v]
                keys = torch.cat([past[2 * i], k], 2)
                values = torch.cat([past[2 * i + 1], v], 2)
                x = x + sa.out_proj(attend(q, keys, values, mask))
                h = layer.encoder_attn_layer_norm(x)
                x = x + ca.out_proj(attend(split(ca.q_proj(h)), cross[2 * i], cross[2 * i + 1]))
                x = x + layer.fc2(layer.activation_fn(layer.fc1(layer.final_layer_norm(x))))
            return (full.proj_out(dec.layer_norm(x))[:, 0], *new)

    # Padding sits after the prompt, so the causal mask alone keeps it out of every real row.
    causal = torch.triu(torch.full((prefill, prefill), -1e4), 1)[None, None]

    def spread(x):
        return x.view(1, prefill, heads, dim).transpose(1, 2)

    def attend_all(q, k, v, mask=None):
        w = torch.matmul(q, k.transpose(2, 3)) * dim**-0.5
        if mask is not None:
            w = w + mask
        return torch.matmul(torch.softmax(w, -1), v).transpose(1, 2).reshape(1, prefill, -1)

    class Prefill(nn.Module):
        def forward(self, ids, pick, *cross):
            x = dec.embed_tokens(ids) + dec.embed_positions.weight[:prefill][None]
            new = []
            for i, layer in enumerate(dec.layers):
                sa, ca = layer.self_attn, layer.encoder_attn
                h = layer.self_attn_layer_norm(x)
                q, k, v = spread(sa.q_proj(h)), spread(sa.k_proj(h)), spread(sa.v_proj(h))
                new += [k, v]
                x = x + sa.out_proj(attend_all(q, k, v, causal))
                h = layer.encoder_attn_layer_norm(x)
                q = spread(ca.q_proj(h))
                x = x + ca.out_proj(attend_all(q, cross[2 * i], cross[2 * i + 1]))
                x = x + layer.fc2(layer.activation_fn(layer.fc1(layer.final_layer_norm(x))))
            # A one-hot row, not an index: the NPU compiler could not place an int64 gather.
            h = torch.matmul(pick[:, None, :], dec.layer_norm(x))
            return (full.proj_out(h)[:, 0], *new)

    # A prompt as dictation will send it: <|startofprev|>, the vocabulary, the start tokens,
    # then answer tokens, all teacher-forced so every position's logits can be compared.
    tok = proc.tokenizer
    sot = tok.convert_tokens_to_ids("<|startoftranscript|>")
    prev = tok.convert_tokens_to_ids("<|startofprev|>")
    nots = tok.convert_tokens_to_ids("<|notimestamps|>")
    head = [sot]
    # The multilingual model takes a language and a task; the English-only one refuses them.
    if full.config.vocab_size > 51864:
        head += [tok.convert_tokens_to_ids(t) for t in ("<|en|>", "<|transcribe|>")]
    vocab = tok.encode(" Karan, OpenVINO, Wildcat Lake, Plasma", add_special_tokens=False)
    answer = tok.encode(" Karan ran OpenVINO on Wildcat Lake.", add_special_tokens=False)
    sequence = [prev, *vocab, *head, nots, *answer]

    rng = np.random.default_rng(0)
    audio = (rng.standard_normal(7 * 16000) * 0.05).astype(np.float32)
    mel = proc.feature_extractor(audio, sampling_rate=16000, return_tensors="pt").input_features
    with torch.inference_mode():
        logits = full(input_features=mel, decoder_input_ids=torch.tensor([sequence])).logits[0]
        cross = list(Encoder()(mel))
        past = [torch.zeros(1, heads, slots, dim) for _ in range(2 * layers)]
        mask = torch.full((1, 1, 1, slots + 1), -1e4)
        mask[..., slots] = 0
        mine = []
        for t, token in enumerate(sequence):
            out = Step()(torch.tensor([[token]]), torch.tensor([t]), mask, *past, *cross)
            for j in range(2 * layers):
                past[j][:, :, t] = out[1 + j][:, :, 0]
            mask[..., t] = 0
            mine.append(out[0][0])
        drift = float((torch.stack(mine) - logits).abs().max())
        n = len(sequence) - len(answer)
        padded = torch.zeros(1, prefill, dtype=torch.long)
        padded[0, :n] = torch.tensor(sequence[:n])
        pick = torch.zeros(1, prefill)
        pick[0, n - 1] = 1.0
        pre = Prefill()(padded, pick, *cross)
        drift = max(drift, float((pre[0][0] - logits[n - 1]).abs().max()))
        for j in range(2 * layers):
            drift = max(drift, float((pre[1 + j][:, :, :n] - past[j][:, :, :n]).abs().max()))
    if drift > 1e-3:
        sys.exit(
            f"the export wrapper does not match transformers (logit drift {drift:.2g}); not writing an export"
        )
    print(
        f"==> wrapper matches transformers over {len(sequence)} positions (logit drift {drift:.1g})",
        flush=True,
    )

    enc_ir = ov.convert_model(
        Encoder(), example_input=(mel,), input=[ov.PartialShape([1, mel.shape[1], MEL_FRAMES])]
    )
    enc_ir.inputs[0].get_tensor().set_names({"mel"})
    for j, port in enumerate(enc_ir.outputs):
        port.get_tensor().set_names({f"cross_{'kv'[j % 2]}{j // 2}"})
    names = ["ids", "pos", "mask"]
    names += [f"past_{'kv'[j % 2]}{j // 2}" for j in range(2 * layers)]
    names += [f"cross_{'kv'[j % 2]}{j // 2}" for j in range(2 * layers)]
    example = (
        torch.zeros(1, 1, dtype=torch.long),
        torch.zeros(1, dtype=torch.long),
        torch.zeros(1, 1, 1, slots + 1),
        *[torch.zeros(1, heads, slots, dim) for _ in range(2 * layers)],
        *cross,
    )
    dec_ir = ov.convert_model(
        Step(), example_input=example, input=[ov.PartialShape(list(e.shape)) for e in example]
    )
    for port, name in zip(dec_ir.inputs, names, strict=True):
        port.get_tensor().set_names({name})
    out_names = ["logits"] + [f"new_{'kv'[j % 2]}{j // 2}" for j in range(2 * layers)]
    for port, name in zip(dec_ir.outputs, out_names, strict=True):
        port.get_tensor().set_names({name})
    pre_names = ["ids", "pick"] + names[3 + 2 * layers :]
    pre_example = (torch.zeros(1, prefill, dtype=torch.long), torch.zeros(1, prefill), *cross)
    pre_ir = ov.convert_model(
        Prefill(),
        example_input=pre_example,
        input=[ov.PartialShape(list(e.shape)) for e in pre_example],
    )
    for port, name in zip(pre_ir.inputs, pre_names, strict=True):
        port.get_tensor().set_names({name})
    for port, name in zip(pre_ir.outputs, out_names, strict=True):
        port.get_tensor().set_names({name})

    shape = {
        "layers": layers,
        "heads": heads,
        "head_dim": dim,
        "cache_slots": slots,
        "prefill_tokens": prefill,
        "mel_bins": int(mel.shape[1]),
        "mel_frames": MEL_FRAMES,
    }
    for out in (cpu_out, npu_out):
        out.mkdir(parents=True, exist_ok=True)
        for name in COPIED:
            shutil.copy(source / name, out / name)
        (out / "step_export.json").write_text(json.dumps(shape, indent=2) + "\n")
    ov.save_model(enc_ir, str(cpu_out / "encoder.xml"), compress_to_fp16=True)
    ov.save_model(dec_ir, str(cpu_out / "decoder.xml"), compress_to_fp16=True)
    ov.save_model(pre_ir, str(cpu_out / "prefill.xml"), compress_to_fp16=True)
    print(f"==> fp16 (CPU/GPU) export in {cpu_out}", flush=True)
    core = ov.Core()
    for name in ("encoder", "decoder", "prefill"):
        model = nncf.compress_weights(
            core.read_model(str(cpu_out / f"{name}.xml")), mode=nncf.CompressWeightsMode.INT8_SYM
        )
        ov.save_model(model, str(npu_out / f"{name}.xml"))
    print(f"==> INT8 (NPU) export in {npu_out}", flush=True)

    # The IR itself, under plain openvino on the CPU: the same teacher-forced sequence.
    for label, out in (("fp16", cpu_out), ("int8", npu_out)):
        e = core.compile_model(str(out / "encoder.xml"), "CPU")
        d = core.compile_model(str(out / "decoder.xml"), "CPU").create_infer_request()
        kv = list(e.create_infer_request().infer({"mel": mel.numpy()}).values())
        cache = [np.zeros((1, heads, slots, dim), np.float32) for _ in range(2 * layers)]
        m = np.full((1, 1, 1, slots + 1), -1e4, np.float32)
        m[..., slots] = 0
        worst, agree = 0.0, 0
        for t, token in enumerate(sequence):
            feed = {"ids": np.array([[token]]), "pos": np.array([t]), "mask": m}
            feed.update({names[3 + j]: cache[j] for j in range(2 * layers)})
            feed.update({names[3 + 2 * layers + j]: kv[j] for j in range(2 * layers)})
            res = d.infer(feed)
            row = res["logits"][0]
            for j in range(2 * layers):
                cache[j][:, :, t] = res[out_names[1 + j]][:, :, 0]
            m[..., t] = 0
            ref = logits[t].numpy()
            worst = max(worst, float(np.abs(row - ref).max()))
            agree += int(row.argmax() == ref.argmax())
        print(
            f"==> {label} IR on CPU: {agree}/{len(sequence)} argmax agree, max logit drift {worst:.3g}",
            flush=True,
        )
        pf = core.compile_model(str(out / "prefill.xml"), "CPU").create_infer_request()
        ids = np.zeros((1, prefill), np.int64)
        ids[0, :n] = sequence[:n]
        onehot = np.zeros((1, prefill), np.float32)
        onehot[0, n - 1] = 1.0
        feed = {"ids": ids, "pick": onehot}
        feed.update({names[3 + 2 * layers + j]: kv[j] for j in range(2 * layers)})
        res = pf.infer(feed)
        row, ref = res["logits"][0], logits[n - 1].numpy()
        print(
            f"==> {label} prefill on CPU: argmax {'agrees' if row.argmax() == ref.argmax() else 'DIFFERS'}, "
            f"logit drift {float(np.abs(row - ref).max()):.3g}",
            flush=True,
        )

    for out in (cpu_out, npu_out):
        digests = {
            p.name: _sha256(p)
            for p in sorted(out.iterdir())
            if p.is_file() and p.name != "SHA256SUMS"
        }
        (out / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in digests.items()))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export Whisper for vinoWhisper's own decode loop."
    )
    models = _data_home() / "vinowhisper/models"
    parser.add_argument("--model", choices=MODELS, default=MODELS[0])
    parser.add_argument("--npu-out", type=Path)
    parser.add_argument("--cpu-out", type=Path)
    parser.add_argument("--cache-slots", type=int, default=CACHE_SLOTS)
    parser.add_argument("--prefill-tokens", type=int, default=PREFILL_TOKENS)
    parser.add_argument("--source-root", type=Path, default=models / "source")
    parser.add_argument("--sources", help="model_sources.json to read the pins from")
    args = parser.parse_args()
    _capped(sys.argv)
    stem = args.model.split("/")[1]
    npu_out = args.npu_out or models / f"{stem}-step-ov"
    cpu_out = args.cpu_out or models / f"{stem}-step-ov-fp16"
    tmp = _cache_home() / "vinowhisper/export-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(tmp)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    source = fetch(args.model, _pins(args.model, args.sources), args.source_root)
    os.environ["HF_HUB_OFFLINE"] = "1"
    export(source, npu_out, cpu_out, args.cache_slots, args.prefill_tokens)
    return 0


if __name__ == "__main__":
    sys.exit(main())
