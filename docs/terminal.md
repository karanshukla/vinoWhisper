# The terminal captions

`vinowhisper-caption` prints confirmed words into the terminal's own
scrollback, so the transcript survives quitting and native search and
selection work on it. Only the status bar at the bottom redraws.

That is why it uses Rich and not Textual. A word is never revised once
printed, so the transcript belongs in scrollback and only one line needs a
live region, which is `rich.live.Live`. Textual owns the screen, so the
transcript would live in a widget that is gone on quit and cannot be piped.

- **Paragraphs are inferred.** A pause well above a normal gap between
  sentences starts a new paragraph, and so does a sentence end once a
  paragraph is about a screenful long. Sentence ends skip common abbreviations
  and initialisms ("U.S.", "F.B.I."); a miss only delays the break to the next
  sentence.
- **`--transcript [PATH]` saves the words to a file as well** (issue #36,
  added 2026-10-07), whichever renderer is showing them, `--json` included.
  It is off unless asked for, because it writes whatever was playing to disk.
  With no PATH it writes to `$XDG_DATA_HOME/vinowhisper/transcripts/`. A
  directory gets a new `YYYY-MM-DD-HHMMSS.txt` per session (local time); a path
  that is not a directory is a file and is appended to, with a new header each
  session. A path that does not exist yet and has no extension is taken as a
  directory.

  ```
  vinoWhisper transcript, 2026-09-29 14:02, system audio, NPU

  [00:00] First paragraph of confirmed words ...

  [01:12] Next paragraph after a real pause ...
  ```

  One line per paragraph, no hard wrapping. Paragraphs break by the rules
  above (`paragraphs.ParagraphBreaker`, shared with the status bar). The stamp is
  the time since the model was ready, when the paragraph's first word was
  confirmed, which runs about two cycles behind the speech. Only confirmed and
  flushed words are written, never the dimmed pending ones, so the file is
  append-only for the same reason scrollback is. It is flushed after every
  cycle. The file is created at the first word, so a session with no speech
  leaves nothing behind. Ctrl+C and hiding the overlay (SIGINT) flush the
  tail; a SIGTERM or a kill loses what was still pending, a couple of seconds.
  Files are 0600 in a 0700 directory it creates. Nothing deletes them: a
  minute of speech is about 1KB. Dictation is not written.
- **The level meter spans -60 to 0 dBFS.** The silence gate, 0.002 rms, sits
  at about -54 dBFS.
- **Resizing does not rewrap old lines.** Lines already in scrollback keep the
  width they were printed at, the standing cost of using scrollback rather
  than a widget.
- **`--plain`, `--debug` and a piped stdout** print plain lines without the
  status bar, so redirecting to a file works.
