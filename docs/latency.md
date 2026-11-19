# Latency

Caption latency comes from two coupled factors: the time each decode cycle takes, and the two-cycle agreement rule that prevents unstable words from flashing on screen.

## Window size and cycle time

Because the caption loop is synchronous, the hop between consecutive windows equals the previous cycle's decode time. Because LocalAgreement-2 requires two cycles to agree before printing a word, captions trail audio by roughly twice the cycle time.

Whisper's encoder always pads input to 30s (fixed cost), while the decoder runs autoregressively (one pass per output token). A 29.5s window of dense speech emits ~2.5x as many tokens as a 12s window and takes proportionally longer. For that reason, the default window is 12s:

```bash
vinowhisper-caption --window 8      # lower lag, less context, more wording drift
vinowhisper-caption --window 20     # steadier wording, higher lag
```

Measured 2026-09-12 against the live NPU server at live-loop pacing (12s window):

- Synthetic speech (28 words/window): 0.66s mean decode.
- LibriVox narration (32 words/window, 806 cycles over 10 minutes): 0.70s mean, 0.85s p90, with no drift over the session (~0.7s hop, ~1.5s lag floor).

Full 10-minute LibriVox pass scored against the Gutenberg text (2026-09-12, n=1, single speaker, one Wildcat Lake laptop):

| Window | Cycles | Decode mean | Decode p90 | Hop | Lag floor | Wrong or extra words | Dropped words |
|---|---|---|---|---|---|---|---|
| 8s | 1044 | 0.53s | 0.61s | 0.57s | ~1.1s | 102 | ~28 |
| 12s | 806 | 0.70s | 0.85s | 0.74s | ~1.4s | 93 | ~38 |
| 16s | 665 | 0.87s | 1.06s | 0.90s | ~1.7s | 93 | ~24 |

Accuracy plateaus at 12s and drops ~10% at 8s because shorter windows cut more phrases mid-sentence. 12s is the default; 8s trades minor accuracy for ~0.3s lower lag, while 16s adds lag without measurable accuracy gains.

- Use `--debug` to inspect window length, hop, RMS, applied gain, time to first streamed token, total cycle time, and confirmed vs. pending word counts.
- Decoding waits until the buffer holds at least 1.5s (`config.MIN_WINDOW_S`) to avoid sub-second hallucinations. On exit, pending words that agreed once are flushed rather than dropped.
- Use `vinowhisper-replay DIR --sweep` to benchmark window sizes on your own recordings. Its `est. lag` column reports `2 × mean`:

  ```
  | window | decodes | mean | p90 | first piece | words/decode | est. lag |
  ```

## Why captions drift across windows

Each cycle re-decodes an overlapping audio window with additional right-context, and Whisper's phrasing often shifts until it has enough context. For example, testing on 2026-08-03 produced "Ex-sherzer", "you're told", and "you know Dalton" across three consecutive cycles for the same audio.

`stitch.py` applies LocalAgreement-2: a word is committed only after two cycles agree on it. This also suppresses single-cycle hallucinations on quiet audio, at the cost of the two-cycle lag floor.

Priming each decode with the prior transcript via `initial_prompt` is blocked on OpenVINO's NPU pipeline (`RuntimeError: 'initial_prompt' parameter is not supported on NPU device`), so some cross-window wording drift remains.

## How the stitcher finds overlap

Each rule in `stitch.py` addresses a failure mode observed in real recordings:

