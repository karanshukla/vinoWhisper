#!/usr/bin/env bash
# Install or upgrade vinoWhisper from a GitHub release, for this user:
#
#   curl -fsSL https://github.com/karanshukla/vinoWhisper/releases/latest/download/install.sh | bash
#
# Options go after `bash -s --`, e.g. `| bash -s -- --gui`:
#
#   vN          that release instead of the newest
#   anything else is passed to vinowhisper-setup on a first install:
#               --gui (the overlay, tray and settings window), --yes,
#               --device NPU|GPU|CPU, --language ..., --dry-run
#
# It downloads the release's SHA256SUMS, then the wheel and this script, and
# refuses anything not listed there or not matching. The wheel goes in with
# `uv tool install` (uv itself comes from its own release, checked against a
# hash pinned below, if it isn't installed). No git, no build.
#
# A first install then runs vinowhisper-setup, which checks the capture tools
# and NPU driver, exports the model once (that pulls CPU torch, the one
# time), writes the systemd units and, with --gui, fetches the tray and window
# binaries the wheel pins. An upgrade reinstalls the wheel, refreshes the tray
# and window if they're installed, and restarts the server: the model stays.
#
# It keeps a checked copy of itself as vinowhisper-upgrade, which the settings
# window's Update button runs with VINO_UPDATE_TAG set.
#
#   --uninstall removes it: the server units, the tray's menu and login
#               entries, the commands in ~/.local/bin, the models and their
#               caches. Saved sessions, settings and exported documents stay.
#               Downloads nothing. `vinowhisper-upgrade --uninstall`.
#
# From a checkout, scripts/install.sh builds from source instead.

# Everything is in main, called on the last line: piped into bash, a download
# cut short must not run half a script.
set -euo pipefail

REPO="karanshukla/vinoWhisper"
# requires-python is >=3.11,<3.14; OpenVINO's wheels decide the top.
PYTHON="3.13"

# Bumped by hand, in a reviewed commit, with scripts/install.sh's: the binary
# it fetches is run as you. Bump: gh api repos/astral-sh/uv/releases/tags/$UV_VERSION \
#   -q '.assets[]|select(.name|test("linux-musl.tar.gz$"))|"\(.digest) \(.name)"'
UV_VERSION="0.12.20"
UV_SHA256_X86_64="14114d66a094f1907af0fcbc863f34226bbb7f5e430e13dce6676ba40dcc6891"

BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"

