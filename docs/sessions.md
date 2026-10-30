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
left alone: the sessions window offers to bundle and summarise them (backfill).
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
- **The shape is assembled here, not asked of the model.** Asked for the whole shape
  in one pass, SmolLM3 wrote its own headings and numbered lists, left the stamps
  out, and on a 16-minute session with two topics kept only the second (2026-10-30).
  So each chunk becomes bullet points (its answer is prefilled with `-`), the code
  picks key points spread across every chunk, and the title (prefilled with `#`) and
  the overview are written from those points in separate short passes.
- `repetition_penalty=1.1`: without it, greedy decoding looped on a long summary
  in #121, and on bullet points again in 2026-10-30's test.
- **The text is data.** It goes in between `<transcript>` (or `<notes>`) tags, with
  any closing tag inside it removed, and the prompt says never to obey it. The
  summary is only displayed and indexed, never typed, run or pasted.
- **Stamps come from the transcript.** Anything the model wrote that looks like a
  stamp is removed, and each point gets the stamp of the paragraph in its chunk that
  shares the most words with it.
- **Language** follows the session's: French captions get a French summary, and
  `translate` sessions get English ones.
- **Shape:** a `# title` of at most eight words, up to three sentences, at least eight
  key points where the session offers them (never fewer than one per chunk, so a long
  session's middle is kept), each ending in `[MM:SS]`, and a closing line saying it is
  machine-written and that the transcript is the record. The date, length and model
  are in the attestation.
- One failure is retried quietly on the next run. After three (`MAX_FAILURES`) the
  session stops being retried and the notification says so. A missing model stops
  the queue without counting a failure; the sessions window says to download it.

## The sessions window and `vinowhisper-session`

Saved sessions have a window of their own, `vinowhisper-gui --sessions`, the way
vinoSearch's search window is its own (`vinosearch-settings --search`). The settings
window's **Sessions** page keeps the summary setting and a button that opens it. It
lists every bundle (date, length, source, title or state). From there you can read the transcript or the summary (key points link to their stamp in the
transcript), summarise again, delete the summary, delete the session, show it in
the file manager, export it, copy the part on screen, or have vinoSpeak read it aloud. It drives `vinowhisper-session`:

```
vinowhisper-session list [--json]
vinowhisper-session show BUNDLE [--part transcript|summary|attestation]
vinowhisper-session export BUNDLE [--part both|summary|transcript] [--format md|txt|odt]
                              [-o PATH] [--force | --reuse] [--json] [--print-path]
vinowhisper-session summarise|delete-summary|delete|verify BUNDLE...
vinowhisper-session backfill
```

### Export

`export` writes the summary, the transcript or both (the default) to a file outside
the bundle. The logic is in the CLI so the window stays thin and anything else can use it.

| `--format` | What's in it |
|---|---|
| `md` (default) | YAML frontmatter from the attestation (`date`, `duration`, `source`, `language`, `model`, `summary_model`, `vinowhisper_session`, `tags`), a `# title`, then `## Summary` and `## Transcript` |
| `txt` | The same text with no markup: a `Label: value` header block, then `SUMMARY` and `TRANSCRIPT` |
| `odt` | An OpenDocument text file (headings, paragraphs, a bulleted list), written by hand with `zipfile` like the bundle; the facts are also in `meta.xml` as custom properties. Opens in LibreOffice, Word and Google Docs |

Without `--format` the format comes from `-o`'s extension, else `md`.

- **What leaves keeps its marks.** `[MM:SS]` stamps stay in every format, and the summary's
  "Machine-written summary" line is always kept. The transcript's header line is dropped:
  the frontmatter or header block says the same.
- **`-o`** is a file, or a directory to name it in; without it, `$XDG_DOCUMENTS_DIR/vinoWhisper/`
  (from `user-dirs.dirs`, `~/Documents` when that is unset or points at `$HOME`). The name is
  `<YYYY-MM-DD> <summary title>.<ext>` for the summary, with ` (transcript)` or
  ` (summary and transcript)` after it for the other parts, and `<YYYY-MM-DD> Transcript.<ext>`
  without a summary. Characters a file system or a file manager could trip on (`/`, `\`, `:`,
  `*`, `?`, `"`, `<`, `>`, `|`, control and bidi characters) become spaces, and it is capped at
  200 bytes. `--print-path` prints where it would write and writes nothing.
- **Nothing is replaced without `--force`.** Into a directory a taken name gets ` (2)`, ` (3)`;
  a named file that exists is an error. The file is written to a temp file and renamed (a hard
  link when not forcing, so two exports cannot race onto one name), mode 0644: it leaves the
  private store on purpose.
- `--part summary` on a session with no summary is an error; `--part both` exports the
  transcript and says so (`"summary_missing": true` with `--json`).
- **Export is read-only**: nothing is recorded in the bundle or `sessions.json`.
- **`--reuse`** (into a directory) keeps an earlier export of this session instead of
  numbering a new one: `"state": "unchanged"` when its bytes are what export would write
  now, `"edited"` when they differ. Which session a file came from is read from its
  `vinowhisper_session` frontmatter key, `Session:` line or `meta.xml` property (LibreOffice
  keeps that on save, checked 2026-10-29), so another session's file with the same name is
  never taken; it gets a ` (2)` of its own.
- `--json` prints `{"path", "parts", "summary_missing", "state"}` for the sessions window.

Not supported: SRT/VTT (the bundle has one start stamp per paragraph, no end or word times,
so cues would be guesses; it needs segment times in a `vinowhisper-session/2`), DOCX (ODT
opens in Word), and exporting many sessions at once.

In the window, **Export…** is in each card's More menu and in the viewer's header. It asks
for the part (in the viewer it starts on the tab you're reading) and the format, then
opens the desktop's save dialog through the FileChooser portal. Without a portal it saves into
`$XDG_DOCUMENTS_DIR/vinoWhisper/` and says where, with a Show in folder button. **Copy** in
the viewer's header puts the part on screen on the clipboard, as `vinowhisper-session show`
prints it.

### Open in…

The viewer's header has an **Open in…** split button: the main half opens the part on screen
in the app picked last ("Open in word processor" to start with), the arrow picks the other
one. The card's More menu has both (the summary, or the transcript when there is none).

| Entry | Exports |
|---|---|
| Word processor | `odt` |
| Text editor | `md` |

1. It exports with `--reuse` into `$XDG_DOCUMENTS_DIR/vinoWhisper/`, a kept file rather than a
   temp file, because people edit it. A copy that differs from the session (edited, or
   summarised since) is opened as it is, never written over, and the notice says so;
   **Export…** writes a fresh one.
2. It opens the file through the OpenURI portal with `ask` set, so GNOME and KDE show their
   own app chooser, Flatpak apps work, and the desktop remembers the pick. With no portal it
   runs `xdg-open`, and the MIME defaults decide.

There is no app list of our own. The last entry picked is `open_in` in `gui.json`
(`"word_processor"` or `"text_editor"`). Obsidian wants the file inside a vault, which this
doesn't do; **Export…** to the vault's folder does.

Opening a session from elsewhere (vinoSearch, a notification, a file manager):

```
vinowhisper-gui --open <bundle>[#transcript[@MM:SS]|#summary]
vinowhisper-gui <bundle>.vwsession
```

### Read aloud

When `vinospeak` is on `PATH` (looked up once, when the window opens), the viewer's tab row
has **Read aloud** and each card's More menu has the same. It runs
`vinospeak --file BUNDLE --part summary|transcript` (karanshukla/vinoSpeak#42): the tab being
read in the viewer, the summary (or the transcript, without one) from a card. Without
vinoSpeak there is no button and no prompt to install it.

- While it reads the button is **Stop**, which sends SIGTERM to vinoSpeak's process group, so
  whatever it plays through stops too. Going back to all sessions, opening another session or
  closing the window stops it as well, and `PR_SET_PDEATHSIG` ends it if the window crashes.
  One reading at a time.
- If vinoSpeak exits non-zero, its last line on stderr is shown. An older vinoSpeak that
  refuses `--part` gets "this vinoSpeak can't read sessions yet. Update vinoSpeak to read them."

`--page sessions --open <bundle>` is still read and opens the same window: vinoSearch
and notifications posted by older trays send it. `--page sessions` alone is the settings
page.

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

## Measured on the laptop, 2026-10-30

Wildcat Lake NPU, SmolLM3-3B, version 1.1.1 plus the assembled shape. The long
transcripts are a LibriVox reading of Pride and Prejudice decoded through the
server in 12 s windows, so n=1 per length and not the user's usual content.

| Session | Chunks | Total | Per chunk |
|---|---|---|---|
| 13 s (real) | 1 | 12 s | |
| 35 s (real) | 1 | 17 s | |
| 30 min | 4 | 74 s | 16 s |
| 60 min | 8 | 144 s | 16 s |

Every chunk of about 7,000 characters fills `CHUNK_TOKENS` at 16 tokens/s, so its
last lines are dropped and the end of a chunk is covered less than its start.

- **Faithful in topic, not in every detail.** Stamps pointed at the right paragraph
  in all four checked. The points are not all right: of nine on the 60-minute
  session, three name the wrong person or misread the line, and a real 16-minute
  review gained an "Apple Watch" its transcript never mentions. The footer's
  "check it at the time stamps" is needed.
- **Sharing the NPU:** Whisper resident, SmolLM3 summarising and vinoSearch queries
  (one during that run, eleven during another, no failures) gave no
  `Failed to register command queue: -16`, or any other `intel_vpu` line, in the
  kernel log.
- **Memory:** `drm-resident-memory` peaked at 2.2 GB summarising, 2.4 GB with Whisper
  in the same server, and fell to 86 MiB (Whisper) as soon as the queue was empty.
- **Captions interrupting a summary do not stutter:** the first 12 s decode took
  0.77 s against 0.65-0.96 s beforehand, and seven more took 0.62-0.79 s. The session
  restarted from its first chunk once the 20 s of quiet had passed.
- **On screen:** the "Summary ready" notification is sent with its title and an Open
  action (seen on the session bus). The sessions window is right in light and dark.
  A window that is open when the desktop switches theme follows it on its next focus.
  `.vwsession` files have their icon in Dolphin.

Not checked: clicking Open on the notification.
