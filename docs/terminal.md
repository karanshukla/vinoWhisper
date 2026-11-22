# The terminal captions

`vinowhisper-caption` prints confirmed words directly into the terminal's scrollback buffer, so the transcript persists after you quit and works with native terminal search and text selection. Only the status bar at the bottom redraws in place.

This is why vinoWhisper uses Rich rather than Textual. Confirmed words are never revised once printed, so only the single status line needs a live region (`rich.live.Live`). A full-screen TUI framework like Textual would trap the transcript inside a widget that disappears on exit and cannot be piped cleanly.

- **Paragraphs are inferred automatically.** A pause longer than a normal inter-sentence gap starts a new paragraph, as does a sentence ending once a paragraph reaches roughly a screenful of text. Sentence detection skips common abbreviations and initialisms ("U.S.", "F.B.I."); any missed abbreviation simply delays the paragraph break until the next sentence.
- **`--transcript [PATH]` saves confirmed text to disk** (issue #36, added 2026-10-07), regardless of which renderer is active (including `--json`). It is disabled by default because it records whatever audio is playing. Omitting `PATH` writes to `$XDG_DATA_HOME/vinowhisper/transcripts/`. When given a directory, each session creates a new `YYYY-MM-DD-HHMMSS.txt` file (local time) that is bundled into `YYYY-MM-DD-HHMMSS.vwsession` at session end and summarised in the background ([sessions.md](sessions.md)). When given a file path, transcripts are appended with a new session header. A nonexistent path with no file extension is treated as a directory.

  ```
  vinoWhisper transcript, 2026-09-29 14:02, system audio, NPU

  [00:00] First paragraph of confirmed words ...

  [01:12] Next paragraph after a real pause ...
  ```

  Each paragraph is written on one line without hard wrapping, using the same break rules (`paragraphs.ParagraphBreaker`) as the status bar. Timestamps measure elapsed time since the model became ready when the paragraph's first word was confirmed (about two decode cycles behind speech). Only confirmed and flushed words are written (never dimmed pending words), so the file is append-only and flushed after every cycle. The file is created only when the first word arrives, so silent sessions create no file. `Ctrl+C` and hiding the overlay (`SIGINT`) flush pending words; `SIGTERM` or `SIGKILL` drops the last few seconds of pending text. Files are created with mode `0600` inside `0700` directories and are not automatically deleted (one minute of speech is roughly 1 KB). Dictation is never written to transcript files.
- **The level meter spans -60 to 0 dBFS.** The `0.002` RMS silence gate corresponds to roughly -54 dBFS.
- **Resizing does not rewrap previous lines.** Lines already printed to scrollback retain the width they were wrapped at, which is an inherent trade-off of writing to terminal scrollback instead of a TUI widget.
- **`--plain`, `--debug`, and piped stdout** emit plain lines without the status bar so output can be redirected to files or pipes.

## Calls: `--source both` (issue #118, added 2026-11-22)

`--source both` captions the microphone and system audio simultaneously in two lanes: `you` (microphone) and `them` (system audio). Each lane has its own capture stream, speech detector, and stitcher, since LocalAgreement-2 operates per stream. Because the server processes one transcription at a time (`transcriber.py` holds a lock), the caption loop schedules whichever lane has more un-transcribed audio first; an inactive lane incurs no decode cost.

- Every `Cycle` and `Silence` event includes a `lane` field (`""` for single-source sessions), and `Flushed(lane, words)` emits each lane's held-back words before `Stopped`.
- Both the terminal and saved transcripts start a labeled paragraph whenever the speaker changes (`[01:12] you: ...`). The overlay (2026-11-22) stacks the two lanes instead: `them` above `you`, two lines each, with grey pending words disabled (because decoding alternates between lanes, a pending tail would stay visible and keep shifting). The overlay expands to four lines on the first two-lane event, and silence on the `you` lane is never reported as "no signal".
- `--record DIR` writes `audio-you.wav` and `audio-them.wav` alongside a single `events.jsonl`. `vinowhisper-replay --restitch` processes each lane independently, while `--sweep` requires `--lane you|them`.
- When using speakers without headphones, the microphone picks up playback audio and remote speech appears in both lanes. See [echo.md](echo.md) (#216) for echo cancellation.
- Transcript saving in two-lane mode follows the existing `save_transcripts` setting.
