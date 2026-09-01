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
