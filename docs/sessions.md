# Saved sessions and their summaries

Issue #123, built 2026-10-28. Recording stays opt-in: only sessions captioned with
**Save transcripts** on (or `vinowhisper-caption --transcript` into a directory)
are kept, and only those are summarised.

## What happens after a session

1. While captions run, the transcript is written live to
   `~/.local/share/vinowhisper/transcripts/YYYY-MM-DD-HHMMSS.txt`, as before
   ([terminal.md](terminal.md)). The file holds an `flock` until the session ends.
2. When it ends, `vinowhisper-caption` turns it into
   `YYYY-MM-DD-HHMMSS.vwsession` (below) and removes the `.txt`. A transcript
   written to a named file (`--transcript notes.txt`, appended to across
   sessions) is never bundled.
3. It then asks the server (`POST /summaries`) to summarise. The server
   summarises in the background, newest session first, and only once no
   caption or dictation request has reached it for 20 s (`summary.QUIET_S`). If
   one arrives mid-summary, the model is cancelled at its next token and that
   chunk runs again later, so captions and dictation never wait behind a
   summary. The server does not idle-exit while summaries are queued.
4. The summary is written into the bundle (temp file, then rename), the model
   is unloaded as soon as the queue is empty, and the tray posts one desktop
   notification for everything that finished together. "Open" goes to the
   summary in the settings window.

If the session never ended (crash, power loss), the next server start bundles
the leftover `.txt` and summarises it. Transcripts saved before this feature are
left alone: the Sessions page offers to bundle and summarise them (backfill).
What counts as "before" is the first time the summariser ran, recorded in
`~/.local/state/vinowhisper/sessions.json`, along with failure counts and the
sessions whose summary was deleted on purpose (they are not summarised again).

**Summarise saved sessions automatically** (settings window, Sessions page,
`~/.config/vinowhisper/summaries.json`, `{"auto": true}` by default) pauses the
automatic runs. Summarising one session by hand still works when it is off.

## The bundle: `.vwsession`

A zip with a fixed layout, like `.odt` and `.epub`:

```
mimetype           application/vnd.vinowhisper.session+zip   first entry, stored, no extra field
transcript.txt     the transcript exactly as captured, header line included
summary.md         absent until summarised
attestation.json   provenance
```

Entry times are fixed (1980-01-01), so the bytes depend only on the content. Bundles
are 0600, and temp files are `.<name>.<random>.tmp`, never ending in `.vwsession`.

vinoSearch reads these (karanshukla/vinoSearch#143, #145) and depends on:
mimetype stored and first; `format == "vinowhisper-session/1"` (it refuses anything
else); entries found by name; `session.started` RFC 3339 with an offset;
`session.duration_s` an integer; `files["summary.md"].made_by == "ai-generated-summary"`;
paragraph stamps matching `^\[(\d{2,}):(\d{2})\] ` (minutes keep counting past an
hour: `[61:05]`). `tests/test_bundle.py` pins all of this, and
`tests/fixtures/session_v1*.vwsession` are the fixtures vinoSearch copies
(`VINOWHISPER_BLESS_FIXTURES=1` regenerates them). Change the layout only with a
new `format`.

### Attestation

```json
{
  "format": "vinowhisper-session/1",
  "generator": {"name": "vinoWhisper", "version": "1.1.0"},
  "session": {"started": "2026-10-27T14:05:12-04:00", "duration_s": 2530,
              "source": "system audio", "language": "en", "task": "transcribe"},
  "files": {
    "transcript.txt": {"made_by": "speech-to-text", "model": "openai/whisper-small.en",
      "revision": "<40-char commit>", "device": "NPU", "sha256": "…"},
    "summary.md": {"made_by": "ai-generated-summary", "model": "HuggingFaceTB/SmolLM3-3B",
      "export": "arankays/SmolLM3-3B-int4-cw-ov@<40-char commit>", "device": "NPU",
      "generated": "2026-10-27T14:48:03-04:00", "sha256": "…"}
  }
}
```

It says how each file was made, in C2PA's spirit (the summary is marked
AI-generated), and its hashes show an edit made afterwards
(`vinowhisper-session verify`). It is unsigned: anyone can write the same JSON, so
it is provenance, not proof. Bundled leftovers and backfilled transcripts carry
`null` where something isn't known (language, model revision).

