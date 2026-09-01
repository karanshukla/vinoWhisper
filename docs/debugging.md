# Debugging without the hardware in front of you

`--record DIR` writes `audio.wav` (16kHz mono, opens in Audacity) plus
`events.jsonl`, one JSON object per cycle with every number `--debug` prints
and the raw pre-stitch transcript. Roughly 2MB per minute.

That turns "it was laggy while I watched a video" into a fixture:

- `vinowhisper-replay DIR --restitch` feeds the recorded transcripts back
  through the stitcher with no NPU, no server and no audio. The model output
  is frozen, so any difference in what gets printed is your change and nothing
  else. It diffs against what the original run printed and points at the first
  divergence.
- `vinowhisper-replay DIR --sweep 8,12,16,20` needs the NPU and slices the
  recorded audio at a fixed hop, so every window size sees the same decodes
  over the same audio.

`vinowhisper-doctor` checks the environmental things: OpenVINO's device list,
which device would be selected, whether each model export matches the device
that needs it, server reachability, the capture backend, default sink, mute
state, `monitor.channel-volumes`, and a two-second live level probe on the sink
monitor and on each playing app. Run it once with audio playing normally and
once with the system muted. If the sink monitor drops to silence while an app
stream stays audible, that is the mute problem confirmed by measurement, and it
says so.