- **Search only the last 40 confirmed words:** Prevents locking onto an earlier repetition of a short phrase. Raised from 15 on 2026-08-03 after slow cycles and repeated phrases caused boundary restatements ("was with the best pitcher in baseball. was with the best pitcher in baseball for the first month.").
- **Case- and punctuation-insensitive comparison:** Prevents capitalization ("world series" vs. "World Series", 2026-08-03) and punctuation drift (2026-08-07) from breaking anchors and reprinting sentences.
- **Prefer the furthest valid match, not the longest:** On 2026-08-03, a single inserted word ("acquire Hose Jose Soriano") split a match; picking the longer earlier block reprinted an entire sentence.
- **Scale minimum match length for short decodes:** A fixed 3-word minimum failed on 1-word transcripts, allowing repeated hallucinations ("you" 26 times on quiet audio) to reprint every cycle.
- **Skip re-spelled trailing confirmed words (`_REDECODE_RATIO`):** With a 0.7s hop, newly confirmed words sit near the window edge where spelling can still settle (2026-09-12: "Whiskers" / "Whispers" / "Whisper's" caused "Whispers Whisper's encoder"). Confirmed words past the anchor are compared against the head of the new decode as joined strings (so "auto-aggressive. One" matches "auto regressive one") and skipped if similar enough. On the 10-minute LibriVox recording this reduced insertions from 60 to 43 (no reprints left), and on synthetic speech from 8 to 3.
- **Check every overlap length at the boundary (longest first):** Fixes a 2026-09-01 bug that compared position-by-position from one window back and silently stopped matching after the first minute of a session.
- **Realign on pending words after a stall:** If music, noise, or a speaker change stalls commits until the confirmed tail leaves the window, realigning on pending words prevents permanent stalls or large drops. Measured 2026-09-12 under forced 4-cycle stalls: dropped words fell from 75/141 to 28/141.
- **Require minimum anchor coverage (`_MIN_ANCHOR_COVERAGE = 0.4`):** Distinguishes a true overlap from a recurring phrase. Measured 2026-09-21 at 1.75x speech: after a stall, matching "man of large" between "a young man of large fortune" and "a single man of large fortune" dropped 47 valid words. Values from 0.3 to 0.5 gave identical results; 0.6 caused reprints.
- **Keep two-cycle agreement:** Testing three-cycle agreement on the same recording cut insertions by 15 but dropped 31 more words and doubled pending hold time.
- **Cap repeat collapse at ~5-word units:** Matches the longest observed loop ("do things that make you", 2026-09-01) while staying conservative so valid repeated phrasing is not deleted.

## Translation drift and lookback agreement

Measured 2026-10-06 on multilingual `whisper-small` using saved decodes of LibriVox recordings (French, German, Spanish, and English spliced with switches every 10–30s; two recordings of 160s and 155s). Translation lags transcription even though decode time is similar (0.57s vs. 0.61s): on one recording, mean commit gap was 1.9s vs. 1.4s, p90 was 4.0s vs. 2.3s, and 68% of cycles committed nothing. Translated phrasing alternates between variants across windows ("on the ship" / "on the crowd" / "on the ship"), resetting strict consecutive agreement on every flip.

`stitch.py` now checks agreement against either of the last two decodes (`_LOOKBACK_DECODES`), still requiring at least two decodes to agree before printing:

| Recording | Task | Mean gap | p90 gap | Longest stall | Words committed |
|---|---|---|---|---|---|
| Mixed 1 | translate | 1.5s → 1.4s | 2.9s → 2.3s | 13.3s → 12.3s | 469 → 475 |
| Mixed 2 | translate | 1.9s → 1.5s | 4.0s → 2.5s | 7.6s → 7.6s | 366 → 395 |
| Mixed 1 | transcribe | 1.5s → 1.4s | 3.2s → 2.8s | 10.1s → 10.9s | 414 → 422 |
| Mixed 2 | transcribe | 1.4s → 1.3s | 2.3s → 1.7s | 11.0s → 11.0s | 382 → 385 |
| English-only (240s, 1x) | transcribe | 1.06s → 1.01s | 2.0s → 1.6s | 5.8s → 4.3s | 828 → 828 |
| English-only (160s, 1.5x) | transcribe | 1.46s → 1.31s | 2.7s → 1.7s | 8.8s → 6.9s | 796 → 798 |

Accuracy relative to forced-language reference decodes stayed within ±0.02, and repeated trigram share stayed at 0.00–0.02. On the 77-decode `espeak` fixture, matched reference words rose from 123/141 to 126/141 (including one wrong early flip, "the coding" for "decoding"). Lookback trims typical commit gaps, especially under translation, without resolving long post-switch stalls. Extending lookback to 3 decodes, Hold-n, fuzzy matching, and tolerating 1 differing word were tested and rejected (the latter two committed loops like "go go go go"). English-only runs are single-speaker, one run per setting.

## Fast speech

Measured 2026-09-21 on 5 minutes of the LibriVox reading time-stretched with `ffmpeg`'s pitch-preserving `atempo` filter at live-loop pacing (note that time-stretched audio does not replicate how natural fast speakers slur words):

