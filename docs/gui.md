# The caption overlay

`vinowhisper-gui` puts the captions in a small box that floats above every
other window, fullscreen video included. It adds a tray icon and a global
keyboard shortcut. It is optional: the terminal captions need none of it, and
installing it adds nothing to the Python package.

It is a renderer, not a second implementation. It runs `vinowhisper-caption
--json` and draws the events that come back, so capture, stitching, the
server and every fix to them are shared with the terminal UI.

## Install

```bash
vinowhisper-setup --gui
```

That downloads `vinowhisper-gui-x86_64-linux` from the GitHub release matching
your installed vinoWhisper and checks it against the sha256 pinned inside the
Python package. It installs the binary into `~/.local/bin`, adds a launcher
entry, and asks whether to start the tray at login. A plain
`vinowhisper-setup` offers the same step when it runs in a Wayland session.
Nothing needs Rust.

**It is not on PyPI, on purpose.** A Rust GUI has no place inside a Python
wheel, and `pip install vinowhisper` stays the terminal tool alone. The link
between the two is the pin. The release workflow builds the binary, writes its
sha256 into the wheel (`vinowhisper/gui_release.json`), and only then builds
the wheel, so the digest on PyPI and the binary on GitHub come out of one
workflow run.

- **A mismatch installs nothing**, not even a partial file, and fails setup.
- **No pin means no download.** A source checkout has no pin, and neither does
  a wheel built outside the release workflow. There the wizard offers a cargo
  build instead, never an unverified binary. It is the same line
  `model_digests.json` draws for the model export.
- **x86_64 only**, for now. On other architectures the wizard points at cargo.

