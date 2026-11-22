# Speech models

vinoWhisper defaults to Whisper small and also supports NVIDIA's [Canary 1B v2](https://huggingface.co/nvidia/canary-1b-v2) (#143). Select a model under **Settings → Speech → Speech model** or pass `--model whisper-small|canary-1b-v2` to `vinowhisper-server`. The choice is stored in `~/.config/vinowhisper/language.json` alongside language and task settings; switching restarts the server.

Whisper remains slightly more accurate on stitched English captions, while Canary is stronger for translation into English and supports 25 European languages.

| Feature | Whisper small(.en) | Canary 1B v2 |
|---|---|---|
| Languages | `en`, `fr`, `de`, `es`, `auto` | 25 European languages (`auto` translates to English only) |
| Translation | Into English | Into English (`en → X` exists in model but is disabled; see below) |
| Installation | `convert_model.sh` | Settings → Speech, `vinowhisper-canary model fetch`, or `uv run scripts/export_canary.py` |
| Export size | 0.9 GB | 0.93 GB INT8 (NPU) + 1.8 GB fp16 (CPU/GPU) |
| License | MIT | CC-BY-4.0 |

## Downloading Canary

Download Canary from **Settings → Speech**, via `vinowhisper-setup --model canary-1b-v2`, or from the terminal with `vinowhisper-canary model status|fetch|delete` (outputs JSON lines like `vinowhisper-cleanup model`).

The fetcher downloads the prebuilt export matching the server's selected device:

