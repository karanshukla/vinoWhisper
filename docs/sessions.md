# Saved sessions and summaries

Added 2026-10-28 (issue #123). Recording is opt-in: only caption sessions run with **Save transcripts** enabled (or `vinowhisper-caption --transcript` pointing to a directory) are bundled and summarised.

## Session lifecycle

1. **Live capture:** Words are written to `~/.local/share/vinowhisper/transcripts/YYYY-MM-DD-HHMMSS.txt` ([terminal.md](terminal.md)) while holding an `flock` lock.
2. **Bundling:** When the session ends, `vinowhisper-caption` packages the `.txt` into `YYYY-MM-DD-HHMMSS.vwsession` and removes the raw `.txt`. Transcripts appended to an explicit file path (`--transcript notes.txt`) are never bundled.
3. **Background summarisation:** The client sends `POST /summaries` to the server, which processes queued sessions newest-first after 20s without any caption or dictation requests (`summary.QUIET_S`). If a live caption or dictation request arrives mid-summary, generation cancels on the next token and resumes that chunk later. The server stays alive while summaries are queued.
4. **Completion:** The summary is written atomically (temp file + rename), the summarisation model unloads as soon as the queue empties, and the tray posts one desktop notification for the batch with an **Open** action leading to the summary in the sessions window.

Leftover `.txt` files from crashes or power loss are bundled and summarised on the next server start. Older transcripts saved before this feature first ran (tracked in `~/.local/state/vinowhisper/sessions.json`, alongside retry counts and intentionally deleted summaries) are left untouched until you backfill them from the sessions window.

Automatic summarisation is controlled by **Summarise saved sessions automatically** in Settings → Sessions (`~/.config/vinowhisper/summaries.json`, `{"auto": true}` by default). Manual per-session summarisation still works when automatic runs are disabled.

## Transcribing audio and video files (`vinowhisper-transcribe`)

Added 2026-11-09 (issue #162). Convert existing recordings (voice memos, lectures, calls) into `.vwsession` bundles without playing them back:

```bash
vinowhisper-transcribe FILE [--model …] [--language …] [--task …] [-o DIR] [--json]
```

You can also click **Import audio or video…** in the sessions window to run the same command with progress display and cancellation. Imported sessions support summaries, export, Open in…, and Read aloud identically to live sessions.

- **Audio decoding:** Uses `ffmpeg` (extracting the first audio stream as 16 kHz mono float, so video files work), falling back to `gst-launch-1.0` with `decodebin` (requires GStreamer's `libav` plugin for AAC and most video). Without either, only 16-bit 16 kHz WAV is supported and other formats print your distro's install command (also shown in `vinowhisper-doctor` and `vinowhisper-setup`). Audio is streamed rather than loaded whole into RAM (both decoders verified 2026-11-09 on WAV, MP3, MP4, and filenames containing quotes, backslashes, and `!`).
- **Chunking without overlap:** Files have no live-latency requirement, so they are split into 15–29.5s pieces (`FILE_PIECE_MIN_S`, `MAX_WINDOW_S`) at the quietest 0.3s window in that span, normalized like captions, and post-processed like dictation (repeat loops, `[BLANK_AUDIO]`). Digital silence below `SILENCE_RMS_THRESHOLD` is skipped.
- **Non-speech filtering via Silero VAD (#180, 2026-11-11):** `vinowhisper/vad.py` uses MIT-licensed `silero_vad_openvino_16k.onnx` (1.3 MB, commit/SHA-256 pinned in `vad_model.json`, downloaded on first import after validating file/server) on CPU to score 32 ms frames. 100 ms frames scoring below 0.5 are treated like silence, and 3.1s without speech (2.5s paragraph gap + two 0.3s pads) forces a piece boundary so pauses with background noise (barking, bells) still split paragraphs reliably. Non-speech frames are scaled by `1e-4` rather than zeroed so pause cuts do not tie at the earliest point (zeroing increased pieces from 52 to 63 and dropped 33 words on the 19-minute reading below). If VAD fails to fetch, load, or run mid-file, import continues using RMS silence only and reports a warning (`Ready.warnings` and `{"event": "Warning", "message": …}` in `--json`, shown as an "Import note" in the UI).
- **Timestamps:** Paragraph breaks occur after 2.5s without speech or at sentence ends past 70 words. Breaks inside a piece estimate their timestamp proportionally by word position (within a few seconds); breaks after a pause are stamped at speech start minus the 0.3s pad (up to 1s early).
  - **Why Whisper segment timestamps are not used:** Tested on the NPU (2026-11-11, 19-minute *Pride and Prejudice* LibriVox reading): timestamp mode took 106s vs. 65s, raised WER from 6.9% to 8.6%, hallucinated phrases (including repeating "the book of" 24 times), and still stamped the first segment at `0.00` after 10s of barking.

  | NPU VAD benchmark (2026-11-11) | Time | WER | Paragraphs |
  |---|---|---|---|
  | 19-min LibriVox reading, no VAD | 67.0s | 6.9% | 42 |
  | 19-min LibriVox reading, with Silero VAD | 73.3s | 6.9% | 42 |
  | `espeak` (3 talks at 0:00, 0:26, 1:04 + synth barks/bells) | 2.5s | — | 3, stamped 0:00, 0:25, 1:03 (was 2) |

  Silero VAD uses ~7s of one CPU core per 19 minutes of audio. (Noise clip used synthetic square-wave barks and sine bells; the issue's original `linuxlab` MP4 has not been re-run.)
- **Flag checking:** `--model`, `--language`, and `--task` verify that the running server matches instead of silently switching models or transcribing in the wrong language.
- **Background priority:** Pieces send `X-Vinowhisper-Priority: background`, yielding to live captions/dictation for up to 5s (`BACKGROUND_YIELD_S`). While `/health` reports `live_active` (live use within the last 10s), pieces shrink to 6–12s so live cycles wait behind at most one short decode (tested 2026-11-09 on CPU: 57 short pieces took 34.9s vs. 25.7s at 7.0% vs. 7.4% WER). Summaries yield to file imports as well.
- **Retries, hashing, and cancellation:** The input file is hashed before decoding so moving or deleting it mid-import does not break saving. Each piece retries up to 3 times if the server restarts (aborting if the server restarts with a different model/language). Cancelling, closing the sessions window, or importing silent audio writes nothing. `-o DIR` writes outside the managed transcripts folder (excluded from listing/auto-summary).

**CPU benchmark (2026-11-09, n=1; not yet benchmarked on NPU):** 4-core cloud Xeon 2.3 GHz, OpenVINO 2026.4.1, prebuilt `OpenVINO/whisper-small.en-fp16-ov`, 8.5 minutes of LibriSpeech (73 utterances, 1,150 words, 0.4s gaps):

| Input file | Time | Speed | Pieces | Raw WER |
|---|---|---|---|---|
| MP3 (64 kbit/s) | 25.7s | 20x real time | 23 (10–28s) | 7.4% |
| MP4 video (AAC) | 25.4s | 20x real time | 23 (16–29s) | 6.5% |

Raw WER reflects spelling differences against LibriSpeech's normalized text ("mister" vs. "Mr", "Kaliko" vs. "Calico", quotes); actual errors totaled ~15 words (including the final 5 words), with zero errors at piece boundaries.

## Bundle format (`.vwsession`)

A standard zip container modeled on `.odt`/`.epub`:

```
mimetype           application/vnd.vinowhisper.session+zip   (first entry, uncompressed, no extra field)
transcript.txt     exact captured transcript including header line
summary.md         present once summarised
attestation.json   provenance metadata and file hashes
```

Zip entry timestamps are fixed to `1980-01-01` for deterministic output. Bundles are created with mode `0600` via temp files named `.<name>.<random>.tmp`.

`vinoSearch` consumes `.vwsession` bundles (`karanshukla/vinoSearch#143`, `#145`) and relies on: uncompressed first `mimetype` entry, `format == "vinowhisper-session/1"`, named entries, RFC 3339 `session.started` with timezone offset, integer `session.duration_s`, `files["summary.md"].made_by == "ai-generated-summary"`, and paragraph timestamps matching `^\[(\d{2,}):(\d{2})\] ` (minutes continue past 60, e.g., `[61:05]`). All invariants are pinned by `tests/test_bundle.py` and fixture files `tests/fixtures/session_v1*.vwsession` (regenerated with `VINOWHISPER_BLESS_FIXTURES=1`). Breaking layout changes require bumping `format`.

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

Attestation records provenance (marking summaries as AI-generated) and SHA-256 digests verifiable with `vinowhisper-session verify`. It is unsigned provenance, not cryptographic proof. Crash-recovered or backfilled transcripts set unknown fields (such as `language` or `revision`) to `null`.

Additional optional fields inside `session` (backwards-compatible with `vinowhisper-session/1`):

- **`file` (imported files only):** Sets `source` to the filename, `started` to import time, `duration_s` to audio duration, and adds:

  ```json
  "file": {"name": "lecture.mp3", "sha256": "…", "bytes": 40481234}
  ```

- **`heard_language` (#189, 2026-11-13):** Present on Whisper `--language auto` sessions (live or imported), recording the majority detected language weighted by word count across windows (e.g., `"heard_language": "fr"`).
- **`playing` ([#156](https://github.com/karanshukla/vinoWhisper/issues/156), 2026-11-09, live system audio only):** Records active audio sources in order first observed:

  ```json
  "playing": [{"app": "Mozilla Firefox", "title": "How NPUs work - YouTube", "artist": "Some Channel"},
              {"app": "Some Game"}]
  ```

  `vinowhisper/playing.py` queries playing MPRIS players on the session bus via `busctl` (no extra dependency) and running PipeWire output streams (filtered to `--target` when set). It stores only `app`, `title`, and `artist` (control characters stripped, capped at 200 characters, max 12 entries), excluding track URLs, cover art paths, and IDs. Sampling runs off the caption loop at most once every 30s on word commit. `playing` is omitted on mic sessions, files, crash recoveries, or when no session bus is present. In summaries, `_Playing: …_` is inserted directly by code from the attestation, so the LLM never sees page titles. Tested 2026-11-09 with two fake MPRIS players on a live session bus; pairing real Chrome/Firefox MPRIS `Identity` against PipeWire `application.name` has not yet been tested on hardware.

## How summaries are generated

`vinowhisper/summary.py` uses SmolLM3-3B inside the transcription server (the same model used for dictation cleanup; see [tray.md](tray.md)):

- **Chunking:** Splits on `[MM:SS]` paragraph boundaries into ~7,000-character chunks (`CHUNK_CHARS`, fitting inside `MAX_PROMPT_LEN=3072`; oversized paragraphs split at word boundaries retaining their timestamp).
- **Code-assembled structure:** Asking SmolLM3 for a full formatted summary in one pass caused it to invent headings, omit timestamps, and drop the first half of a 16-minute dual-topic session (2026-10-30). Instead, each chunk generates bullet points (prefilled with `-`), code selects points spread across all chunks (at least 1 per chunk, aiming for ≥8 total), and separate short passes generate the `# title` (≤8 words, prefilled with `#`) and overview (≤3 sentences).
- **Decoding parameters:** `repetition_penalty=1.05` and `CHUNK_TOKENS=400`. Without repetition penalty, greedy decoding looped (#121, 2026-10-30); at `1.1`, it hallucinated details and only covered the first half of chunks.
- **Name and number filtering:** Drops any bullet introducing a proper name or number absent from its source chunk (unless all bullets in the chunk would be dropped). Incorrect claims built from valid names can still pass.
- **Prompt isolation:** Transcript text is wrapped in `<transcript>` / `<notes>` tags (with internal closing tags stripped) and treated strictly as data; summaries are only displayed and indexed, never typed or executed.
- **Timestamps and language:** Model-generated timestamps are stripped; each bullet receives the timestamp of the highest word-overlap paragraph in its chunk. Summary language follows the session language (or English for `translate` sessions).
- **Footer and retries:** Summaries end with `_Playing: …_` (if recorded) and a note stating the summary is machine-written and the transcript is authoritative. Transient failures retry up to `MAX_FAILURES` (3) before notifying; a missing model pauses the queue without counting a failure.

## Sessions window and `vinowhisper-session` CLI

Open the sessions window with `vinowhisper-gui --sessions`, from Settings → Sessions, or by enabling **Show saved sessions in the app menu** (writes `~/.local/share/applications/io.github.karanshukla.vinowhisper.sessions.desktop`, removed by `vinowhisper-tray --uninstall`).

The window lists bundles, supports file import, displays transcripts and summaries (clicking key-point timestamps jumps to the transcript paragraph), and drives `vinowhisper-session`:

```bash
vinowhisper-session list [--json]
vinowhisper-session show BUNDLE [--part transcript|summary|attestation]
vinowhisper-session export BUNDLE [--part both|summary|transcript] [--format md|txt|odt]
                              [-o PATH] [--force | --reuse] [--json] [--print-path]
vinowhisper-session summarise|delete-summary|delete|verify BUNDLE...
vinowhisper-session backfill
```

Open a specific bundle or timestamp externally (used by `vinoSearch`, notifications, and file managers):

```bash
vinowhisper-gui --open <bundle>[#transcript[@MM:SS]|#summary]
vinowhisper-gui <bundle>.vwsession
```

Legacy `--page sessions --open <bundle>` is still accepted (`--page sessions` alone opens Settings → Sessions). The fragment splits on the last `#` only for the three recognized suffixes so filenames containing `#` still open. `@MM:SS` scrolls to and highlights the matching paragraph.

### Exporting sessions

| `--format` | Output contents |
|---|---|
| `md` (default) | YAML frontmatter (`date`, `duration`, `source`, `language`, `model`, `summary_model`, `vinowhisper_session`, `tags`), `# title`, `## Summary`, and `## Transcript` |
| `txt` | Plain text `Label: value` header block, `SUMMARY`, and `TRANSCRIPT` |
| `odt` | OpenDocument Text file written via `zipfile` with headings, bullet list, and custom `meta.xml` properties (opens in LibreOffice, Word, Google Docs) |

- If `--format` is omitted, it is inferred from `-o`'s extension (default `md`).
- `[MM:SS]` timestamps and the "Machine-written summary" footer are always preserved; the raw transcript header line is omitted because metadata moves to frontmatter/headers.
- Without `-o`, files write to `$XDG_DOCUMENTS_DIR/vinoWhisper/` (falling back to `~/Documents`) named `<YYYY-MM-DD> <summary title>.<ext>` (with ` (transcript)` or ` (summary and transcript)` for other parts, or `<YYYY-MM-DD> Transcript.<ext>` if unsummarised). Unsafe filesystem/control/bidi characters become spaces; filenames cap at 200 bytes. `--print-path` prints the resolved path without writing.
- Existing files are never overwritten without `--force`: directory targets append ` (2)`, ` (3)`, etc. (written atomically via temp file + hard link/rename, mode `0644`); explicit existing file targets error.
- `--part summary` errors if no summary exists; `--part both` falls back to transcript and sets `"summary_missing": true` in `--json` (`{"path", "parts", "summary_missing", "state"}`). Export is read-only and never modifies the bundle or `sessions.json`.
- `--reuse` (with a directory target) reuses an existing export belonging to the same session (identified via `vinowhisper_session` frontmatter, `Session:` header, or `meta.xml` custom property, which LibreOffice preserves on save; checked 2026-10-29), returning `"state": "unchanged"` or `"edited"`.
- **Not supported:** SRT/VTT (bundles lack word/segment end times; requires a future `vinowhisper-session/2`), DOCX (use ODT), and batch export.
- In the UI, **Export…** opens the FileChooser portal (or saves to `$XDG_DOCUMENTS_DIR/vinoWhisper/` without a portal), and **Copy** copies the visible tab (`vinowhisper-session show`).

### Open in…

The viewer header includes an **Open in…** split button (remembering your last pick in `gui.json` as `open_in`: `"word_processor"` or `"text_editor"`):

| Option | Format |
|---|---|
| Word processor | `odt` |
| Text editor | `md` |

It exports with `--reuse` into `$XDG_DOCUMENTS_DIR/vinoWhisper/` (preserving user edits rather than overwriting) and launches via the `OpenURI` portal with `ask=true` (falling back to `xdg-open` without a portal). For Obsidian vaults, use **Export…** directly into the vault folder.

### Read aloud

If `vinospeak` is on `PATH` when the window opens, the viewer and card menus display **Read aloud**, invoking `vinospeak --file BUNDLE --part summary|transcript` (`karanshukla/vinoSpeak#42`). Clicking **Stop**, navigating away, or closing the window sends `SIGTERM` to `vinospeak`'s process group (`PR_SET_PDEATHSIG` handles crashes); only one reading runs at a time. Older `vinospeak` builds without `--part` display an update notice.

## File type association

On first start, the tray registers `~/.local/share/mime/packages/io.github.karanshukla.vinowhisper-session.xml` (`*.vwsession` + zip `mimetype` magic at offsets 30 and 38, icon `io.github.karanshukla.vinowhisper`), adds `MimeType=application/vnd.vinowhisper.session+zip;` and `%f` to the settings `.desktop` entry, and runs `update-mime-database` and `update-desktop-database`. `--export-desktop` emits the same XML for packagers.

## Summary benchmarks (2026-10-30)

Measured on Wildcat Lake NPU, SmolLM3-3B, v1.1.1 with code-assembled structure (long transcripts from LibriVox *Pride and Prejudice* decoded in 12s windows; n=1 per length):

| Session length | Chunks | Total time | Time per chunk |
|---|---|---|---|
| 13s (live) | 1 | 10s | — |
| 35s (live) | 1 | 14s | — |
| 16 min (live) | 3 | 66s | 11–24s |
| 30 min | 4 | 87s | 11–24s |
| 60 min | 8 | 181s | 11–24s |

Chunk coverage comparison on the 30-minute session (average fraction of chunk reached by the final bullet's timestamp):

| `CHUNK_TOKENS` | `repetition_penalty` | Chunk reach | Notes |
|---|---|---|---|
| 256 | 1.1 | 0.46 | 65s; hallucinated "£100k", "London", "Lizzy" not in chunk |
| 400 | 1.1 | 0.56 | 85s |
| 256 (3,500-char chunks) | 1.1 | 0.79 | 103s; more out-of-chunk names |
| 400 | 1.05 | 0.76 | 77s; plainer bullets, one out-of-chunk name |

- **Accuracy:** Timestamps pointed to the right paragraph in all 4 spot-checked cases. On the 60-minute session, ~3 of 9 bullets mixed up details (e.g., "Miss Bingley abused Jane" instead of Elizabeth). Name filtering blocked "Apple Watch" and "Android 11" (from "Android 17" + "11 Pro") on the 16-minute session, though the unchecked overview merged topics into "Android Wear OS version of the Pixel Watch".
- **NPU sharing:** Concurrent resident Whisper, SmolLM3 summarisation, and `vinoSearch` queries (1 in one run, 11 in another; ~3 MB resident) produced zero `Failed to register command queue: -16` or `intel_vpu` kernel warnings.
- **NPU memory:** `drm-resident-memory` peaked at 2.2 GB summarising (2.4 GB with Whisper loaded) and dropped back to 86 MiB (Whisper only) once the queue emptied (measured prior to the 400-token bump).
- **Caption preemption:** Interrupting an active summary caused no caption stutter (first 12s decode 0.77s vs. 0.65–0.96s baseline; next seven 0.62–0.79s); the interrupted summary restarted after 20s idle.
- **Desktop integration:** Verified light/dark theme switching, Dolphin `.vwsession` icon, `xdg-open`, and D-Bus `ActionInvoked` opening the summary from the notification (not yet clicked with a physical pointer).