From a checkout, with a Rust toolchain ([rustup](https://rustup.rs) or your
distro's `cargo` package), either of these builds it locally:

```bash
vinowhisper-setup --gui        # sees the checkout and offers `cargo build`
./scripts/install.sh --gui     # the bootstrap installer, same result
```

Or by hand:

```bash
cargo build --release --locked --manifest-path gui/Cargo.toml
install -Dm755 gui/target/release/vinowhisper-gui ~/.local/bin/vinowhisper-gui
vinowhisper-gui --install --autostart   # launcher, icon, tray at login
```

The release asset is a fully static musl build, 6.0MB, so the same file runs
on any distro's libc. A local `cargo build` is 6.3MB (5.6MB before dictation) and links only libc
(both measured 2026-09-12). Every dependency is pure Rust and compiled in, and
the 135 crates it pulls in are a build-time cost only.

A distro package is not available yet. Fedora cannot package the Python side:
its openvino is 2025.1.0, older than the 2026.3.1 floor, and openvino-genai
and optimum are not packaged (checked 2026-09-12). Packaging the overlay alone
is planned. `vinowhisper-gui --export-desktop DIR` writes the launcher and
icons for a packager, with the launcher naming the `vinowhisper-gui` command
rather than a path, since a buildroot path would be wrong once installed.
`vinowhisper-setup --gui` already recognises a copy in /usr/bin.

It needs `vinowhisper-caption` from the Python package. It looks for it on
`PATH`, then in `~/.local/bin` (where `vinowhisper-setup` links it), then next
to its own binary. `--caption PATH` or `$VINOWHISPER_CAPTION` override the
search.

**The launcher and autostart.** The first run writes a launcher only if there
is none: `--install` may have baked a `--caption` path into one, and a
packaged launcher must not be shadowed by a copy in the home directory. The
icons are the exception, rewritten whenever they differ, since they hold
nothing of yours. The autostart entry runs `--hidden`, so it starts in the
tray without touching the NPU until asked. The tray's "Start at login"
checkmark writes or removes that entry (baking in `--caption` if this instance
was started with one); re-running `--install` without `--autostart` also
turns it off.

## Using it

Launch it from the app menu or run `vinowhisper-gui`. The box appears at the
bottom of the screen with a status line (device, lag, silence) above two lines
of captions. Words still waiting on a second cycle to confirm them are shown
dimmed, the same rule the terminal uses. They matter more here: with only two
lines, showing nothing until two cycles agree reads as a frozen caption rather
than one still being decided.

The box is dark and mostly opaque, because legibility over a white video frame
matters more than seeing the frame through it, and about 70 characters wide, a
comfortable reading measure. The newest lines sit at the bottom and older text
leaves at the top. On a device below the NPU the whole frame gets a red
border, the terminal's rule carried over.

The box ignores the mouse completely: clicks go straight through to whatever
is underneath, so it never blocks the video controls it sits over. It is
driven from three places instead:

| | |
|---|---|
| **Shortcut** | Meta+Alt+C by default. Shows or hides the captions. Meta+H is [dictation](#dictation) |
| **Tray icon** | The app's own mark, drawn in Breeze's style (see [The icon](#the-icon)). Left click does the same. The menu has Listen to (system audio or microphone), Position (bottom or top), Text size, Change shortcut… and Quit |
| **Command** | `vinowhisper-gui show`, `hide`, `toggle`, `dictate`, `quit`, sent to the running instance |

**Hidden means stopped.** Hiding the box also stops the caption process. A
hidden box that kept transcribing would keep the server from ever idling out,
and scale-to-zero is why the server is socket-activated at all
([architecture.md](architecture.md)). Showing it again starts a fresh
session, which also retries after an error.

After 30 minutes with the captions hidden and no dictation, the tray icon
reports itself Passive, which Plasma moves into the hidden icons behind the
panel's arrow. Showing captions or dictating brings it back. Same mechanism
as waydroid-tray; measured flipping both ways over D-Bus on 2026-09-21. The
delay is `tray_idle_minutes` in `gui.json`, and 0 keeps the icon in view.
It is read at startup, so restart the overlay after changing it.

Tray choices are remembered in `~/.config/vinowhisper/gui.json`. An
unreadable one is reported and ignored, and every field has a default, so a
bad settings file never stops captions from starting. The top position is for
video that burns its own subtitles into the bottom of the frame.

### The shortcut

Wayland does not let an app grab a key while another window has focus, so the
shortcut goes through the desktop portal: the app asks for one, the desktop
decides. On Plasma a dialog asks you to confirm it the first time. You can
also pick a different key right there. After that the binding lives in the
desktop's own settings, like any other shortcut. Change it with
**Change shortcut…** in the tray menu, which opens that settings page, or in
System Settings > Shortcuts, where it is listed under *vinoWhisper Captions*.

The preferred trigger only matters the first time. It is `shortcut` in
`gui.json`, in the XDG shortcuts syntax (`LOGO+ALT+C`). Editing it later does
not move a binding that already exists; rebind it in the desktop instead.

**The portal needs a launcher.** Measured 2026-09-12 on Plasma 6.7: with no
`.desktop` file, the portal refuses the app id ("App info not found"), and
the shortcut portal then refuses the shortcut ("An app id is required"). So
the first run writes the launcher itself if `--install` never did. With no
portal at all, bind `vinowhisper-gui toggle` in your desktop's keyboard
settings; it reaches the running instance the same way.

**Change shortcut…** needs version 2 of the shortcut portal, where
ConfigureShortcuts first appears. On version 1 the binding is still an
ordinary desktop shortcut, so change it in System Settings instead.

## Dictation

Added 2026-09-21, from [issue #12](https://github.com/karanshukla/vinoWhisper/issues/12).
The overlay also types what you say into whatever window has focus.

- **Hold** the dictation key, talk, release: the text is typed on release.
- **Tap** it (shorter than 350ms) to start hands-free, talk, tap again to finish.

It asks the portal for **Meta+H**, which is what the dictation key on this
laptop's F-row sends, after Windows' Win+H. A small pill shows what it is
doing: a level meter while listening, then "Transcribing…", then the text it
typed. It sits above the caption box when that is showing, and works with the
captions hidden. Without a shortcuts portal, bind `vinowhisper-gui dictate` to a
key: it behaves as a tap. For hold-to-talk there, bind `vinowhisper-gui
dictate-press` to the key's press and `vinowhisper-gui dictate-release` to its
release (sway: `bindsym` and `bindsym --release`; Hyprland has the portal).

Rebinding works like the captions shortcut: **Change shortcut…** or System
Settings, listed as *Dictate*. Measured 2026-09-21: adding Meta+J beside
Meta+H took effect immediately, KDE sent `ShortcutsChanged` with
"Meta+H, Meta+J", hold-to-talk worked on the new key, and the tray names both.

One utterance is one decode, so none of the caption stitching applies: 29.5s
at most (it stops and types by itself there), about 0.5-0.8s from release to
text on the NPU, measured 2026-09-21.

**How the text gets there.** Wayland lets no app type into another, so it
copies the text and presses Shift+Insert:

1. The text goes on the clipboard *and* the primary selection, through
   `ext-data-control-v1`. Both, because Shift+Insert pastes the primary
   selection in terminals (Ghostty binds it to `paste_from_selection`).
2. Shift+Insert rather than Ctrl+V: it pastes in terminals and GUI apps
   alike, and uses evdev codes that do not move with the keyboard layout.
3. The keys come from a virtual keyboard on `/dev/uinput` when the user can
   open it; else from the compositor's own virtual keyboard
   (`zwp_virtual_keyboard_v1`, which wlroots compositors, niri, COSMIC and
   KWin offer to any client); else from the remote-desktop portal.
4. The keys go out only once the compositor confirms it has the new
   selection (a `wl_display.sync` round trip). Without that, uinput is fast
   enough to paste the *previous* clipboard, which it did on 2026-09-21.
5. Once the paste has read the text, both selections are cleared: 300ms after
   the last read, never sooner than 500ms after the keys, and at 2s whether
   or not anything read it. The text is also offered with
   `x-kde-passwordManagerHint: secret`, so Klipper never records it, and
   Klipper then puts back whatever you had copied before (measured
   2026-09-21). Without a clipboard manager the clipboard is left empty.

**Why uinput first.** Measured 2026-09-21 on Plasma 6.7: the portal works,
and after the first permission dialog a saved restore token starts every
later session silently (0.00s, three of three; KDE issues a new token each
time, so it is saved after every start, in
`~/.local/state/vinowhisper/remote-desktop.token`). But KDE posts "Remote
control session started" for every session and shows a Remote Control tray
icon while one is open, which is a notification per dictation. `/dev/uinput`
has no dialog and no notification. Whether you can open it depends on udev:
on this laptop Steam's `60-steam-input.rules` grants it. The log says which
path is in use.

**The compositor's virtual keyboard, added 2026-09-22, is for everything
that is not KDE with Steam.** `zwp_virtual_keyboard_v1` needs no udev rule
and no portal: on the first paste that needs it, the overlay hands the
compositor a two-key keymap (Shift and Insert, at their evdev codes plus 8,
with a `modifier_map` so Shift really sets Shift) and sends the four key
events on the same Wayland connection that set the clipboard, so they cannot
overtake it. The keymap was compiled
through libxkbcommon before shipping; it has not been tried against a live
compositor yet. It sits between uinput and the portal because the uinput
path is the one measured on hardware, and on Sway, niri and COSMIC there is
no remote-desktop portal to fall back to. A compositor that restricts the
protocol (Hyprland's `ecosystem:enforce_permissions`, off by default) may
answer a protocol error, which ends the Wayland connection; none of the
desktops in the table below do so out of the box.

**Where the text goes is not checked.** Wayland does not say what has focus,
so it pastes into whatever does. If typing failed outright, the text is left
on the clipboard and the pill says so; after a paste that went to the wrong
window it is gone, and the pill is the only place it still shows.

The microphone is recorded by `vinowhisper-dictate --json`, which the
overlay keeps running idle (reading its stdin, not the microphone) so that
Python's imports (270ms) are not paid when the key goes down. It starts
`pw-record` on the key, which delivers audio 90-290ms later, and keeps
recording 250ms past the release so a key let go on the last syllable does
not cut it off. The key also wakes the server, so a cold model load overlaps
the speech.

## Where it works

The box is a **wlr-layer-shell** surface on the *overlay* layer. That is what
lets it sit above fullscreen windows without a window rule or keep-above
hint, and it is a Wayland protocol that not every compositor offers:

| Desktop | Overlay | Tray | Shortcut | Dictation types via | Status |
|---|---|---|---|---|---|
| KDE Plasma 6.7 (Wayland) | yes | yes | portal | uinput, then virtual keyboard, then portal | **Tested on hardware, 2026-09-12; dictation 2026-09-21** |
| Hyprland | layer-shell: yes | needs an SNI tray | portal | virtual keyboard | untested |
| Sway, niri, COSMIC | layer-shell: yes | needs an SNI tray | none: bind `dictate-press`/`dictate-release` | virtual keyboard | untested |
| GNOME | **no layer-shell** | AppIndicator extension | portal (48+) | no data-control, so no paste | will not start; use the terminal |
| X11 sessions | no | | | | will not start; use the terminal |

The clipboard half needs `ext-data-control-v1`: KWin 6.3, wlroots 0.19 (Sway
1.11), Hyprland 0.49, niri 25.02 and COSMIC all have it; older ones offer
only the `wlr-` version, which the overlay does not speak. GNOME has neither,
which is what rules it out for dictation independently of layer-shell.

On a compositor without layer-shell it exits with a message that says so,
rather than falling back to a normal window that cannot stay on top.
`vinowhisper-caption` in a terminal works everywhere.

It renders at the display's fractional scale (`wp_fractional_scale_v1` with
viewporter), so text stays sharp at 125% or 150%. A compositor missing either
gets the next whole-number scale shrunk down, which shows as slightly soft
text. It lands on whichever output the compositor picks, usually the focused
one, keeps clear of panels rather than sliding under them, and is recreated if
the compositor withdraws it (usually because its output went away). It cannot
be dragged; Position is top or bottom only.

## How it fits together

```
vinowhisper-gui ──spawns──> vinowhisper-caption --json ──HTTP──> vinowhisper-server (NPU)
      │   ▲                          │
      │   └──── one JSON event per line on stdout
      ├──spawns──> vinowhisper-dictate --json ──HTTP──> (same server)
      │              ▲ start / stop / cancel on stdin
      ├── tray (StatusNotifierItem over D-Bus)
      ├── shortcuts (xdg-desktop-portal GlobalShortcuts: Activated and Deactivated)
      ├── paste: ext-data-control, then Shift+Insert via /dev/uinput,
      │          zwp_virtual_keyboard_v1, or RemoteDesktop
      └── $XDG_RUNTIME_DIR/vinowhisper-gui.sock (show/hide/toggle/dictate/quit)
```

- **The wire format is `events.to_dict`, verbatim.** `--json` writes one
  ASCII-escaped object per event, plus an `Error` record when the CLI gives
  up, since a GUI has no terminal to show stderr on.
  `tests/test_caption.py` names every field `gui/src/protocol.rs` reads, so
  renaming one fails a Python test rather than leaving the overlay silently
  blank.
- **Only the fields it draws are read.** Extra fields are ignored, and an
  unknown record or a stray line on stdout costs that one line rather than
  the session, so the Python events can grow without a new GUI release. The
  fixtures in `protocol.rs` were captured from `JsonRenderer`, not written by
  hand, so they pin the real wire format.
- **The surface spans the whole output width** and the box is centred in it.
  Being click-through makes the empty sides free, and no output size has to be
  known before the compositor picks an output.
- **Redraws follow what is on screen, not events.** The caption process sends
  an event every half second even in silence, and one that changes nothing
  visible draws nothing, since every frame also makes the compositor repaint.
  The once-a-second timer runs only while "Starting… Ns" is showing, so a
  hidden overlay has no timer at all. The tray is updated only when its menu
  or tooltip actually changed, since each update is a D-Bus round trip.
- **The tray decides nothing.** Each choice goes to the main loop as a command,
  and the main loop answers with a fresh view. Registration is assumed rather
  than checked, since at login the overlay can start before Plasma's tray.
- **Stopping is Ctrl+C.** The GUI sends SIGINT, so the Python side flushes its
  last pending words and stops `pw-record` exactly as it would in a terminal.
  It gets 4s before SIGKILL. The child also carries a parent-death signal, so
  a crashed or killed GUI never leaves captioning running with no one
  watching.
- **One instance per session.** A second launch sends its command to the
  first over the socket and exits, which is how `vinowhisper-gui toggle`
  works from any keybinding tool, and why a double-clicked launcher never
  starts two. A socket file left by a killed instance is detected by trying to
  connect, and `--hidden` only pings, so an autostart that finds an instance
  running leaves it alone.

## Why Rust

Asked for 2026-09-12: something native and fast that installs without a pile
of dependencies. The first candidate was PySide6. `PySide6-Essentials` is only
two wheels, but measured 232MB installed. System PyGObject cannot be imported
from the project's Python 3.13 venv (Fedora 44's is built for 3.14). Tkinter
has neither a tray nor Wayland. The Rust binary is 6.3MB, links only libc,
and draws its own text with no GPU context, since it redraws a couple of times
a second at most. The shapes are hand-drawn too: antialiased rounded
rectangles and circles from a signed-distance function, into Wayland's
premultiplied ARGB8888 buffer. That is all a caption box and a tray icon need,
not enough to justify a 2D graphics library.

## The icon

The mark is the overlay itself: three voice bars in the listening dot's green
become a word, above a confirmed line that ends in a pending one, in the box's
own colour. It is drawn as data in `gui/src/icon.rs`. The launcher SVG, the
tray's fallback pixmaps and the copies in `docs/assets/` all come from it;
`VINOWHISPER_BLESS_ICONS=1 cargo test` regenerates the assets, and a test fails
when they are stale.

The tray version is the same composition redrawn on Breeze's 16px grid in
one-pixel lines, so it sits with the panel's other icons. Its outline and text
follow the panel's text colour through KDE's `current-color-scheme`
stylesheet, while the voice bars stay green so the tray matches the start
menu. The outline is the box minus its inset under the even-odd rule rather
than a stroke, so it lands on whole pixels.

## Troubleshooting

Run it from a terminal to see its log. Lines from the caption process are
prefixed `[caption]`.

| Symptom | Cause |
|---|---|
| "no global shortcut (... An app id is required)" | No launcher. It writes one on first run; if it could not, run `vinowhisper-gui --install` |
| "vinowhisper-caption was not found" | Not on `PATH` for a desktop-launched app. `vinowhisper-gui --install --caption /path/to/vinowhisper-caption` bakes the path into the launcher |
| Box says "not reachable" | The server, not the GUI: `vinowhisper-doctor` |
| No tray icon | No StatusNotifierItem host (GNOME without the AppIndicator extension). The shortcut and commands still work |
| "does not offer wlr-layer-shell" | GNOME or X11. See the table above |
| Box shows raw error text | The caption process exited without an `Error` record, so the box shows its last stderr lines. Run `vinowhisper-caption` in a terminal for the rest |
| Text in an unexpected font | fontconfig names a sans-serif that is not installed. It falls back to the first installed of Inter, Noto Sans, Cantarell, Ubuntu, DejaVu Sans and Liberation Sans |
| Dictation: "vinowhisper-dictate was not found" | Older install. `pip install -U vinowhisper`, then `vinowhisper-setup` links it |
| Dictation: a notification every time | No `/dev/uinput` access, so it types through the portal. A udev rule giving your user `/dev/uinput` (as Steam's does) stops it |
| Dictation: "On the clipboard, not typed" | The paste was refused or failed; the text is on the clipboard. After refusing the portal dialog, restart the overlay to be asked again |
| Dictation: a press does nothing | A press while the last one is still being transcribed is ignored. `VINOWHISPER_GUI_TRACE=1 vinowhisper-gui` logs every key and state change |
| Old icon after an upgrade | Plasma's icon cache. `rm ~/.cache/icon-cache.kcache`, then log out and in or restart plasmashell |