say() { printf '== %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

install_uv() {
    [ "$(uname -m)" = x86_64 ] || die "no pinned uv for $(uname -m); install uv (https://docs.astral.sh/uv/) and re-run"
    local name="uv-x86_64-unknown-linux-musl"
    say "Installing uv $UV_VERSION into $BIN"
    curl -fLsS --proto '=https' --tlsv1.2 -o "$WORK/uv.tar.gz" \
        "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$name.tar.gz" \
        || die "could not download uv $UV_VERSION"
    printf '%s  %s\n' "$UV_SHA256_X86_64" "$WORK/uv.tar.gz" | sha256sum --check --status \
        || die "uv $UV_VERSION doesn't match its pinned sha256; installed nothing"
    tar -xzf "$WORK/uv.tar.gz" -C "$WORK"
    install -Dm755 "$WORK/$name/uv" "$BIN/uv"
    install -Dm755 "$WORK/$name/uvx" "$BIN/uvx"
}

# fetch NAME: download a release asset into $WORK and check it against
# SHA256SUMS, where it must be listed exactly once.
fetch() {
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 20 \
        -o "$WORK/$1" "$BASE/$1" || die "download failed: $BASE/$1"
    (cd "$WORK" \
        && awk -v f="$1" '$2 == f || $2 == "./" f || $2 == "*" f { print $1 "  " f }' SHA256SUMS > "want-$1" \
        && [ "$(wc -l < "want-$1")" -eq 1 ] \
        && sha256sum --check --strict --quiet "want-$1") \
        || die "$1 doesn't match the release's SHA256SUMS. Nothing was changed."
}

# The models go; what the user made or chose stays: transcripts and sessions,
# ~/.config/vinowhisper, ~/.local/state/vinowhisper, exported documents, and
# the NPU driver's own cache. Shared tools (uv, ovfetch) stay too.
uninstall() {
    local data="${XDG_DATA_HOME:-$HOME/.local/share}"
    local cache="${XDG_CACHE_HOME:-$HOME/.cache}/vinowhisper"
    local units f uv

    say "Stopping vinoWhisper"
    if [ -x "$BIN/vinowhisper-tray" ]; then
        "$BIN/vinowhisper-tray" quit >/dev/null 2>&1 || true
    fi
    if command -v systemctl >/dev/null; then
        systemctl --user disable --now vinowhisper-server.socket vinowhisper-server.service \
            >/dev/null 2>&1 || true
    fi
    # vinowhisper-setup writes to ~/.config whatever XDG_CONFIG_HOME says.
    for units in "$HOME/.config/systemd/user" "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"; do
        rm -f "$units/vinowhisper-server.service" "$units/vinowhisper-server.socket"
    done
    if command -v systemctl >/dev/null; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        systemctl --user reset-failed vinowhisper-server.service vinowhisper-server.socket \
            >/dev/null 2>&1 || true
    fi

    say "Removing the menu, login and file-type entries"
    if [ -x "$BIN/vinowhisper-tray" ]; then
        "$BIN/vinowhisper-tray" --uninstall >/dev/null || true
    fi
    rm -f "$data"/applications/io.github.karanshukla.vinowhisper*.desktop \
        "${XDG_CONFIG_HOME:-$HOME/.config}"/autostart/io.github.karanshukla.vinowhisper*.desktop

    say "Removing the commands"
    uv="$(command -v uv || true)"
    [ -n "$uv" ] || { [ -x "$BIN/uv" ] && uv="$BIN/uv"; }
    # Into a variable first: under pipefail, grep -q leaving early can fail the pipe (EPIPE).
    local tools=""
    [ -z "$uv" ] || tools="$("$uv" tool list 2>/dev/null || true)"
    if grep -q '^vinowhisper ' <<<"$tools"; then
        "$uv" tool uninstall vinowhisper
    fi
    # A checkout's vinowhisper-setup links its .venv's commands, and bash completion.
    for f in "$BIN"/vinowhisper-* "$HOME"/.local/share/bash-completion/completions/vinowhisper-* \
        "$data"/bash-completion/completions/vinowhisper-*; do
        [ -L "$f" ] || continue
        case "$(readlink "$f")" in
            */.venv/bin/vinowhisper-* | */vinowhisper-completion.bash) rm -f "$f" ;;
        esac
    done
    rm -f "$BIN/vinowhisper-tray" "$BIN/vinowhisper-gui" "$BIN/vinowhisper-upgrade"

    say "Deleting the models and caches"
    rm -rf "$data/vinowhisper/models" "$cache"
    # scripts/install.sh's own clone; kept when it holds work that exists nowhere else.
    local src="$data/vinowhisper/src" why=""
    if [ -d "$src/.git" ]; then
        if [ -n "$(git -C "$src" status --porcelain 2>/dev/null || echo unknown)" ]; then
            why="uncommitted changes"
        elif [ -n "$(git -C "$src" log --branches --not --remotes -1 --format=%h 2>/dev/null || echo unknown)" ]; then
            why="commits that are not on any remote"
        elif [ -n "$(git -C "$src" stash list 2>/dev/null || echo unknown)" ]; then
            why="stashed changes"
        fi
        if [ -z "$why" ]; then
            rm -rf "$src"
        else
            printf '  kept %s: it has %s (delete it yourself once they are saved elsewhere)\n' "$src" "$why"
        fi
    fi
    rmdir "$data/vinowhisper" 2>/dev/null || true

    say "Removed vinoWhisper. Kept your saved sessions in $data/vinowhisper/transcripts and your settings in ${XDG_CONFIG_HOME:-$HOME/.config}/vinowhisper."
}