- NPU: [arankays/canary-1b-v2-int8-ov](https://huggingface.co/arankays/canary-1b-v2-int8-ov)
- CPU/GPU: [arankays/canary-1b-v2-fp16-ov](https://huggingface.co/arankays/canary-1b-v2-fp16-ov)

Both repositories are pinned by commit and per-file SHA-256 in `vinowhisper/canary_model.json` (re-downloaded and verified byte-for-byte against local exports on 2026-11-06). Deleting Canary while it is selected switches Settings back to Whisper.

## Deleting Whisper

**Settings → Speech → Whisper small** lists the exports built by `vinowhisper-setup` (up to four directories: English-only/multilingual × NPU/stateful) and the ~1 GB source download (`models/source/openai--whisper-small*`). Each item has an in-place confirmed Delete action:

- **Source download:** Used only as export input; deleting it does not affect running models and future exports re-download it if needed.
- **Delete Whisper:** Deletes all exports (including the fp16 and INT8 step exports written by `scripts/export_whisper.py` for vocabulary prompting, since 2026-11-11) and the source download. If Whisper is active, it stops the server first (`vinowhisper-server --stop`) and leaves captions and dictation inactive until `vinowhisper-setup` exports a model again. If Canary is active, the server continues running. Step exports alone do not mark Whisper as `present` because they cannot run live captioning on their own.

Settings updates the UI only after the refreshed status confirms deletion; failed deletes leave the page unchanged and display the error on a *Last attempt* row. If status cannot be read, the group shows an error instead of disappearing.

CLI equivalent (JSON lines): `vinowhisper-whisper model status|delete|delete-source`. Silero VAD (1.3 MB, used by both speech models) is kept separately and removed only on uninstall.

## Other languages

French, German, and Spanish work offline with Whisper's multilingual `whisper-small` model. Enable it with `vinowhisper-setup --language fr` (~1 GB additional disk space alongside `whisper-small.en`), which writes `~/.config/vinowhisper/language.json` (overridable via `vinowhisper-server --language fr`). You can also switch **Language** and **Translate to English** in the tray menu; switching stops captions, restarts the server on the new model (10–30s cold load), and resumes. `en` keeps `whisper-small.en`.

- **English-only translation:** `--task translate` translates French, German, or Spanish speech into English inside the same model at no extra cost. Translating into other languages is not supported.
- **Specify the language when known:** `--language auto` re-detects language on every window and can flip mid-caption. Forcing a language on mismatched audio makes Whisper output in the forced language anyway.
- **NPU status:** Verified on CPU on 2026-10-06 using INT8 multilingual small and synthetic speech (transcription, forced language, auto-detect, and translate all worked). The multilingual NPU export has not been benchmarked or digest-pinned (`vinowhisper-doctor` reports it `unpinned`), real-speech/accent accuracy is untested, and multilingual `small` is weaker on English than `small.en`.

## Detect automatically

With Whisper, the overlay and terminal status bar display the detected language as `auto: fr` or `auto: fr → en` when translating ([#189](https://github.com/karanshukla/vinoWhisper/issues/189), 2026-11-13). Because each 12s window is detected independently, the displayed label updates only after two consecutive windows agree (staying `auto` until then). Clients opt in by sending `X-Vinowhisper-Language: 1`; the server appends a NUL byte and the language code after the text once `generate()` completes. On the NPU (`StaticWhisperPipeline`, OpenVINO 2026.4.1), `WhisperDecodedResults.language` returns the bare language code under streaming and translation (verified with `espeak-ng` in `fr`, `de`, `es`, `en`). Forced languages are not echoed, and dictation does not request the header.

Canary lacks native language detection, but translates into English reliably even when the prompt specifies a different European source language (tested 2026-11-06, 2 windows each: French labeled German, German labeled French, and English labeled French all translated correctly, whereas French labeled English leaked French words). Therefore, under Canary, "Detect automatically" always translates to English using `config.CANARY_AUTO_SOURCE` (`fr`). Auto-language transcription remains Whisper-only. Of Canary's 25 languages, only `en`, `fr`, `de`, and `es` have been tested (2026-11-06).

## Exporting Canary

```bash
uv run scripts/export_canary.py
```

`export_canary.py` is a PEP 723 standalone script with its own dependencies (`transformers` 5.22.2, CPU `torch`, `nncf`) because Canary requires `transformers>=5.15`, whereas Whisper's NPU export requires `transformers<5.4`. The script:

1. Downloads `nvidia/canary-1b-v2` at the pinned revision in `vinowhisper/model_sources.json` and verifies every file's SHA-256.
2. Validates its decoder wrapper against `transformers` (identical greedy tokens, logits within `1e-3`).
3. Writes fp16 IR to `canary-1b-v2-ov-fp16` and per-channel INT8 IR to `canary-1b-v2-ov`.
4. Runs under a `systemd-run` memory cap (90% of available RAM, ~10 GB peak) with `TMPDIR` on disk.

From a wheel install without a checkout:

```bash
uv run https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/scripts/export_canary.py
```

## How Canary runs

Because `optimum-intel` and `WhisperPipeline` do not support Canary, `vinowhisper/canary.py` runs two OpenVINO IRs directly:

- **Encoder:** Static 12s window (`[1, 1201, 128]` log-mel + valid-frame mask) outputting cross-attention K/V for all 8 decoder layers and the cross-attention mask.
- **Decoder:** Stateless single-token step with static shapes and a host-managed 192-slot self-attention KV cache (the same approach as the `--disable-stateful` Whisper export).
- **Features:** Parakeet's mel extractor ported to NumPy (Slaney filters computed in place; matches `transformers` within `2.3e-4`, no `librosa` or `torch` at runtime).
- **Long audio (up to 29.5s dictation):** Split at the quietest 100 ms in the 3s preceding each 12s mark and decoded chunk by chunk.

Implementation caveats:

- The feature extractor treats `len // 160` frames as valid (1200 of 1201 for 12s); omitting this mask shifts encoder outputs by up to 2.5 and degrades decoding.
- OpenVINO's CPU plugin fails to compile the INT8 IR (`m_element_type.is_static()`, OpenVINO 2026.4.0), which is why CPU/GPU use the fp16 export.
- On CPUs with `AVX512_BF16` (observed 2026-11-10 on a cloud Xeon, OpenVINO 2026.4.1), the CPU plugin defaults to `bfloat16` and emits end-of-text immediately. `canary.py` sets `INFERENCE_PRECISION_HINT=f32` on CPU (Core 5 320 lacks BF16, so it only appeared on Xeon).
- Channel-wise INT4 on the encoder was no faster than INT8 on the NPU and altered output tokens.

## Canary benchmarks (2026-11-06)

Measured on Core 5 320 NPU, OpenVINO 2026.4.0 (n=1, audiobook narration rather than typical video audio):

- **Latency (INT8, 12s window):** 0.56–0.65s (encoder ~115 ms, decoder ~7.5 ms/token, first token ~0.18–0.21s), vs. 0.67s for `whisper-small.en` on the same windows. NPU compile takes 16s cold and 3.5s from driver cache.
- **Accuracy (LibriVox *Pride and Prejudice* ch. 1–3, 24–300s, scored against Gutenberg text):**

| Evaluation mode | Whisper small.en | Canary 1B v2 |
|---|---|---|
| 23 separate 12s windows | 12.1% WER | 8.0% WER |
| Stitched session (`caption_events` pacing) | **4.8% WER** | 10.5% WER (**5.8%** with loop guard) |

Canary's advantage on isolated windows came from Whisper dropping words at window starts (already handled by stitcher overlap). In stitched mode, Canary had 9 hallucinated repetition loops in 330 cycles (mostly on short startup windows) vs. Whisper's 1 in 403. Because two consecutive loops agree, `Stitcher.push` now drops decodes with a zlib compression ratio above 2.0 (Canary loops measured 2.4–2.9; normal speech peaked at 1.66 for both models).

- **Translation into English (10 windows per language from LibriVox, manual review):** Canary retained content more faithfully (though more literal, occasionally leaking source words like "ungeefer" for *Ungeziefer*, and leaving 2 of 10 Spanish legal-preamble windows untranslated), while Whisper read more naturally but dropped or invented phrases. Stitched French → English clearly favored Canary.
- **English → other languages (disabled):** Returned untranslated English on 5–7 of 23 windows, consistently when windows started mid-sentence.
- **Confidence commits (prototyped, not implemented):** Canary exposes per-token probabilities. Replaying a stitched session and committing pending words with probability ≥ 0.8 (while holding back the last two words) displayed half the words 0.53s sooner at 5.9% vs. 6.0% WER; thresholds below 0.8 committed incorrect words.

## Custom Whisper step decode loop (#176)

Because `WhisperPipeline` rejects decoder prompts on the NPU (`pipeline_static.cpp` assert), vocabulary prompting on the NPU uses a custom export and decode loop (matching Canary's architecture).

### Step export (`scripts/export_whisper.py`, #177)

A PEP 723 script (`transformers` 5.22.2, OpenVINO 2026.4.0) that traces Whisper directly without `optimum` (avoiding the `transformers<5.4` restriction). It exports from the pinned source in `model_sources.json` (offline, SHA-256 verified) with 448 KV cache slots (`--cache-slots`) into `whisper-small.en-step-ov` (per-channel symmetric INT8) and `whisper-small.en-step-ov-fp16` (fp16), or `--model openai/whisper-small` for multilingual (`--fp16-out`/`--int8-out`, legacy aliases `--cpu-out`/`--npu-out`):

- `encoder.xml`: 80 × 3000 log-mel input (30s), outputs cross-attention K/V for all 12 decoder layers (`cross_k0`…`cross_v11`).
- `decoder.xml`: Stateless single-token step taking `ids`, `pos`, `mask` (`1 × 1 × 1 × slots+1`), `past_k*`/`past_v*` (`1 × 12 × slots × 64`), and cross K/V; outputs `logits` and `new_k*`/`new_v*`.
- `prefill.xml` (#179): Processes up to 128 prompt tokens in one call with a causal mask, taking `ids` (`1 × 128`), a one-hot `pick` row (an `int64` index caused NPU `FeasibleAllocation Pass failed`; one-hot matmul compiles cleanly), and cross K/V; outputs last-token `logits` and K/V for all 128 slots.
- `step_export.json` (layers, heads, slots, prefill tokens), tokenizer/config files, and `SHA256SUMS`.

Before saving, the exporter teacher-forces a 26-token dictation-shaped sequence (`<|startofprev|>`, vocabulary, start tokens, answer) against `transformers` and fails if fp32 wrapper logit drift exceeds `1e-3`, or if the saved fp16 IR or fp16 prefill disagrees on any argmax token (2026-11-11). INT8 comparison warns only (25/26 agreement on noise; INT8 is not loaded at runtime).

| Export check (2026-11-11, `whisper-small.en`, 7s noise) | Max logit drift | Argmax agreement |
|---|---|---|
| Wrapper vs. `transformers` (fp32) | `6e-05` | — |
| fp16 IR (CPU) | `9.5e-05` | 26 / 26 |
| INT8 IR (CPU) | `1.34` | 25 / 26 |

### Step decode loop (`vinowhisper/whisper_step.py`, #178)

Pure Python/NumPy wrapper around the IRs (unit-tested without OpenVINO):

- **Features:** Whisper log-mel (`n_fft=400`, `hop=160`, 80 × 201 filters from `preprocessor_config.json`, 30s zero-padding; matches `transformers` within `3.3e-5`, pinned by `tests/fixtures/whisper_features.npz`).
- **Tokenizer:** GPT-2 byte-level BPE loaded from `tokenizer.json` with stdlib `re` equivalents for `\p{L}`/`\p{N}` (matches `transformers` on accents, emoji, CJK, contractions, and underscores; tested with a 5 KB 106-merge fixture).
- **Prompt layout:** Mirrors `context_tokens.cpp` `hotwords`: `<|startofprev|>`, tokens of `" " + terms` (special tokens stripped), `<|startoftranscript|>` (plus language/task for multilingual), `<|notimestamps|>`.
- **Greedy decoding:** Applies `begin_suppress_tokens` on the first answer token and `suppress_tokens` on all tokens, suppresses timestamps, and stops at `<|endoftext|>` or cache capacity.

### Step-loop NPU measurements (2026-11-11)

Measured on Core 5 320 NPU, driver 1.35.0, OpenVINO 2026.4.0 (n=1):

**Compile and per-step latency (empty driver cache):**

| Metric | INT8 | fp16 |
|---|---|---|
| Encoder compile | 23.4s | 21.3s |
| Decoder compile | 2.0s | 2.1s |
| Encoder (30s) | 226 ms | 177 ms |
| Decoder step (copying NumPy arrays into `infer()`) | 28.6 ms | 31.7 ms |
| Decoder step (writing in-place into `request.get_tensor(name).data`) | 11.7 ms | 15.6 ms |

Writing cross K/V once and updating single cache slots in-place via `request.get_tensor(name).data` avoids copying 24 × 4.6 MB cross tensors per step (`set_tensor` with shared host memory still took 25 ms). Estimated unprompted 12s piece at 11.7 ms/step: ~0.66s vs. 0.70s for `WhisperPipeline`.

**Transcript parity vs. `WhisperPipeline` (52 pieces, 19-min LibriVox reading, no prompt):**

| Export | Identical pieces | Differing words |
|---|---|---|
| fp16 encoder + fp16 decoder | 52 / 52 | 0 / 3395 |
| fp16 encoder + INT8 decoder | 42 / 52 | 3 / 3395 |
| INT8 encoder + INT8 decoder | 39 / 52 | 7 / 3395 |

Because INT8 dropped or altered words ("settled" → "settle", dropped "is"), the server loads the fp16 export (`config.STEP_MODEL_DIR`).

**Quiet-NPU end-to-end timing (#179, <1% NPU load, fp16, 8 LibriVox pieces per row, median and worst):**

| Piece length | `WhisperPipeline` | Step loop (no prompt) | Step loop (20 terms, step-by-step) | Step loop (30 terms, step-by-step) |
|---|---|---|---|---|
| 5s | 0.37s (0.41) | 0.46s (0.55) | 1.62s (2.48) | 2.04s (2.17) |
| 12s | 0.65s (0.80) | 0.80s (0.97) | 1.80s (2.04) | 1.93s (2.44) |
| 29s | 1.54s (1.90) | 1.82s (2.38) | 7.31s (17.1) | 6.42s (8.10) |

- Unprompted, the step loop is ~20% slower than `WhisperPipeline` (encoder 285 ms; cold compile from empty cache 24.8s vs. 27.7s; NPU memory with both loaded: 157 MB `drm-resident-memory`).
- **Mid-sentence prompt collapse:** On 8s windows cut mid-sentence with unspoken names in the prompt, Whisper collapsed to single words ("and", "Chitlana,", "Xochitl,") on 15/40 windows (10 terms), 9/40 (20 terms), and 8/40 (30 terms). `transformers.generate(prompt_ids=...)` reproduces this on CPU, so it is a Whisper behavior affecting `hotwords` on all devices. On 38 pause-bounded pieces (≥0.5s pauses, median 5.3s, matching normal dictation), 0 pieces lost words (consistent with #175's 3.23% → 3.36% on whole LibriSpeech utterances). Long dictations cut without a pause remain exposed.

**With `prefill.xml` (same quiet NPU, 23 ms single-call prompt prefill, identical tokens across 48 prompted decodes, median and worst of 8 pieces):**

| Piece length | `WhisperPipeline` (no prompt) | Step loop + prefill (20 terms) | Step loop step-by-step (20 terms) |
|---|---|---|---|
| 5s | 0.36s (0.41) | 0.47s (1.32) | 1.63s (2.54) |
| 29s | 1.55s (1.90) | 1.84s (5.59) | 2.96s (6.72) |

Prefill reduces prompted dictation overhead to ~0.1s above an unprompted decode. If `prefill.xml` is missing (older export) or fails to compile (logged), the loop falls back to step-by-step prompt feeding.

Since 2026-11-11, when the step export exists, language is not `auto`, and `vocabulary.txt` is non-empty, the server pre-compiles the step models during Whisper NPU startup so the 24.8s cold compile does not stall the first dictation (otherwise they compile on first prompted dictation). A compile failure disables prompting until reload; `ZE_RESULT_ERROR_DEVICE_LOST` triggers normal device-reset reload rather than disabling prompting (covered by unit tests; not yet run on NPU).