| Speed | Words/window | Decode mean | Hop | Error rate | Dropped (decoded by Whisper) |
|---|---|---|---|---|---|
| 1x | 32 | 0.67s | 0.70s | 6.9% | 31 (12) |
| 1.25x | 41 | 0.78s | 0.80s | 6.9% | 22 (7) |
| 1.5x | 52 | 0.92s | 0.94s | 9.4% | 32 (15) |
| 1.75x | 55 | 0.95s | 0.97s | 19.0% | 105 (75) |

Effects at higher speed:

- **Higher decode lag:** More words per window means more autoregressive decoder steps; reducing lag requires a smaller `--window`.
- **Higher model error rate:** Misheard words roughly double by 1.5x.
- **Stall-induced drops at 1.75x:** Partial decodes or short hallucinations ("Oh, sorry!") break agreement, letting the confirmed tail roll out of the window. Adding the `_MIN_ANCHOR_COVERAGE` rule reduced 1.75x error rate from 19.0% to 16.3% without changing 1x–1.5x output.

**Capping repetition loops:** Across those four runs, six decodes looped ("a little bit more than a little bit more…") to Whisper's 448-token limit, stalling captions for 5.7–8.1s. `config.max_new_tokens` now caps output at `12 tokens/s × audio_s + 16` (the densest 1.75x real window was 8.5 tokens/s). Replaying two looping windows on the NPU dropped their stalls from 6.9s and 8.1s to 2.8s without affecting streaming. Multilingual mode multiplies this cap by `config.MULTILINGUAL_TOKEN_FACTOR` (1.25x), as French, German, and Spanish use more tokens per second than English (checked on synthetic speech peaking at 3.5–4.9 tokens/s, 2026-10-06).

**Rejected alternative (`whisper_streaming` variable buffer):** Prototyped 2026-09-21 using [whisper_streaming](https://arxiv.org/abs/2307.14743)'s sentence-trimmed buffer with `return_timestamps=True`. Although NPU word timestamps landed within 20 ms at p90, timestamp mode required a larger compiled decoder and ran 2.8x slower. With a 14s trim:

| Speed | Fixed 12s window | Variable buffer model |
|---|---|---|
| 1x | 6.9% | 9.1% |
| 1.25x | 6.9% | 8.0% |
| 1.5x | 9.4% | 8.6% |
| 1.75x | 19.0% | 12.8% |

Because the variable buffer averaged 8.5s of context (similar to the 8s window penalty), it degraded normal-speed accuracy by 1–2 percentage points and was not adopted.

## Language switches and the stall breaker

Measured 2026-10-06 on the NPU with multilingual `whisper-small` (`--language auto`) using two spliced LibriVox recordings (160s and 155s, 20s clips in French, German, Spanish, and English switching every 10–30s; decode time 0.57–0.65s per 12s window).

After a language switch, captions froze for 5–13s (4–5 stalls >5s per recording) while the 12s window still contained the previous language. An oracle that trimmed the window at exact switch boundaries reduced the longest transcribe stalls from 10.1s and 11.0s to 9.4s and 3.4s.

`caption.stall_window_s` implements a practical version: if no word commits for 4s (`STALL_AFTER_S`), the client shrinks requests to the last 6s (`STALL_WINDOW_S`) until a commit lands. Silence resets the stall timer so pauses do not shrink the next window.

| Transcribe run | Longest stall | Stalls > 5s | Difference vs. forced-language reference |
|---|---|---|---|
| Recording 1 (baseline) | 10.1s | 5 | 0.10 |
| Recording 1 (stall breaker) | 5.6s | 3 | 0.12 |
| Recording 2 (baseline) | 11.0s | 4 | 0.11 |
| Recording 2 (stall breaker) | 5.6s | 3 | 0.09 |

Mean commit gap stayed flat (1.4s → 1.3s on recording 2), and accuracy remained within ±0.02 noise. A more aggressive 3s/5s threshold removed all long stalls on recording 1 but hurt accuracy elsewhere.

**Disabled during translation:** In `--task translate` mode, the stall breaker did not improve stalls (3 → 4 on one recording) and degraded accuracy on the other (0.32 → 0.41 with the aggressive setting), because translation lag is caused by phrasing drift rather than lingering audio from the previous language.