main() {
    local tag="${VINO_UPDATE_TAG:-}" arg remove=0
    local setup_args=()
    for arg in "$@"; do
        case "$arg" in
            --uninstall) remove=1 ;;
            v[0-9]*) tag="$arg" ;;
            -h|--help) sed -n '2,32p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) setup_args+=("$arg") ;;
        esac
    done
    if [ -n "$tag" ] && ! [[ "$tag" =~ ^v[0-9]+(\.[0-9]+)*$ ]]; then
        die "not a release tag: '$tag'"
    fi
    [ "$(uname -s)" = Linux ] || die "vinoWhisper is Linux-only (PipeWire/PulseAudio + Intel NPU)"
    [ "$(id -u)" -ne 0 ] || die "run this as yourself, not root: vinoWhisper installs into your home"
    if [ "$remove" = 1 ]; then
        uninstall
        return
    fi
    local tool
    for tool in curl tar sha256sum awk; do
        command -v "$tool" >/dev/null || die "missing $tool: install it, then run this again"
    done

    # uv's own Python is a generic glibc build that expects the standard
    # loader path, which NixOS only provides through nix-ld.
    if [ "$(uname -m)" = x86_64 ] && [ ! -e /lib64/ld-linux-x86-64.so.2 ] \
        && grep -qsx 'ID="\{0,1\}nixos"\{0,1\}' /etc/os-release; then
        die "no /lib64/ld-linux-x86-64.so.2: uv's Python can't run on NixOS without nix-ld. Set programs.nix-ld.enable = true; rebuild, then run this again"
    fi

    # An install this script made before: upgrade it rather than set up again.
    local upgrade=0
    [ -x "$BIN/vinowhisper-upgrade" ] && [ -x "$BIN/vinowhisper-setup" ] && upgrade=1

    # Where the assets are: a mirror, or CI's file:// fake release.
    if [ -n "${VINOWHISPER_RELEASE_BASE:-}" ]; then
        BASE="$VINOWHISPER_RELEASE_BASE"
    elif [ -n "$tag" ]; then
        BASE="https://github.com/$REPO/releases/download/$tag"
    else
        BASE="https://github.com/$REPO/releases/latest/download"
    fi
    WORK="$(mktemp -d)"
    # shellcheck disable=SC2064 # expand now
    trap "rm -rf '$WORK'" EXIT

    say "Downloading ${tag:-the latest release} from $BASE"
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 20 \
        -o "$WORK/SHA256SUMS" "$BASE/SHA256SUMS" || die "download failed: $BASE/SHA256SUMS"
    local wheel
    wheel="$(awk '{ sub(/^[*.\/]+/, "", $2); print $2 }' "$WORK/SHA256SUMS" \
        | grep -E '^vinowhisper-[0-9][0-9.]*-py3-none-any\.whl$' | head -1 || true)"
    [ -n "$wheel" ] || die "the release's SHA256SUMS lists no vinowhisper wheel"
    fetch "$wheel"
    fetch install.sh
    echo "checksums verified"

    if ! command -v uv >/dev/null && [ ! -x "$BIN/uv" ]; then
        install_uv
    fi
    export PATH="$BIN:$PATH"

    say "Installing ${wheel%-py3-none-any.whl}"
    # --force replaces an older one in place, at the same path, so the units
    # vinowhisper-setup wrote keep pointing at it. uv's own Python, not a
    # system or Homebrew python3.13 (Tumbleweed, Bluefin): removing that would
    # leave the tool's venv pointing at nothing. The env var, not
    # --managed-python, so an older uv already on PATH still runs it.
    UV_PYTHON_PREFERENCE=only-managed uv tool install --force --python "$PYTHON" "$WORK/$wheel"
    install -Dm755 "$WORK/install.sh" "$BIN/vinowhisper-upgrade"

    if [ "$upgrade" = 1 ]; then
        # The tray and window binaries are pinned in the wheel: fetch the new
        # release's if they're installed.
        if [ -x "$BIN/vinowhisper-tray" ] || [ -x "$BIN/vinowhisper-gui" ]; then
            say "Updating the tray and settings window"
            "$BIN/vinowhisper-setup" --gui --yes </dev/null
        fi
        # A running server keeps the old code; the socket starts the new one.
        systemctl --user try-restart vinowhisper-server.service 2>/dev/null || true
        # The tray's dictation child holds the old Python even when the binary is unchanged.
        if [ -x "$BIN/vinowhisper-tray" ] && "$BIN/vinowhisper-tray" --help 2>/dev/null | grep -q '^  restart '; then
            "$BIN/vinowhisper-tray" restart || true
            say "Updated to ${wheel%-py3-none-any.whl}. Reopen the settings window if it is open."
        else
            say "Updated to ${wheel%-py3-none-any.whl}. Restart the tray and settings window to use it."
        fi
        return
    fi

    say "Handing over to vinowhisper-setup"
    # At a terminal it asks before each step, so it reads the terminal: piped
    # into bash, stdin is this script.
    if [ -t 1 ] && (: </dev/tty) 2>/dev/null; then
        "$BIN/vinowhisper-setup" "${setup_args[@]}" </dev/tty
    else
        "$BIN/vinowhisper-setup" "${setup_args[@]}" </dev/null
    fi
}

main "$@"
