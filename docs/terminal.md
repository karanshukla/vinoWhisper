# Terminal captions

`vinowhisper-caption` prints confirmed words directly into your terminal's scrollback buffer and redraws only the status bar at the bottom. Confirmed text survives quitting and works with native terminal search, selection, and piping.

The UI uses `rich.live.Live` rather than a full-screen TUI framework like Textual because confirmed words are append-only: only the single bottom status line needs live redraws, while full-screen frameworks capture scrollback and break piping.

## Display behavior

- **Paragraph breaks:** Paragraphs are inferred automatically (`paragraphs.ParagraphBreaker`, shared with the status bar). A pause well above normal inter-sentence spacing starts a new paragraph, as does a sentence end once a paragraph reaches roughly a screenful. Common abbreviations and initialisms ("U.S.", "F.B.I.") are skipped; a missed break simply waits for the next sentence boundary.
- **Level meter:** Spans -60 to 0 dBFS. The 0.002 RMS silence gate sits at roughly -54 dBFS.
- **Terminal resizing:** Lines already printed into scrollback keep their original wrap width rather than rewrapping on resize.
- **Plain and piped output:** `--plain`, `--debug`, and a piped `stdout` omit the status bar so output can be redirected cleanly to a file.

## Saving transcripts (`--transcript`)

Added 2026-10-07 (issue #36). `--transcript [PATH]` is off by default and works with every output mode, including `--json`.

- **Default directory:** With no `PATH`, files are written to `$XDG_DATA_HOME/vinowhisper/transcripts/`.
- **Directory vs. file:** When given a directory (or a nonexistent path with no file extension), each session writes to a new local-time `YYYY-MM-DD-HHMMSS.txt` file, which is converted into a `YYYY-MM-DD-HHMMSS.vwsession` bundle and summarised in the background when the session ends (see [sessions.md](sessions.md)). When given a file path, new sessions append to it under a new header.
- **Format:** One unwrapped line per paragraph, preceded by a session header:

  ```
  vinoWhisper transcript, 2026-09-29 14:02, system audio, NPU

  [00:00] First paragraph of confirmed words ...

  [01:12] Next paragraph after a real pause ...
  ```

- **Timestamps and flushing:** Each `[MM:SS]` timestamp records the elapsed time since the model became ready when the paragraph's first word was confirmed (roughly two cycles behind live speech). Only confirmed and flushed words are written (never pending dimmed text), and output is flushed after every cycle. The file is created on the first confirmed word, so silent sessions create no file.
- **Shutdown and permissions:** `Ctrl+C` and hiding the overlay (`SIGINT`) flush pending tail words; `SIGTERM` or `SIGKILL` loses the couple of seconds still pending. Created files use mode `0600` inside `0700` directories. Transcripts are never deleted automatically (~1 KB per minute of speech). Dictation text is never written to transcript files.

## Calls: `--source both` (issue #118, added 2026-11-22)

`--source both` captions the microphone and the system audio at once, as two
lanes: `you` (mic) and `them` (what is playing). Each lane has its own capture,
speech detector and stitcher, since LocalAgreement-2 works per stream. The
server runs one transcription at a time (`transcriber.py` holds a lock), so the
loop does the same: the lane with more unheard audio goes first, and a silent
lane costs nothing.

- Every `Cycle` and `Silence` event carries a `lane` field (`""` for one
  source), and `Flushed(lane, words)` carries each lane's held-back words
  before `Stopped`.
- The terminal, status bar, overlay and transcript start a labelled paragraph
  when the speaker changes (`[01:12] you: ...`). The `you` lane going quiet is
  never reported as "no signal".
- `--record DIR` writes `audio-you.wav` and `audio-them.wav` next to one
  `events.jsonl`. `vinowhisper-replay --restitch` redoes each lane;
  `--sweep` needs `--lane you|them`.
- Not decided here: the mic hears the speakers, so without headphones their
  words appear in both lanes. Echo cancellation is #216.
- Whether to save transcripts by default in this mode is left to the existing
  `save_transcripts` setting.
