# The terminal captions

`vinowhisper-caption` prints confirmed words into the terminal's own
scrollback, so the transcript survives quitting and native search and
selection work on it. Only the status bar at the bottom redraws.

- **Paragraphs are inferred.** A pause well above a normal gap between
  sentences starts a new paragraph, and so does a sentence end once a
  paragraph is about a screenful long. Sentence ends skip common abbreviations
  and initialisms ("U.S.", "F.B.I."); a miss only delays the break to the next
  sentence.
- **The level meter spans -60 to 0 dBFS.** The silence gate, 0.002 rms, sits
  at about -54 dBFS.
- **Resizing does not rewrap old lines.** Lines already in scrollback keep the
  width they were printed at, the standing cost of using scrollback rather
  than a widget.
- **`--plain`, `--debug` and a piped stdout** print plain lines without the
  status bar, so redirecting to a file works.
