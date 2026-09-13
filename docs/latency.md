# Latency

Two halves of one mechanism: cycle time sets the lag, and the commit
policy that hides the model's indecision is what doubles it.

## Cycle time, the one knob that matters

The caption loop is synchronous, so the hop between windows is just however
long the previous cycle took, and the commit policy needs two cycles to agree
before printing anything. Captions therefore trail the audio by roughly twice
the cycle time. Cycle time is the only real lever.

Whisper's encoder cost is fixed (it pads to 30s no matter what), but decoding
is autoregressive, one forward pass per token. A window packed with 29.5s of
dense speech emits roughly 2.5x the tokens of a 12s one and takes
correspondingly longer. That is why the default window is now 12s rather than
the full 29.5s short-form limit:

```
vinowhisper-caption --window 8      # snappier, less context, more wording drift
vinowhisper-caption --window 20     # steadier wording, noticeably laggier
```

Measured 2026-09-12 by sliding a 12s window over recorded speech at the
loop's own pacing, against the live server: 0.66s mean decode on synthetic
speech (28 words a window), 0.70s mean and 0.85s p90 on a LibriVox reading
(32 words a window, 806 cycles over ten minutes, no drift in either number
across the session). So the hop sits at 0.7s and the lag floor at about 1.5s.

The same reading at three window sizes, same day, each a full ten-minute pass
at the loop's own pacing, scored against the Gutenberg text (n=1, one voice,
one laptop):

| window | cycles | decode mean | p90 | hop | lag floor | wrong or extra words | dropped |
|---|---|---|---|---|---|---|---|
| 8s | 1044 | 0.53s | 0.61s | 0.57s | ~1.1s | 102 | ~28 |
| 12s | 806 | 0.70s | 0.85s | 0.74s | ~1.4s | 93 | ~38 |
| 16s | 665 | 0.87s | 1.06s | 0.90s | ~1.7s | 93 | ~24 |

Decode time tracks the window because the decoder is autoregressive, but the
encoder's fixed cost keeps the spread to a third of a second across the whole
range. Accuracy is flat from 12s up and about 10% worse at 8s, which is the
edge effect: a shorter window has more cycles where a phrase is cut
mid-sentence. 12s stays the default; 8s is a fair trade if 0.3s matters more
than a handful of words an hour, and 16s buys nothing measurable.

`--debug` prints the numbers to tune against: window length, hop, RMS, gain
applied, time to first streamed piece, total cycle time, and how many words
each cycle confirmed versus held pending.

Nothing is transcribed until the buffer holds 1.5s (`config.MIN_WINDOW_S`),
because Whisper hallucinates freely on sub-second clips. On exit, words that
agreed once but never got their confirming cycle are printed rather than
dropped.

`vinowhisper-replay --sweep` measures the tradeoff on your own audio instead of
guessing at it. The est. lag column is `2 x mean`, which is the floor the
two-cycle commit policy imposes:

```
| window | decodes | mean | p90 | first piece | words/decode | est. lag |
```

## Why the captions reword themselves

Each cycle re-transcribes a window that mostly overlaps the last one, and
Whisper does not decode the same audio the same way twice. Real testing on
2026-08-03 caught it producing "Ex-sherzer", "you're told", and "you know
Dalton" for the same underlying audio across three consecutive cycles. That is
not paraphrasing near a boundary, the words genuinely are not the same until
the model has enough context to settle.

`stitch.py` handles this with a LocalAgreement-2 commit policy: a word only
prints once two consecutive cycles agree on it. That also means a hallucinated
guess on near-silence never reaches the screen, because the next cycle guesses
something else. The cost is the two-cycle latency described above.

The obvious remaining fix, priming each decode with the prior transcript via
`initial_prompt`, is hard-blocked on this device: `RuntimeError:
'initial_prompt' parameter is not supported on NPU device`. Some cross-window
wording drift is an accepted limitation until that changes.

## How the stitcher finds the overlap

The overlap between the confirmed words and each new decode is found by
matching words, and each rule below came from a real failure:

- **It searches the last 40 confirmed words, not the whole transcript**,
  because speech repeats short phrases and a global match can lock onto an
  earlier occurrence. It was 15 until 2026-08-03, when testing against
  YouTube's captions showed restatement at cycle boundaries ("was with the
  best pitcher in baseball. was with the best pitcher in baseball for the
  first month.") and cycles slow enough that the true overlap fell outside 15
  words.
- **Words compare without case or punctuation.** The same audio came back as
  "world series last year." and "World Series last year." two cycles apart
  (2026-08-03); an exact comparison broke the anchor and reprinted the
  transcript. Drifting punctuation did the same on 2026-08-07.
- **The match that reaches furthest wins, not the longest**, among those long
  enough to trust. On 2026-08-03 a hallucinated word ("acquire Hose Jose
  Soriano") split a match, and the longer block, ending 8 words earlier,
  reprinted a sentence.
- **The minimum match shrinks for short decodes.** A flat three words cannot be
  met by a one-word transcript, which let a repeated hallucination ("you" 26
  times on quiet audio) reprint every cycle.
- **Confirmed words that the new decode spells differently are skipped, not
  reprinted.** With a 0.7s hop the freshest confirmed word is about a second
  from the window's edge, where Whisper is still making its mind up. Measured
  2026-09-12: the trailing words flipped between "Whiskers", "Whispers" and
  "Whisper's" for five cycles, two near-identical windows agreed on
  "Whispers", it printed, and later decodes settled on "Whisper's". The anchor
  could not match the settled form against the printed one, so the cut landed
  a word early and the settled form printed again ("Whispers Whisper's
  encoder"). Now the confirmed words past the anchor match are compared
  against the head of the new text as joined strings, and a resemblance
  (`_REDECODE_RATIO`) skips them. Joined, so "auto-aggressive. One" against
  "auto regressive one" skips three words for two. On a ten-minute LibriVox
  reading this took insertions from 60 to 43 and left no reprints, only
  words the reader or the model added; on synthetic speech, from 8 to 3.
- **The boundary check compares the confirmed tail against the head of the
  new text at every overlap length**, longest first. The 2026-09-01 version
  compared position for position from one window's worth back, which only
  matched while the whole transcript still fit in one window: it worked for
  the first minute of a session and silently stopped.
- **A lost anchor realigns on the pending words instead of stalling.** When
  a stall runs long enough for the confirmed tail to roll out of the window,
  the prefix comparison between pending and the new text has nothing to line
  them up, and the old code stayed stuck until a spurious match, then dropped
  everything before it. Measured 2026-09-12 by forcing long stalls (four
  cycles of agreement): 75 of 141 words lost without the realignment, 28
  with it. Live, the same stalls come from music, noise and speaker changes.
- **Two cycles of agreement stays.** Three was tried on the same recording:
  15 fewer insertions, 31 more dropped words, and pending words held twice
  as long. Not worth it.
- **Repeats collapse in units of at most about five words**, the longest loop
  seen being "do things that make you" (2026-09-01). It errs short, because a
  false positive deletes words that can never be restored.
