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
- **Repeats collapse in units of at most about five words**, the longest loop
  seen being "do things that make you" (2026-09-01). It errs short, because a
  false positive deletes words that can never be restored.
