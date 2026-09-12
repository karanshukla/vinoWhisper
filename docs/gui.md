# The caption overlay

`vinowhisper-gui` puts the captions in a small box that floats above every
other window, fullscreen video included. It adds a tray icon and a global
keyboard shortcut. It is optional: the terminal captions need none of it, and
installing it adds nothing to the Python package.

It is a renderer, not a second implementation. It runs `vinowhisper-caption
--json` and draws the events that come back, so capture, stitching, the
server and every fix to them are shared with the terminal UI.

## Install

From a checkout, with a Rust toolchain ([rustup](https://rustup.rs) or your
distro's `cargo` package):

```bash
./scripts/install.sh --gui
```

Or by hand:

```bash
cargo build --release --locked --manifest-path gui/Cargo.toml
install -Dm755 gui/target/release/vinowhisper-gui ~/.local/bin/vinowhisper-gui
vinowhisper-gui --install --autostart   # launcher, icon, tray at login
```

What you get is one 5.6MB binary that links nothing beyond libc (measured
2026-09-12, x86_64 release build). Every dependency is pure Rust and compiled
in, so there is nothing to install alongside it on any distro. The 135 crates
it pulls in are a build-time cost only.

It needs `vinowhisper-caption` from the Python package. It looks for it on
`PATH`, then in `~/.local/bin` (where `vinowhisper-setup` links it), then next
to its own binary. `--caption PATH` or `$VINOWHISPER_CAPTION` override the
search.

## Using it

Launch it from the app menu or run `vinowhisper-gui`. The box appears at the
bottom of the screen with a status line (device, lag, silence) above two lines
of captions. Words still waiting on a second cycle to confirm them are shown
dimmed, the same rule the terminal uses.

The box ignores the mouse completely: clicks go straight through to whatever
is underneath, so it never blocks the video controls it sits over. It is
driven from three places instead:

| | |
|---|---|
| **Shortcut** | Meta+Alt+C by default. Shows or hides the captions |
| **Tray icon** | Left click does the same. The menu has Listen to (system audio or microphone), Position (bottom or top), Text size, Change shortcut… and Quit |
| **Command** | `vinowhisper-gui show`, `hide`, `toggle`, `quit`, sent to the running instance |

**Hidden means stopped.** Hiding the box also stops the caption process. A
hidden box that kept transcribing would keep the server from ever idling out,
and scale-to-zero is why the server is socket-activated at all
([architecture.md](architecture.md)). Showing it again starts a fresh
session, which also retries after an error.

Tray choices are remembered in `~/.config/vinowhisper/gui.json`.

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

## Where it works

The box is a **wlr-layer-shell** surface on the *overlay* layer. That is what
lets it sit above fullscreen windows without a window rule or keep-above
hint, and it is a Wayland protocol that not every compositor offers:

| Desktop | Overlay | Tray | Shortcut | Status |
|---|---|---|---|---|
| KDE Plasma 6.7 (Wayland) | yes | yes | portal | **Tested on hardware, 2026-09-12** |
| Sway, Hyprland, niri, COSMIC | layer-shell: yes | needs an SNI tray | varies | untested |
| GNOME | **no layer-shell** | AppIndicator extension | portal (48+) | will not start; use the terminal |
| X11 sessions | no | | | will not start; use the terminal |

On a compositor without layer-shell it exits with a message that says so,
rather than falling back to a normal window that cannot stay on top.
`vinowhisper-caption` in a terminal works everywhere.

It renders at the display's fractional scale (`wp_fractional_scale_v1`), so
text stays sharp at 125% or 150%. It lands on whichever output the compositor
picks, usually the focused one. It cannot be dragged; Position is top or
bottom only.

## How it fits together

```
vinowhisper-gui ──spawns──> vinowhisper-caption --json ──HTTP──> vinowhisper-server (NPU)
      │   ▲                          │
      │   └──── one JSON event per line on stdout
      ├── tray (StatusNotifierItem over D-Bus)
      ├── shortcut (xdg-desktop-portal GlobalShortcuts)
      └── $XDG_RUNTIME_DIR/vinowhisper-gui.sock (show/hide/toggle/quit)
```

- **The wire format is `events.to_dict`, verbatim.** `--json` writes one
  ASCII-escaped object per event, plus an `Error` record when the CLI gives
  up, since a GUI has no terminal to show stderr on.
  `tests/test_caption.py` names every field `gui/src/protocol.rs` reads, so
  renaming one fails a Python test rather than leaving the overlay silently
  blank.
- **Stopping is Ctrl+C.** The GUI sends SIGINT, so the Python side flushes its
  last pending words and stops `pw-record` exactly as it would in a terminal.
  It gets 4s before SIGKILL. The child also carries a parent-death signal, so
  a crashed or killed GUI never leaves captioning running with no one
  watching.
- **One instance per session.** A second launch sends its command to the
  first over the socket and exits, which is how `vinowhisper-gui toggle`
  works from any keybinding tool.

## Why Rust

Asked for 2026-09-12: something native and fast that installs without a pile
of dependencies. The first candidate was PySide6. `PySide6-Essentials` is only
two wheels, but measured 232MB installed. System PyGObject cannot be imported
from the project's Python 3.13 venv (Fedora 44's is built for 3.14). Tkinter
has neither a tray nor Wayland. The Rust binary is 5.6MB, links only libc,
and draws its own text with no GPU context, since it redraws a couple of times
a second at most.

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
