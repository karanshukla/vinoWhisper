# Debugging

vinoWhisper includes recording, offline replay, and environment diagnostic tools so stitcher bugs, window-size tradeoffs, and audio issues can be investigated without reproducing live playback.

## Recording and offline replay

Passing `--record DIR` to `vinowhisper-caption` writes two files (~2 MB per minute):

- `audio.wav`: 16 kHz mono 16-bit integer PCM (can be opened directly in Audacity). The Python `wave` module writes integer PCM, which still provides roughly 65 amplitude steps around the ~0.002 RMS silence floor.
- `events.jsonl`: one JSON object per cycle containing all `--debug` metrics and the raw pre-stitch transcript. Each event is flushed immediately, so interrupting a session with `Ctrl+C` leaves a complete log.

Replay tools turn a recorded session into a reproducible test fixture:

- `vinowhisper-replay DIR --restitch`: feeds recorded transcripts back through the stitcher without an NPU, server, or audio stack. Because model outputs are frozen, any output diff isolates stitcher changes and points to the first divergence.
- `vinowhisper-replay DIR --sweep 8,12,16,20`: requires the NPU and re-slices the recorded audio at a fixed hop so different window sizes are compared on identical audio.

`tests/fixtures/espeak_12s_session.jsonl` contains 77 real NPU decodes of a 12s sliding window over `espeak-ng` reading `espeak_12s_session.txt` at live loop pacing (recorded 2026-09-12). `test_a_real_session_prints_no_phrase_twice` restitches this fixture to catch regressions against real model drift. To build a longer fixture, slice the trailing 12s from a 16 kHz WAV at the current position, send it to the running server, pass the output through `Stitcher`, write a `Cycle` event, and advance the position by the measured decode time.

## Environment diagnostics (`vinowhisper-doctor`)

`vinowhisper-doctor` checks local runtime prerequisites in one pass:

- OpenVINO device enumeration and selected device
- Model export compatibility with the selected device and digest verification
- Transcription server reachability
- Audio capture backend, default sink, mute state, and `monitor.channel-volumes`
- A 2-second live level probe on the default sink monitor and each playing application stream

To verify mute behavior on your system, run `vinowhisper-doctor` once while audio is playing normally and once with system audio muted. If the sink monitor drops to silence while an application stream stays audible, the doctor reports that directly.
