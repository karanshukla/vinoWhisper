# Overlay and dictation

`vinowhisper-tray` displays live captions in a floating box above all other windows (including fullscreen video), adds a system tray icon and global keyboard shortcuts, and provides hold-to-talk and hands-free dictation. It is optional and adds no dependencies to the Python package.

The tray spawns `vinowhisper-caption --json` and renders its event stream directly, so audio capture, stitching, and server logic remain shared with the terminal UI.

## Install

```bash
vinowhisper-setup --gui
```

`vinowhisper-setup --gui` downloads `vinowhisper-tray-x86_64-linux` and `vinowhisper-gui` from the matching GitHub Release, verifies both against SHA-256 digests pinned in the Python package (`vinowhisper/tray_release.json` and `vinowhisper/gui_release.json`), installs them into `~/.local/bin`, writes desktop launchers, and offers to enable autostart at login. Plain `vinowhisper-setup` offers this step on supported Wayland sessions and skips it on GNOME (see [Where it works](#where-it-works)).

- **Distribution details:** The Rust binaries are hosted on GitHub Releases rather than bundled in the PyPI wheel. `vinowhisper-tray` is a static musl binary (6.0 MB release; 6.3 MB local glibc build, 5.6 MB before dictation; measured 2026-09-12, 135 pure-Rust crates) with no runtime library requirements. `vinowhisper-gui` is built on Ubuntu 22.04 and requires glibc 2.35+; if it is missing, the tray continues to work.
- **Verification rules:** Any hash mismatch aborts without installing. Source checkouts and unpinned wheels have no release pin and build locally with Cargo instead of downloading unverified binaries. Prebuilt assets are currently `x86_64` only.

To build from a checkout (requires `cargo` / [rustup](https://rustup.rs)):

```bash
vinowhisper-setup --gui        # detects checkout and offers cargo build
./scripts/install.sh --gui     # bootstrap installer; rebuilds both binaries
```

Or manually:

```bash
cargo build --release --locked --manifest-path tray/Cargo.toml
install -Dm755 tray/target/release/vinowhisper-tray ~/.local/bin/vinowhisper-tray
cargo build --release --locked --manifest-path gui/Cargo.toml
install -Dm755 gui/target/release/vinowhisper-gui ~/.local/bin/vinowhisper-gui
vinowhisper-tray --install --autostart   # launcher, icons, autostart at login
```

**Distro packaging status:** Not yet packaged in distros (Fedora ships OpenVINO 2025.1.0, below the 2026.4.1 floor, and lacks `openvino-genai` and `optimum`; checked 2026-09-12). Packagers can export `.desktop` files and icons with `vinowhisper-tray --export-desktop DIR` (using bare `vinowhisper-tray` in `Exec`), and `vinowhisper-setup --gui` detects `/usr/bin/vinowhisper-tray`.

**Locating `vinowhisper-caption`:** The tray searches `PATH`, then `~/.local/bin`, then its own directory. Override with `--caption PATH` or `$VINOWHISPER_CAPTION`.

**Launchers and autostart:** First launch creates the main `.desktop` launcher only if absent (avoiding shadowing a packaged or `--caption`-pinned entry). Icons and the Settings app-menu entry (`io.github.karanshukla.vinowhisper.settings.desktop`, matching the settings window's app ID so Plasma displays its icon rather than a generic Wayland icon; fixed 2026-10-11) are kept up to date automatically. Autostart launches with `--hidden` so the NPU stays idle until requested; toggling **Start at login** in the tray menu (or re-running `--install` without `--autostart`) adds or removes it.

## Using the overlay

Launch **vinoWhisper** from the app menu or run `vinowhisper-tray`. A dark, mostly opaque box (~70 characters wide) appears at the bottom of the screen (or top, for videos with burned-in bottom subtitles) with a status bar above two caption lines. Pending unconfirmed words render dimmed, and a non-NPU fallback adds a red border. All mouse clicks pass straight through the box to windows underneath.

### Controls

- **Global shortcuts:** `Meta+Alt+C` toggles captions; `Meta+H` triggers [dictation](#dictation).
- **Tray icon:** Left-clicking toggles captions. The context menu exposes frequent controls: **Show captions**, **Listen to** (system audio, microphone, or both as two lanes), **Recent dictations** (last 5, newest first, plus **Clear** and **Add to dictionary…**), active dictation shortcut display, **Settings…** (`vinowhisper-gui`), **Start at login**, and **Quit**. Position, text size, transcript saving, language/translation, shortcuts, cleanup, and links (`vinopilot.ca` and sibling vinoPilot apps on About) live in `vinowhisper-gui`.
- **CLI control commands** (forwarded to the running tray instance):
  - `vinowhisper-tray show | hide | toggle | quit | restart`
  - `vinowhisper-tray dictate | dictate-press | dictate-release`
  - `vinowhisper-tray reload`: reloads `gui.json` and `language.json`. Source, position, text size, tray idle delay, hide-after-silence delay, and language apply immediately; `save_transcripts` applies on the next caption session; editing `shortcut` has no effect once bound in the portal. Switching language while dictating is refused (`reload` exits 1 and restores `language.json`).
  - `vinowhisper-tray shortcuts` / `configure-shortcut`: prints portal-bound shortcuts as JSON or opens the portal configuration dialog.

### Idle, auto-hide, and configuration behavior

- **Hiding stops capture:** Hiding the box stops `vinowhisper-caption` so the transcription server can idle out after 5 minutes ([architecture.md](architecture.md)). Showing it starts a fresh session (also resetting errors).
- **Auto-hide on silence:** After 3s of `Silence` events (whole-window RMS under `SILENCE_RMS_THRESHOLD`), the status bar counts down (`no signal 4s · … · hiding in 11s`) and hides the box at 15s, stopping capture. Speech cancels the countdown. Configured via `hide_after_silence_s` in `gui.json` (Settings → Captions → **Hide after silence**; `0` disables; untested on hardware as of 2026-10-11).
- **Passive tray icon:** After 30 wall-clock minutes with captions hidden and no dictation (`CLOCK_BOOTTIME` `timerfd`, so suspend time counts without waking the machine; fixed 2026-09-22 from monotonic calloop `Timer`), the tray marks itself `Passive` so Plasma tucks it behind the panel arrow (measured over D-Bus 2026-09-21). Showing captions or dictating restores it. Configured via `tray_idle_minutes` in `gui.json` (read at startup; `0` disables).
- **Saving transcripts:** Toggle **Save transcripts** (`save_transcripts` in `gui.json`, off by default) to pass `--transcript` on the next caption start ([terminal.md](terminal.md)). Completed sessions bundle into `.vwsession` files, summarise in the background, and can be browsed in `vinowhisper-gui --sessions` ([sessions.md](sessions.md)). Unit-tested as of 2026-10-07.
- **Config files:** UI settings live in `~/.config/vinowhisper/gui.json`; speech model, language, and task live in `~/.config/vinowhisper/language.json` (shared with the server). Changing language runs `vinowhisper-server --stop` so the next session loads the new model; switching mid-dictation is refused for 3s on the pill to avoid dropping the recording. Unreadable config files log a warning and fall back to defaults.

### Global shortcuts

On Wayland, global shortcuts are managed by `xdg-desktop-portal`. On Plasma, a confirmation dialog appears on first run; afterward the bindings live in **System Settings → Shortcuts → vinoWhisper** (or click **Change…** on Settings → Shortcuts, which requires portal v2 `ConfigureShortcuts`). The `shortcut` key in `gui.json` (XDG syntax, default `LOGO+ALT+C`) sets only the initial preferred trigger.

Measured 2026-09-12 on Plasma 6.7: the portal requires an installed `.desktop` file to resolve an app ID ("App info not found" / "An app id is required"), which is why the tray creates its launcher automatically on first run if missing.

### Without a shortcuts portal

Sway, niri, and COSMIC do not implement `GlobalShortcuts`, and Hyprland ignores `preferred_trigger`. The tray logs `no global shortcut` and continues running; bind its CLI commands directly in your compositor:

| Command | Action | Bind to |
|---|---|---|
| `vinowhisper-tray toggle` | Show/hide captions (starts tray if stopped) | Key press |
| `vinowhisper-tray dictate-press` | Dictation key down | Key press |
| `vinowhisper-tray dictate-release` | Dictation key up (types, or starts hands-free on short tap) | Key release |
| `vinowhisper-tray dictate` | Tap toggle (start, or stop and type) | Key press (if no release bindings) |

Press + release bindings reproduce full hold-to-talk and tap-for-hands-free behavior (`dictation::TAP` = 350 ms). `dictate*` commands require the tray to be running (`vinowhisper-tray --autostart` or `exec vinowhisper-tray --hidden`).

**Sway (`~/.config/sway/config`):**

```
bindsym --no-repeat $mod+Alt+c exec vinowhisper-tray toggle
bindsym --no-repeat $mod+h exec vinowhisper-tray dictate-press
bindsym --release $mod+h exec vinowhisper-tray dictate-release
```

Use `--no-repeat` so holding the key does not spawn ~25 extra processes per second.

**Hyprland (`~/.config/hypr/hyprland.conf`):**

```
bind = SUPER ALT, C, exec, vinowhisper-tray toggle
bind = SUPER, H, exec, vinowhisper-tray dictate-press
bindr = SUPER, H, exec, vinowhisper-tray dictate-release
```

**niri (`~/.config/niri/config.kdl`)** (tap mode only; hands-free also auto-stops on silence):

```
binds {
    Mod+Alt+C repeat=false { spawn "vinowhisper-tray" "toggle"; }
    Mod+H repeat=false { spawn "vinowhisper-tray" "dictate"; }
}
```

**COSMIC:** Settings → Keyboard → Keyboard Shortcuts → Custom Shortcuts for `vinowhisper-tray toggle` and `vinowhisper-tray dictate`.

Verified 2026-10-24 on headless Sway 1.9 (Ubuntu 24.04, VNC input, stubbed `vinowhisper-dictate`): hold-to-talk, tap-for-hands-free, tap-to-finish, and `toggle` all worked. Hyprland, niri, and COSMIC configs are from documentation.

## Dictation

Added 2026-09-21 ([issue #12](https://github.com/karanshukla/vinoWhisper/issues/12)). Dictation types spoken text into whichever window currently has focus.

- **Hold-to-talk:** Hold `Meta+H` (matching Windows `Win+H` / laptop F-row dictation keys), speak, and release to type.
- **Hands-free mode ([issue #56](https://github.com/karanshukla/vinoWhisper/issues/56), 2026-10-07):** Tap `Meta+H` (<350 ms) to start hands-free, then tap again to finish—or pause speaking (auto-stops after ≥0.5s of speech followed by 2.5s of silence; silence before speech never stops it, and held keys never auto-stop).

Rebind under Settings → Shortcuts → **Change…** or System Settings (*Dictate*). Verified 2026-09-21 on KDE: adding `Meta+J` beside `Meta+H` sent `ShortcutsChanged` (`"Meta+H, Meta+J"`) and worked immediately for hold-to-talk.

**Desktop remote-control prompts:** Because Wayland prevents apps from injecting keystrokes silently, dictation uses `org.freedesktop.portal.RemoteDesktop`:

- First use opens a "Remote Control Requested" dialog (on Plasma 6.8, check "Allow … without asking next time").
- Starting a typing session shows one "Remote control session started" notification and KDE's tray indicator. The session stays open for 5 idle minutes by default (**Settings → Speech → Keep typing access**; `typing_idle_minutes` in `gui.json`; `0` keeps it open until tray quit).
- Compositors without a `RemoteDesktop` portal (Sway, niri, COSMIC) cannot receive typed text or portal clipboard writes: the pill shows "Not typed" and stores the text in **Recent dictations**.

**Hands-free silence detection:** Implemented in Python (`vinowhisper-dictate`) using sample counts (`HANDS_FREE_MIN_SPEECH_S` = 0.5s, `HANDS_FREE_SILENCE_S` = 2.5s, `SILENCE_RMS_THRESHOLD`). Because the overlay only knows tap vs. hold on release, it sends a `hands-free` command on a short tap's release, enabling auto-stop while counting speech already heard. The 2.5s timeout is an initial estimate, not yet tuned on hardware.

### Pausing media while you dictate

Added 2026-11-09 ([issue #163](https://github.com/karanshukla/vinoWhisper/issues/163)). Enable **Pause media** on Settings → Speech (`pause_media` in `gui.json`, off by default).

On dictation key press, `tray/src/media.rs` (dedicated thread, 1s timeout per call) finds `org.mpris.MediaPlayer2.*` players with `PlaybackStatus == Playing` and calls `Pause`. After dictation finishes (or on tray quit mid-dictation), it calls `Play` only on players it paused that are still `Paused`. Players ignoring MPRIS continue playing. Applies to dictation only (enabling hands-free silence detection while music was playing), not mic captions. Verified 2026-11-09 against fake MPRIS players on a live session bus; real browser timing vs. mic startup (90–290 ms) is untested on hardware.

### A space between dictations

Added 2026-11-10 ([issue #120](https://github.com/karanshukla/vinoWhisper/issues/120)). Enable **Space between dictations** on Settings → Speech (`smart_spacing` in `gui.json`, off by default).

Prepends a single space before typing when all four conditions hold (`Spacing` in `tray/src/dictation.rs`, unit-tested only):

1. The previous dictation was typed (not clipboard fallback).
2. Less than 30s elapsed between the previous paste and this key press.
3. The previous text did not end in whitespace.
4. The new text starts with a letter or digit (not punctuation).

Because Wayland does not report focus changes, switching fields within 30s inserts a leading space in the new field (hence off by default). Recent dictations, the pill, and LLM cleanup do not include the prepended space.

### Cleaning up what you say

Optional (beta, off by default). Enable **Clean up dictation** on Settings → Cleanup to run a local SmolLM3-3B model (1.7 GB download, verified per file, runs on NPU/GPU/CPU inside the local server) before typing. **Cleanup strictly deletes words**—it never inserts, rewords, converts "three" to "3", or answers questions ("write me a poem" types those words).

| You say | Typed output |
|---|---|
| "So um I think we should uh ship it on Friday." | "So I think we should ship it on Friday." |
| "We need that that fixed." | "We need that fixed." |
| "Let's meet at three, no wait, four in the lobby." | "Let's meet at four in the lobby." |
| "Send the invoice. Remind Sam about lunch. Scratch that. Call the dentist." | "Send the invoice. Call the dentist." |
| "The release is ready for review, delete that last word." | "The release is ready for." |
| "To undo it, say literal scratch that." | "To undo it, say scratch that." |

- Removes **fillers** (`um`, `uh`, `er`, `hmm`, plus `euh`, `ähm`, `eh`), **stammers/restarts**, **self-corrections** (`no wait`, `I mean`, `sorry`, `or rather`; `actually` is treated only as a filler), and **spoken edits** (`scratch that`, `delete that last word`, plus French/German/Spanish equivalents like `non attends`, `streich das`, `borra eso`). Say **literal** before a command to keep the phrase.
- In default **Smart** mode, cleanup runs only when a cue word or adjacent duplicate appears; **Every dictation** runs it on all utterances.
- Any output that fails the strict deletion check, times out (default 4s), or errors falls back immediately to the raw transcript. The model warms on key press (~2s per cleaned dictation once warm) and unloads after 5 idle minutes. Run with `VINOWHISPER_CLEANUP_DEBUG=1` to inspect inputs and outputs. See [LLM cleanup (beta)](#llm-cleanup-beta) for benchmarks and config keys.

### How dictation works

#### Segmentation, previews, and mic noise floor

Dictation runs as one `Dictated` utterance without caption stitching, capping at 5 minutes (`DICTATION_MAX_S`):

- **Background piece cutting:** Once a piece reaches 5s (`SEGMENT_MIN_S`) and sees 300 ms of consecutive quiet 100 ms chunks (`SEGMENT_PAUSE_S`), it decodes in the background on a single worker thread (serialized behind the server's transcriber lock alongside any active captions). If no pause occurs before `MAX_WINDOW_S`, it cuts after the quietest chunk in the last 3s (`SEGMENT_FALLBACK_S`). On release, only the tail decodes, pieces join with single spaces, and cleanup runs once. Short utterances measured 0.5–0.8s release-to-text on NPU (2026-09-21); ~60s dictations are unmeasured. Whisper capitalizes each piece independently, which can introduce mid-sentence periods.
- **Whole-utterance rules:** The 0.3s minimum duration, silence check, hands-free stop, and cancel apply across the full recording. Dictation keeps its own per-piece audio tap (~19 MB for a 5-minute utterance) separate from captions' 29.5s ring buffer.
- **Live pill previews ([issue #55](https://github.com/karanshukla/vinoWhisper/issues/55), 2026-10-07):** Finished pieces emit cumulative `Partial` events shown beside the level meter (last 64 characters; status messages like "Transcribing" render dim italic). Every 1.5s (`PREVIEW_EVERY_S`), if the worker is idle and the last 0.3s (`PREVIEW_QUIET_S`) was not quiet (avoiding delaying release—2026-10-08 measured 3 of 8 releases waiting 282–528 ms behind a preview, lifting release-to-text from 0.6–0.8s to 0.9–1.2s; logged via `behind_preview`), the uncut tail (`≥PREVIEW_MIN_S`) decodes as a throwaway preview.
- **Adaptive mic noise floor (2026-10-07/08):** Because laptop mic noise (~0.021 RMS) exceeds `SILENCE_RMS_THRESHOLD` (0.002), `_Segmenter` tracks the quietest chunk as the floor (capped at `NOISE_FLOOR_MAX_RMS`, ignoring dropouts under half the fixed threshold) and treats `<2x` floor (`NOISE_FLOOR_MARGIN`) as quiet. The first 0.5s (`NOISE_FLOOR_SETTLE_S`) is excluded from setting the floor because Intel DMIC firmware (Zephyr `drivers/dai/intel/dmic`, triggered whenever WirePlumber resumes a mic after 5s idle) ramps -90 dB → 0 dB over 100 ms at 48 kHz / 200 ms at 16 kHz, which otherwise pulled the simulated floor from 0.021 down to 0.005 (simulated from firmware source, not measured on hardware).
- **Idle worker process:** The tray keeps `vinowhisper-dictate --json` idle on stdin to avoid 270 ms of Python import time on key press. Key press spawns `pw-record` (audio arrives in 90–290 ms) and warms the server; release records an extra 250 ms tail so final syllables are not clipped.

#### Typing, clipboard, and Recent dictations

Since 2026-11-14, both native and Flatpak builds use `org.freedesktop.portal.RemoteDesktop` exclusively (replacing the older `ext-data-control-v1` + `Shift+Insert` / `/dev/uinput` / `zwp_virtual_keyboard_v1` / `org_kde_kwin_fake_input` routes, because KWin 6.8 hides `ext-data-control-v1` and `fake_input` from Flatpak sandboxes; verified 2026-11-14):

- **Keysym typing:** Each character is sent via `NotifyKeyboardKeysym` (Latin-1 code point, or `0x01000000 + codepoint` for other Unicode). Line breaks and tabs (possible via `replacements.json`) are mapped to spaces so typing into a terminal never executes a command. Verified 2026-11-14 on Plasma 6.8.2 (US layout): curly quotes, em dash, ellipsis, accented characters, `€`, and `ß` all typed accurately (non-US layouts may hit [KDE bug 489021](https://bugs.kde.org/show_bug.cgi?id=489021), untested).
- **Session persistence and tokens:** The first session prompts for permission; KDE's restore token is updated on every start in `~/.local/state/vinowhisper/remote-desktop.token` (~0.01s silent restore). One persistent session stays open until `typing_idle_minutes` passes (default 5; verified 2026-11-14: one notification across three dictations 5s apart, tray indicator cleared on idle close). If the desktop closes a session mid-idle, one fresh session is attempted before any character is typed.
- **Portal clipboard & Recent dictations:** Uses the same portal session (`RequestClipboard`, `SetSelection`, `SelectionTransfer`) when typing fails partway or when clicking an entry in **Recent dictations** (last 5 non-empty texts, 40-char menu labels, held in memory only, excluded from disk/logs/`VINOWHISPER_TRAY_TRACE`, cleared on quit or **Clear**). Copies set `x-kde-passwordManagerHint: secret` to keep Klipper out (no primary selection / middle-click). Because the desktop reads offered clipboard selections immediately (traced 2026-11-14), copies remain available until overwritten or the portal session closes. Verified 2026-11-14 on Plasma 6.8.2 Flatpak. Clicking a recent item while dictating is ignored.

#### Word replacements

Added 2026-10-07 ([issue #58](https://github.com/karanshukla/vinoWhisper/issues/58)). Create `~/.config/vinowhisper/replacements.json` (under `$XDG_CONFIG_HOME` if set) to post-process dictated text on any device/model:

```json
{"open vino": "OpenVINO", "vino whisper": "vinoWhisper", "k8s": "Kubernetes"}
```

Matches case-insensitively on whole words, longest key first, collapsing internal whitespace runs and preserving surrounding punctuation (`"open vino."` → `"OpenVINO."`). Re-read on every dictation start; invalid JSON logs a warning to `stderr` and is ignored (`vinowhisper-doctor` reports status). Dictation only (excluded from captions to avoid interfering with stitcher anchors; unit-tested only).

#### Personal vocabulary

Added 2026-11-10 ([issue #161](https://github.com/karanshukla/vinoWhisper/issues/161)). List custom terms (one per line; blank lines and `#` comments ignored) in `~/.config/vinowhisper/vocabulary.txt`, via **Settings → Speech → Vocabulary**, or via **Recent dictations → Add to dictionary…**. Applies to dictation only on the next press.

```
# names and jargon
Kubernetes
vinoWhisper
Saoirse Ronan
```

| Mechanism | Whisper (GPU / CPU) | Whisper (NPU) | Canary (any device) |
|---|---|---|---|
| Post-decode spelling correction | Yes | Yes | Yes |
| Decoder prompt (`hotwords` / step loop) | Yes | Yes (with step export; correction-only without) | No |

- **Spelling correction (`vocabulary.correct`):** Runs before `replacements.json`. Replaces single words sharing the same first letter within Levenshtein distance 1 (5–7 char terms) or 2 (8+ char terms), e.g., `"Creswell"` → `"Cresswell"`, `"Sothey's"` → `"Southey's"`. Short (<5 chars) and multi-word terms match exactly only. Because it has no English dictionary, nearby real words can also match (`"poser"` → `"Poyser"`, `"clause"` → `"Claude"`); move ambiguous terms to `replacements.json`. Uses `rapidfuzz` (lazy-imported) since 2026-11-16 (verified identical on 300,010 pairs and 3,000 generated sentences on Xeon).
- **Decoder prompt:** `vinowhisper-dictate` sends `X-Vinowhisper-Vocabulary`, and the server reloads `vocabulary.txt` on change. Prompts pass as comma-separated `hotwords` (equivalent to `initial_prompt` for <30s audio in `context_tokens.cpp`), capped at 120 tokens (~30 names, max 100 terms of ≤64 chars; answer token limit is reduced accordingly within Whisper's 448 slots—899-token prompts returned `, Qu, Qu`).
  - OpenVINO GenAI's `WhisperPipeline` rejects prompts on the NPU (`pipeline_static.cpp` assert, checked 2026.4.1.0, 2026-11-10). Since #178 (2026-11-11), when the custom fp16 step export (`whisper-small.en-step-ov-fp16` from `scripts/export_whisper.py`) exists and language is not `auto`, the NPU runs `whisper_step.StepWhisper` instead (see [models.md](models.md)). `vinowhisper-doctor` and the server share `vocabulary.unprompted` to report which mode is active.
  - **Prompted-decode safety checks:** If a prompted decode outputs only listed terms (noise echo) or loops (zlib ratio > 2.0, e.g., barking returning `"Cebu"` 48x on NPU), it re-decodes without the prompt and keeps the plain result if dissimilar (`vocabulary.ECHO_SIMILARITY`).
  - **NPU espeak check (2026-11-11):** 5 sentences, 14 rare names, 12-term list: 3/14 without prompt → 9/14 with prompt.

**CPU benchmark (2026-11-10, 4-core Xeon, OpenVINO 2026.4.1, 700 LibriSpeech test-clean utterances / 85.7 min, 15 rare spoken names occurring 47 times + 5 unspoken tech terms):**

| Configuration | Spoken names right (of 47) | WER (name utterances) | WER (other 12,961 words) |
|---|---|---|---|
| Whisper, no list | 28 | 6.32% | 3.23% |
| Whisper, correction only | 39 | — | Unchanged |
| Whisper, prompt (20 terms / 80 tokens) | 47 | 4.30% | 3.36% |
| Whisper, prompt (59 terms / 200 tokens) | 45 | 6.75% | 3.26% |
| Canary, no list (42 name utterances) | 35 | — | — |
| Canary, correction only | 45 | — | — |

Findings: correction alone fixed 12 utterances with zero regressions; 80-token prompting reached 47/47 while slightly raising non-name WER (3.23% → 3.36%, occasional style bleed like `"utter a sigh"` → `"atturriSai"`); 200-token prompts regressed accuracy and slowed decode (0.80s → 1.18s; 321 tokens looped on noise); the echo check reduced false name outputs on 45 noise clips from 7 to 0; unspoken terms never leaked into speech (0/700). Boosting Canary decoder logits (+4/+8 on continuations) was tested and rejected (35 → 36–37/47, broke greedy decoding like `"Southey"` → `"Southie"`). Not yet measured on live mic dictation or multilingual models.

#### LLM cleanup (beta)

Added 2026-10-11, local since 2026-10-27 ([issue #81](https://github.com/karanshukla/vinoWhisper/issues/81), [#91](https://github.com/karanshukla/vinoWhisper/issues/91); the earlier external API path and wallet key are removed).

Uses **SmolLM3-3B** (Apache 2.0, trained on `en`, `fr`, `de`, `es`, `it`, `pt`), exported with channel-wise INT4 (`--group-size -1`) as [`arankays/SmolLM3-3B-int4-cw-ov`](https://huggingface.co/arankays/SmolLM3-3B-int4-cw-ov) and pinned in `vinowhisper/cleanup_model.json` (1.7 GB; managed via Settings → Cleanup or `vinowhisper-cleanup model fetch`).

**Model evaluation on NPU (2026-10-27, n=1, 30-dictation corpus in `scripts/cleanup_eval.py` + 550–2,800-token summaries):**

| Model | Download | Decode speed | Cleanup (good / refused-but-wanted) | Notes |
|---|---|---|---|---|
| **SmolLM3-3B** | 1.6 GB | ~20 tok/s | 22 / 2, median 1.9s | Selected |
| Gemma 4 E2B | 4.5 GB | 9–12 tok/s | 19 / 1, median 1.8s | NPU driver error twice on 2.8k-token prompt |
| Qwen3-4B-2507 | 2.1 GB | ~15 tok/s | 19 / 1, median 2.3s | Obeyed planted instruction in transcript |
| LFM2.5-1.2B | 0.6 GB | ~37 tok/s | 9 / 10 | Answered `fr`/`de`/`es` in English |
| Phi-3.5-mini | 2.0 GB | 10–14 tok/s | 11 / 6 | Leaked prompt tags |
| Qwen3-1.7B (group-wise INT4) | 1.2 GB | — | — | Still compiling on NPU after 10 min |

**Server execution & safeguards:**

- **Endpoints:** `POST /cleanup` (`{"text", "budget_s"}`) and `POST /cleanup/warm` (triggered on key press; cold compile ~17–25s, warm load from `~/.cache/vinowhisper/cleanup-cache` 4.5s; 1.8–2.7s per 1–2 sentence dictation on NPU, 2026-10-27). Uses greedy decoding, `/no_think`, no repetition penalty, `MAX_PROMPT_LEN=3072`, `MIN_RESPONSE_LEN=1024`.
- **Idle unload:** Unloads after 5 idle minutes (`CLEANUP_IDLE_UNLOAD_S`), even while captions keep the server running (2026-10-27: NPU memory dropped 2,078 MB → 8 MB, RSS 1,474 MB → 1,059 MB; timestamp updated in `finally`).
- **Timeouts:** Enforced on both client (`budget_s`, default 4s) and server streamer (capped at `CLEANUP_MAX_BUDGET_S` = 10s).
- **Integrity re-hash:** On load, compares size, mtime, and inode against `.vinowhisper-verified` (0.00s unchanged; 1.27s warm re-hash for 1.7 GB, 2026-10-27). Mismatches block loading and prompt re-download.
- **Validation:** Output must be an ordered word subsequence of the input (case/punctuation ignored) with no control characters, newlines, or unapproved punctuation (only `. , ! ? : ' " - – … ` may be newly added). Replacements run after cleanup. Not streamed; pill shows "Cleaning up with the model" (`Cleaning` event).
- **Circuit breaker (#87):** Missing model or HTTP 400 disables cleanup until settings/model files change; 3 consecutive service errors (timeouts, 5xx, unreachable) pause cleanup for 10 minutes before probing again (refused model outputs do not trip it). Pill shows a 6s `Notice`. Reset by restarting the tray.
- **CLI test helper:** `vinowhisper-cleanup "um so scratch that"` runs one cleanup through the server and prints wall time to `stderr`.

**Configuration (`~/.config/vinowhisper/cleanup.json`, re-read every key press; env vars override; legacy `url`/`model` keys ignored):**

| Environment variable | JSON key | Default |
|---|---|---|
| `VINOWHISPER_CLEANUP` | `enabled` | `false` |
| `VINOWHISPER_CLEANUP_BUDGET_S` | `budget_s` | `4.0` (hard deadline) |
| `VINOWHISPER_CLEANUP_MODE` | `mode` | `smart` (`smart` or `always`) |

`smart` mode checks a merged cue list across `en`, `fr`, `de`, and `es` (fillers, `actually`, `sorry`, `wait`, `"I mean"`, `"scratch that"`, `"delete that"`, `"you know"`, `literal`, `"non attends"`, `"nein warte"`, `"streich das"`, `"no espera"`, `"borra eso"`, or adjacent repeated words).

#### Ambient room noise rejection and dictation diagnostics

Added 2026-10-11 after ambient room noise (0.011–0.028 RMS lifted 2.7x by `normalize()`) caused Whisper hallucinations. `_Segmenter` enforces two rules (constants in `config.py`):

- **Stationary recording check:** Rejects recordings whose loudest 500 ms is both `<2.8x` the quietest 100 ms and `<0.04` RMS.
- **Per-piece speech check:** Requires at least two 100 ms chunks at ≥60% of the recording's peak RMS, trimming quiet margins to 0.3s.

Opt-in debugging switches (off by default—these log or save your voice/text, so unset them after debugging):

- `VINOWHISPER_CLEANUP_DEBUG=1`: logs noise-gate verdicts (`gate: floor=... speech=...`), raw transcripts, and cleanup outputs to tray `stderr`.
- `VINOWHISPER_DICTATE_AUDIO_DIR=<dir>`: saves raw and decoded WAVs for the newest 10 dictations (`0700` dir / `0600` files), replayable with `scripts/decode_wav.py`.
- `VINOWHISPER_DICTATE_TRACE=1` ([issue #59](https://github.com/karanshukla/vinoWhisper/issues/59), 2026-10-07/08; logs timings only, no text or audio; forwarded with `[dictate]` prefix):

  ```
  [vinowhisper] dictate-trace: first_chunk=190ms first_loud=390ms (chunk 3) first_rms=0.0004 threshold=0.002
  [vinowhisper] dictate-release: tail_wait=250ms final_audio=2.4s behind_job=False queue_wait=0ms decode=480ms preview_stale=900ms release_to_text=790ms
  ```

  (Example format only, not a hardware measurement.) Run ~20 normal dictations with `VINOWHISPER_DICTATE_TRACE=1 vinowhisper-tray` to test whether `pw-record` startup clips the first word (`first_loud` at chunk 1 vs. chunk 3+) or whether preview reuse is worthwhile (`preview_stale < ~500 ms`).

### Settings window (`vinowhisper-gui`)

Runs with vsync disabled because KWin withholds frame callbacks from hidden windows, causing vsync swaps to block and trigger "Not Responding" (observed 2026-10-11; eframe 0.37 fixes Wayland via egui#5136, and disabling vsync covers X11 too).

The About page's **Updates** row runs `~/.local/bin/vinowhisper-upgrade` on release installs, `vinowhisper-flatpak-update` on release Flatpaks, or links to GitHub Releases on source checkouts.

### Screen readers

Because the dictation pill is a click-through layer-shell surface with no accessibility tree, `tray/src/announce.rs` mirrors errors and warnings as desktop notifications while a screen reader is active (#98). Dictated text is never sent.

## Where it works

The overlay uses Wayland's `wlr-layer-shell` protocol (*overlay* layer) so it floats above fullscreen windows without window rules:

| Desktop | Overlay | Tray | Shortcut | Dictation typing | Status |
|---|---|---|---|---|---|
| KDE Plasma 6.7 / 6.8 (Wayland) | Yes | Yes | Portal | `RemoteDesktop` portal | **Tested on hardware** (overlay 2026-09-12; dictation 2026-09-21; portal typing 6.8.2 2026-11-14) |
| Hyprland | Yes | Needs SNI tray | Bind in `hyprland.conf` ([above](#without-a-shortcuts-portal)) | Portal if `RemoteDesktop` enabled; else clipboard only | Untested |
| Sway, niri, COSMIC | Yes | Needs SNI tray | Bind commands ([above](#without-a-shortcuts-portal)) | None (no `RemoteDesktop` portal) | Sway 1.9 virtual-keyboard worked until 2026-11-14; niri/COSMIC untested |
| GNOME | **No (`wlr-layer-shell` unsupported)** | AppIndicator extension | Portal (48+) | — | Refuses to start; use `vinowhisper-caption` |
| X11 sessions | No | — | — | — | Refuses to start; use `vinowhisper-caption` |

(Before 2026-11-14, clipboard used `ext-data-control-v1` / `zwlr_data_control_manager_v1`; KWin 6.8 hides both from Flatpak apps, so clipboard moved into the `RemoteDesktop` portal session.)

On unsupported compositors, `vinowhisper-tray` exits with an explicit error instead of falling back to a regular window. Text renders at fractional display scale via `wp_fractional_scale_v1` + `wp_viewporter` (falling back to integer scale otherwise), stays clear of panels, recreates automatically if an output disconnects, and supports top or bottom placement (not draggable).

## How it fits together

```
vinowhisper-tray ──spawns──> vinowhisper-caption --json ──HTTP──> vinowhisper-server (NPU)
      │   ▲                          │
      │   └──── one JSON event per line on stdout
      ├──spawns──> vinowhisper-dictate --json ──HTTP──> (same server)
      │              ▲ start / stop / cancel on stdin
      ├── tray (StatusNotifierItem over D-Bus)
      ├── shortcuts (xdg-desktop-portal GlobalShortcuts: Activated and Deactivated)
      ├── typing: RemoteDesktop portal, one keysym per character, one session
      │           kept open until typing_idle_minutes pass without a dictation
      └── $XDG_RUNTIME_DIR/vinowhisper-tray.sock (show/hide/toggle/dictate/quit)
```

- **Wire protocol:** `--json` serializes `events.to_dict` as ASCII-escaped JSON lines (plus `Error` records on fatal exits). `tests/test_caption.py` verifies every field consumed by `tray/src/protocol.rs`, and `protocol.rs` fixtures are captured from `JsonRenderer`. Unknown fields and stray lines are ignored for forward compatibility.
- **Rendering & power:** The click-through surface spans the full output width with the box centered inside. Only visual changes trigger Wayland repaints or D-Bus tray updates, and the 1s timer runs only while "Starting, Ns" is displayed.
- **Process lifecycle:** Stopping captions sends `SIGINT` (4s grace before `SIGKILL`) so pending words flush cleanly; `PR_SET_PDEATHSIG` ensures child processes exit if the tray crashes.
- **Single-instance socket:** Secondary invocations forward their command over `$XDG_RUNTIME_DIR/vinowhisper-tray.sock` (fallback `/tmp/vinowhisper-tray-<uid>/`, `0700`, verified not symlinked and owned by current UID; `SO_PEERCRED` rejects other UIDs). Stale sockets are detected by probing, and `--hidden` exits quietly if an instance is already running.

## Why Rust

Evaluated 2026-09-12: `PySide6-Essentials` required 232 MB installed, Fedora 44's system PyGObject was built for Python 3.14 (incompatible with the Python 3.13 venv), and Tkinter lacks Wayland and tray support. The Rust overlay is a 6.3 MB libc-only binary that rasterizes text on CPU (`cosmic-text`) and draws antialiased rounded rectangles/circles via signed-distance functions directly into Wayland premultiplied `ARGB8888` buffers without a GPU context or 2D graphics library.

## The icon

Defined procedurally in `tray/src/icon.rs` (three green voice bars above confirmed and pending caption lines inside the caption box). Launcher SVGs, tray fallback pixmaps, and `docs/assets/` copies are generated from it (`VINOWHISPER_BLESS_ICONS=1 cargo test`; a test fails if stale). The 16px Breeze-style tray icon uses 1px even-odd outlines and KDE's `current-color-scheme` stylesheet so strokes adapt to panel color while keeping green voice bars.

## Troubleshooting

Run `vinowhisper-tray` from a terminal to view logs (caption output is prefixed `[caption]`).

| Symptom | Cause and fix |
|---|---|
| `no global shortcut (... An app id is required)` | Missing `.desktop` launcher; run `vinowhisper-tray --install` |
| `vinowhisper-caption was not found` | Not on desktop `PATH`; run `vinowhisper-tray --install --caption /path/to/vinowhisper-caption` |
| Box shows `not reachable` | Server issue rather than overlay; run `vinowhisper-doctor` |
| No tray icon | Missing StatusNotifierItem host (e.g., GNOME without AppIndicator); shortcuts and CLI still work |
| `does not offer wlr-layer-shell` | GNOME or X11 (see [Where it works](#where-it-works)); use `vinowhisper-caption` |
| Box displays raw stderr text | Caption process exited without an `Error` event; run `vinowhisper-caption` in a terminal |
| Unexpected caption font | Configured `sans-serif` not installed; falls back to Inter, Noto Sans, Cantarell, Ubuntu, DejaVu Sans, or Liberation Sans |
| Dictation: `vinowhisper-dictate was not found` | Outdated install; run `pip install -U vinowhisper && vinowhisper-setup` |
| Dictation: "Remote control" notification | KDE announces each new portal session; raise **Settings → Speech → Keep typing access** (`0` keeps it open) |
| `no global shortcut` in log | Compositor lacks `GlobalShortcuts` (Sway/niri/COSMIC) or ignores triggers (Hyprland); bind commands [in the compositor](#without-a-shortcuts-portal) |
| Dictation: `Not typed` | Portal refused or failed; recover text from clipboard (if shown) or **Recent dictations**; restart tray to re-prompt after refusal |
| Dictation key press ignored | Previous dictation is still transcribing; inspect with `VINOWHISPER_TRAY_TRACE=1 vinowhisper-tray` |
| Old behavior after upgrade | Running tray/dictate binary is stale; run `vinowhisper-tray restart` (automated since 1.6.2; refuses outside Wayland/over SSH since 2026-11-13) |
| Old icon after upgrade | Stale Plasma icon cache; run `rm ~/.cache/icon-cache.kcache` and re-login or restart `plasmashell` |
