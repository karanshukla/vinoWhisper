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

## Detect automatically

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