## How a summary is made

`vinowhisper/summary.py`, SmolLM3-3B in the server, the model dictation cleanup
uses ([tray.md](tray.md), "LLM cleanup").

- **Chunked at `[MM:SS]` paragraphs**, about 7,000 characters each
  (`CHUNK_CHARS`), which leaves room for the prompt inside `MAX_PROMPT_LEN=3072`.
  A paragraph longer than that is split at word boundaries and keeps its stamp.
- One chunk: one pass straight to the final shape. More: each chunk becomes two to
  six stamped notes, then one pass turns the notes into the summary. Notes too
  long for one pass are condensed first.
- `repetition_penalty=1.1`: without it, greedy decoding looped on a long summary
  in #121.
- **The text is data.** It goes in between `<transcript>` (or `<notes>`) tags, with
  any closing tag inside it removed, and the prompt says never to obey it. The
  summary is only displayed and indexed, never typed, run or pasted.
- **Stamps are checked.** A stamp the model wrote that isn't in the transcript is
  moved to the paragraph it falls in, or dropped when it is past the end.
- **Language** follows the session's: French captions get a French summary, and
  `translate` sessions get English ones.
- **Shape:** a `# title`, two or three sentences, three to eight key points ending
  in `[MM:SS]`, and a closing line saying it is machine-written and that the
  transcript is the record. The date, length and model are in the attestation.
- One failure is retried quietly on the next run. After three (`MAX_FAILURES`) the
  session stops being retried and the notification says so. A missing model stops
  the queue without counting a failure; the Sessions page says to download it.

## The Sessions page and `vinowhisper-session`

The page lists every bundle (date, length, source, title or state). From there you
can read the transcript or the summary (key points link to their stamp in the
transcript), summarise again, delete the summary, delete the session, or show it in
the file manager. It drives `vinowhisper-session`:

```
vinowhisper-session list [--json]
vinowhisper-session show BUNDLE [--part transcript|summary|attestation]
vinowhisper-session summarise|delete-summary|delete|verify BUNDLE...
vinowhisper-session backfill
```

Opening a session from elsewhere (vinoSearch, a notification, a file manager):

```
vinowhisper-gui --page sessions --open <bundle>[#transcript[@MM:SS]|#summary]
vinowhisper-gui <bundle>.vwsession
```

The fragment is split off at the last `#` only when it is one of those three forms,
so a path containing `#` still opens. `@MM:SS` scrolls to the paragraph holding that
time and highlights it. The text is read by `vinowhisper-session show` and shown in
the window, so nothing is extracted to a temp file.

## File type

On its first start the tray writes
`~/.local/share/mime/packages/io.github.karanshukla.vinowhisper-session.xml`
(`*.vwsession`, plus magic on the `mimetype` entry at offsets 30 and 38, icon
`io.github.karanshukla.vinowhisper`) and adds
`MimeType=application/vnd.vinowhisper.session+zip;` and `%f` to the settings
window's desktop entry, then runs `update-mime-database` and
`update-desktop-database`. `--export-desktop` writes the same XML for packagers.

## Not yet measured on the laptop

Everything above is tested with a fake model and a scratch directory. Still to do
on the NPU and Plasma:

- [ ] Real 30- and 60-minute transcripts: time per chunk and in total, and a read
      for faithfulness.
- [ ] Whisper, SmolLM3 and vinoSearch loaded on the NPU together, with no
      `Failed to register command queue: -16` in the kernel log.
- [ ] Memory while summarising and after the unload (`drm-resident-memory` in the
      server's accel fdinfo).
- [ ] The notification on screen, with "Open" landing on the summary.
- [ ] The Sessions page on screen, light and dark.
- [ ] A `.vwsession` with its icon in Dolphin, opening the settings window.
- [ ] Whether cancelling SmolLM3 at a token boundary frees the NPU quickly enough
      that captions starting mid-summary don't stutter.
