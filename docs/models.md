# Speech models

vinoWhisper ships with Whisper small and can load NVIDIA's [Canary 1B v2](https://huggingface.co/nvidia/canary-1b-v2) instead (#143). Pick one under **Settings → Speech → Speech model**, or pass `--model whisper-small|canary-1b-v2` to `vinowhisper-server`. The choice lives in `~/.config/vinowhisper/language.json` next to the language and task. Switching it restarts the server through the same path as a language switch.

Whisper stays the default. Canary is the better pick for translation into English and for the 21 languages Whisper isn't wired up for here. On stitched English captions Whisper is slightly more accurate.

| | Whisper small(.en) | Canary 1B v2 |
|---|---|---|
| Languages | en, fr, de, es, auto | 25 European languages; auto only to translate |
| Translation | into English | into English (en → X exists in the model but is off, see below) |
| Getting it | `convert_model.sh` | Download in Settings → Speech, `vinowhisper-canary model fetch`, or `uv run scripts/export_canary.py` |
| Export size | 0.9 GB | 0.93 GB INT8 (NPU) + 1.8 GB fp16 (CPU/GPU) |
| License | MIT | CC-BY-4.0 |

## Downloading Canary

Settings → Speech has a Download row for it, `vinowhisper-setup --model canary-1b-v2` selects it and offers the download, and `vinowhisper-canary model status|fetch|delete` does the same from a terminal (JSON lines, like `vinowhisper-cleanup model`). It fetches our prebuilt export for the device the server would pick: [arankays/canary-1b-v2-int8-ov](https://huggingface.co/arankays/canary-1b-v2-int8-ov) on an NPU, [arankays/canary-1b-v2-fp16-ov](https://huggingface.co/arankays/canary-1b-v2-fp16-ov) otherwise. Both are pinned by commit and per-file sha256 in `vinowhisper/canary_model.json`, and both were downloaded back and matched the local export byte for byte (2026-11-06). Deleting the model while it's selected switches Settings back to Whisper.

## Deleting Whisper

Settings → Speech lists Whisper's files under **Whisper small**: the exports
`vinowhisper-setup` built (up to four folders: English-only or multilingual, NPU
or stateful) and the source download they were built from
(`models/source/openai--whisper-small*`, about 1 GB). Each has its own Delete…,
confirmed in place.

- **Source download** only feeds an export, so deleting it changes nothing that
  runs. A new export downloads it again.
- **Delete Whisper** removes every export and the source download. If Whisper is
  the speech model, it stops `vinowhisper-server.service` first, the way a
  language switch does, and dictation and captions stay off until
  `vinowhisper-setup` exports it again. With Canary selected the server keeps
  running.

Since 2026-11-11 the delete and the size also cover the step exports
`scripts/export_whisper.py` writes for the vocabulary prompt (the fp16 folders the
server loads and their INT8 siblings, for both language sets). They do not count
as Whisper being installed: `present` in the status still means one of the four
pipeline exports is there, because a step export decodes nothing on its own.

The page changes only after the delete has happened: "Whisper was deleted" and the
language list falling back to English wait for the refreshed status to say the
exports are gone, and a delete that failed leaves the page as it was with the
reason on a *Last attempt* row. If the status itself cannot be read, the group
says so instead of disappearing. Deleting Canary while it is the speech model
switches back to Whisper, and when no Whisper export exists the confirmation and
the note say captions and dictation stay off until `vinowhisper-setup` exports a
model.

`vinowhisper-whisper model status|delete|delete-source` does the same from a
terminal, in JSON lines like the other two. Silero VAD (1.3 MB, file imports
only) is not part of it: both speech models use it and it downloads itself again
when needed. Uninstalling removes it.

## Detect automatically

With Whisper, the overlay and the terminal status bar show what it heard: `auto: fr`, or `auto: fr → en` when translating ([#189](https://github.com/karanshukla/vinoWhisper/issues/189), 2026-11-13). Each 12 s window is detected on its own, so the label changes only when two windows in a row agree, and stays `auto` until they first do. The server sends the language only to a client that asks (`X-Vinowhisper-Language: 1`), after the text and a NUL, since OpenVINO GenAI reports it once `generate()` returns. On the NPU, `StaticWhisperPipeline` fills `WhisperDecodedResults.language` with the bare code, with a streamer and under translate (2026.4.1, espeak-ng fr, de, es and en, all four right through the server). A forced language comes back as itself, so only auto sends one. Dictation does not ask.

Canary has no language detection, but it doesn't need one to translate into English. With the wrong source language named in its prompt, it translated the same way: French audio named as German, German named as French, and English named as French all came out right (2026-11-06, two windows each). Named as English, French audio leaked French words. So with Canary, Detect automatically always means translate into English, and the prompt names `config.CANARY_AUTO_SOURCE` (`fr`). Transcribing in the detected language stays Whisper-only.

## Exporting Canary

```sh
uv run scripts/export_canary.py
```

The script is a PEP 723 script with its own environment (transformers 5.22.2, torch CPU, nncf). Canary needs transformers >= 5.15, while the Whisper NPU export needs < 5.4 (see CLAUDE.md), so it can't share the `[export]` extra. It:

1. downloads `nvidia/canary-1b-v2` at the revision in `vinowhisper/model_sources.json` and checks every file's sha256,
2. checks its own decoder wrapper against transformers (same greedy tokens, logits within 1e-3),
3. writes fp16 IR to `canary-1b-v2-ov-fp16` and per-channel INT8 to `canary-1b-v2-ov`,
4. runs itself under a `systemd-run` memory cap (90% of available) with `TMPDIR` on disk. It peaks around 10 GB.

From a wheel install, run it straight from GitHub: `uv run https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/scripts/export_canary.py`.

## How it runs

`optimum-intel` has no Canary support and `WhisperPipeline` can't load it, so `vinowhisper/canary.py` drives two IRs itself:

- **Encoder:** a static 12 s window (`[1, 1201, 128]` log-mel plus a valid-frame mask). Its outputs are the cross-attention K/V for all 8 decoder layers and the cross-attention mask.
- **Decoder:** one token per call, static shapes, no state. The host holds a 192-slot self-attention cache, the same idea as the `--disable-stateful` Whisper export.
- **Features:** Parakeet's mel extractor ported to numpy (slaney filters computed in place, no librosa or torch at runtime). It matches transformers' to 2.3e-4.
- **Longer audio** (a dictation piece, up to 29.5 s) is cut at the quietest 100 ms in the last 3 s before each 12 s mark and decoded chunk by chunk.

Traps found building it:

- **The feature extractor counts `len // 160` frames as real, one fewer than it computes** (1200 of 1201 for 12 s). Without that mask the encoder's output moves by up to 2.5 and decoding degrades.
- **The CPU plugin can't compile the INT8 IR** (`m_element_type.is_static()`, OpenVINO 2026.4.0), hence the fp16 export for CPU/GPU.
- **On a CPU with AVX512_BF16 the fp16 export returned no text at all** (2026-11-10, a cloud Xeon, OpenVINO 2026.4.1): the CPU plugin picks bfloat16 there and the decoder's first token is end-of-text. `canary.py` asks for `INFERENCE_PRECISION_HINT=f32` on the CPU. The Core 5 320 has no BF16, so it never showed on the laptop.
- **INT4 channel-wise on the encoder was no faster than INT8 on the NPU** and changed tokens.

## Measured 2026-11-06 (NPU, Core 5 320, OpenVINO 2026.4.0)

Per 12 s window, INT8: 0.56-0.65 s (encoder ~115 ms, decoder ~7.5 ms per token, first token ~0.18-0.21 s). Whisper small.en: 0.67 s on the same windows. The NPU compile is 16 s cold and 3.5 s from the driver cache.

LibriVox Pride and Prejudice ch. 1-3, 24-300 s, scored against the Gutenberg text:

| | Whisper | Canary |
|---|---|---|
| 23 separate 12 s windows | 12.1% WER | 8.0% |
| Stitched session, as `caption_events` paces it | **4.8%** | 10.5%, then **5.8%** with the loop guard |

Canary's lead on separate windows came from Whisper dropping words at window starts, which the stitcher's overlap already covers. Stitched, Canary had 9 hallucinated cycles out of 330 against Whisper's 1 of 403: repetition loops ("The two of them were very well known, and the two of them…"), mostly on the short windows at startup. Two such cycles in a row agree, so LocalAgreement committed them. `Stitcher.push` now drops a decode whose zlib compression ratio is over 2.0. Canary's loops measured 2.4-2.9; real text peaked at 1.66 for both models, so the guard costs Whisper nothing.

**Translation into English** (10 windows each from one LibriVox reading, judged by eye, no reference translation): Canary keeps all the content but translates close to word by word and leaks source words ("ungeefer" for *Ungeziefer*); Whisper reads more naturally but drops and invents content. 2 of 10 Spanish windows (a 1605 legal preamble) came back untranslated from Canary. Stitched French → English was clearly better with Canary.

**English → other languages** is in the model but not offered: on 5-7 of 23 caption windows it returned the English transcript instead, always on windows that start mid-sentence.

**Confidence commits** (Canary only, not built yet): the decode loop has every token's probability, which `WhisperPipeline` never exposed. Replaying a stitched session, committing pending words at probability >= 0.8 while holding back each window's last two showed half the words 0.53 s sooner, at 5.9% WER against 6.0%. Below 0.8, wrong words start committing.

All of it is n=1 on one laptop and audiobook narration, not the YouTube content captions usually see.

## Whisper through our own decode loop (#176)

`WhisperPipeline` refuses a decoder prompt on the NPU (an assert in `pipeline_static.cpp`), so the vocabulary list only reaches the NPU as spelling correction. The way around it is the same as Canary's: our own export and our own loop.

**The export (#177)** is `scripts/export_whisper.py` (PEP 723, its own transformers 5.22.2 and OpenVINO 2026.4.0). It traces the model itself, not through optimum, so the `transformers<5.4` pin the stock NPU export needs does not apply. It writes:

- `encoder.xml`: 80 x 3000 log-mel in (30 s), each decoder layer's cross-attention K and V out (`cross_k0` ... `cross_v11`).
- `decoder.xml`: one token per call, no state. Inputs `ids`, `pos`, `mask` (1 x 1 x 1 x slots+1), the self-attention cache `past_k*`/`past_v*` (1 x 12 x slots x 64) and the cross K/V. Outputs `logits` and the step's `new_k*`/`new_v*`, which the host writes into the cache at `pos`.
- `prefill.xml` (since #179): up to 128 prompt tokens in one call, padded, with a causal mask built in. Inputs `ids` (1 x 128), `pick` (a one-hot row marking the last real token) and the cross K/V; outputs that token's `logits` and every layer's K/V for all 128 positions. The first version took the last position as an int64 index, and the NPU compiler could not place it (`FeasibleAllocation Pass failed`); the one-hot row is a matmul and compiles.
- `step_export.json` (layers, heads, slots, prefill tokens), the tokenizer and configs, and `SHA256SUMS`.

448 cache slots by default (`--cache-slots`), Whisper's `max_target_positions`, since the prompt and the answer share them. INT8 (per-channel, symmetric) in `whisper-small.en-step-ov`, fp16 in `whisper-small.en-step-ov-fp16`. `--model openai/whisper-small` for the multilingual one. The source is the pinned revision in `model_sources.json`, checked by sha256, exported offline.

Before writing anything, it teacher-forces a dictation-shaped sequence (`<|startofprev|>`, a vocabulary, the start tokens, an answer: 26 positions) through its wrapper and through transformers, and refuses on a logit drift over 1e-3. Then it runs the saved IR under plain OpenVINO on the CPU.

| Measured 2026-11-11 (whisper-small.en, 7 s of noise) | Max logit drift | Argmax agrees |
|---|---|---|
| Wrapper against transformers (fp32) | 6e-05 | |
| fp16 IR, CPU | 9.5e-05 | 26 / 26 |
| INT8 IR, CPU | 1.34 | 25 / 26 |

The INT8 drift is on noise, where the logits are flat. Whether INT8 holds on speech is #178's first check.

**On the NPU** (2026-11-11, Core 5 320, driver 1.35.0, OpenVINO 2026.4.0, n=1), with an empty driver cache:

| | INT8 | fp16 |
|---|---|---|
| Encoder compile | 23.4 s | 21.3 s |
| Decoder compile | 2.0 s | 2.1 s |
| Encoder, 30 s | 226 ms | 177 ms |
| Decoder step, every input passed to `infer()` | 28.6 ms | 31.7 ms |
| Decoder step, written into the request's own tensors | 11.7 ms | 15.6 ms |

How the inputs are fed decides the step time. Passing numpy arrays to `infer()` copies 24 cross tensors of 4.6 MB each into the NPU on every token; `set_tensor` with shared host memory barely helps (25 ms). Writing into `request.get_tensor(name).data` once (cross K/V) and per step (one cache slot) is what gets 11.7 ms. #178's loop has to work that way.

Rough cost of a 12 s dictation piece at that rate: 0.2 s encoder plus about 39 steps (4 start tokens, 35 answer tokens) is 0.66 s, against 0.70 s through `WhisperPipeline`. A 20-term prompt adds about 60 steps, 0.7 s, unless the prompt is prefilled in one call. That is an estimate from step timings, not a decode.

**The loop (#178)** is `vinowhisper/whisper_step.py`, built on 2026-11-11. Everything around the two IRs is numpy and Python, so it is tested without OpenVINO:

- **Features:** Whisper's log-mel (n_fft 400, hop 160, the 80 x 201 filters from `preprocessor_config.json`, 30 s of zero padding). Within 3.3e-5 of transformers' extractor on speech; `tests/fixtures/whisper_features.npz`.
- **Tokenizer:** GPT-2 byte-level BPE read from `tokenizer.json`, with the pre-tokenizer's `\p{L}`/`\p{N}` spelled for the stdlib `re`. It matches transformers on accents, emoji, CJK, contractions and underscores. The test fixture is a 5 KB tokenizer holding only the 106 merges its cases use, which encodes them identically (a merge never chosen can't change the result).
- **Prompt:** as `context_tokens.cpp` builds `hotwords`: `<|startofprev|>`, the tokens of `" " + terms` with special tokens dropped, `<|startoftranscript|>` (plus language and task on the multilingual model), `<|notimestamps|>`.
- **Greedy:** `begin_suppress_tokens` on the first answer token, `suppress_tokens` on all of them, timestamps never, stop at `<|endoftext|>`, never past the cache.

Text against `WhisperPipeline`, both on the NPU, no prompt, the 52 pieces of the 19 min LibriVox reading (2026-11-11):

| Export | Identical pieces | Words differing |
|---|---|---|
| fp16 encoder + fp16 decoder | 52 / 52 | 0 / 3395 |
| fp16 encoder + INT8 decoder | 42 / 52 | 3 / 3395 |
| INT8 encoder + INT8 decoder | 39 / 52 | 7 / 3395 |

So the server reads the fp16 export (`config.STEP_MODEL_DIR`). INT8's differences are mostly punctuation and capitals, but include "settled" read as "settle" and a dropped "is".

**Timing on a quiet NPU (#179, 2026-11-11).** The figures above were taken while vinoSearch was indexing on the same NPU; these were taken after it had been under 1% busy for a minute. fp16 export, Core 5 320, driver 1.35.0, OpenVINO 2026.4.0, 8 pieces of the LibriVox reading per row, median and worst:

| Piece | `WhisperPipeline` | Loop, no prompt | Loop, 20 terms | Loop, 30 terms |
|---|---|---|---|---|
| 5 s | 0.37 s (0.41) | 0.46 s (0.55) | 1.62 s (2.48) | 2.04 s (2.17) |
| 12 s | 0.65 s (0.80) | 0.80 s (0.97) | 1.80 s (2.04) | 1.93 s (2.44) |
| 29 s | 1.54 s (1.90) | 1.82 s (2.38) | 7.31 s (17.1) | 6.42 s (8.10) |

- Without a prompt the loop is about 20% behind `WhisperPipeline`.
- The prompt is the cost: each of its tokens is one decoder step (14.6 ms on a quiet NPU), so 20 terms (about 80 tokens) add about 1.2 s before the first word. These rows are from before the prefill, which now takes the whole prompt in one call (below).
- The 29 s worst cases are runaway decodes on mid-sentence windows (below), each running to its token limit.
- Encoder 285 ms. Cold load from an empty driver cache: 24.8 s for the loop's two models, 27.7 s for `WhisperPipeline`. NPU memory with both loaded: 157 MB (`drm-resident-memory`).

**A prompt can wipe out a piece that starts mid-sentence.** On 8 s windows cut from the continuous reading, a list of names never spoken made the decode collapse to "and", "Chitlana," or "Xochitl," on 15 of 40 windows at 10 terms, 9 at 20 and 8 at 30. transformers' own `generate(prompt_ids=...)` gives the same text on the CPU, so this is Whisper, not the loop, and it applies to `hotwords` on a CPU or GPU too. On 38 pieces bounded by real pauses (0.5 s or more, median 5.3 s), as dictation cuts them, none lost words. #175's 3.23% to 3.36% on whole LibriSpeech utterances agrees. The exposure is a long dictation cut without a pause.

**With the prefill** (same day, same quiet NPU), the whole prompt is one 23 ms call, and the loop writes its K/V into the first slots before decoding on as before. On 48 prompted decodes it produced the same tokens as feeding the prompt step by step. Median (worst) of 8 pieces:

| Piece | `WhisperPipeline`, no prompt | Loop, prefill, 20 terms | Loop, step by step, 20 terms |
|---|---|---|---|
| 5 s | 0.36 s (0.41) | 0.47 s (1.32) | 1.63 s (2.54) |
| 29 s | 1.55 s (1.90) | 1.84 s (5.59) | 2.96 s (6.72) |

A typical prompted dictation piece now costs about 0.1 s more than today's unprompted one, which is #179's "about the same" outcome. If `prefill.xml` does not compile, the loop logs it and feeds the prompt step by step. If the file is missing (an export from before #179), it does the same without a log line.

The loop's models are compiled when the server loads Whisper on the NPU, if the step export is there, the language is not auto and `vocabulary.txt` has terms (2026-11-11): the 24.8 s cold compile used to land inside the first prompted dictation, under the transcriber's lock. Otherwise they still load at the first dictation that needs them. A load that fails turns the prompt off until the model is next unloaded and loaded; a device reset (`ZE_RESULT_ERROR_DEVICE_LOST`) is not counted as a failure and goes through the usual reload. Unit tests only, not run on an NPU.

`scripts/export_whisper.py` exits non-zero when the fp16 IR does not agree with transformers on every token of its check sequence, or when the fp16 prefill picks a different token (2026-11-11). The INT8 comparison only warns: 25 / 26 is its recorded result, and nothing loads the INT8 export. `--fp16-out` and `--int8-out` name the two directories (`--cpu-out` and `--npu-out` still work).
