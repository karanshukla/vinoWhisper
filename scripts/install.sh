#!/usr/bin/env bash
# vinoWhisper bootstrap installer.
#
#   curl -fsSL https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/scripts/install.sh | bash
#
# or, from a checkout:
#
#   ./scripts/install.sh [--dir DIR] [--ref REF] [--yes] [--dry-run] [--gui]
#
# What it does and why: docs/install.md
set -euo pipefail

# Bumped by hand, in a reviewed commit: the installer it fetches is run as you.
UV_VERSION="0.12.20"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m error\033[0m %s\n' "$*" >&2; exit 1; }

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '   would run: %s\n' "$*"
        return 0
    fi
    "$@"
}

usage() {
    cat <<'EOF'
vinoWhisper installer.

  --dir DIR     where to keep the checkout (default ~/.local/share/vinowhisper/src)
  --ref REF     branch or tag to install (default: the latest vX.Y.Z release tag;
                --ref main for unreleased code)
  --yes         pass --yes to vinowhisper-setup: no prompts, sudo included
  --dry-run     print what would happen, change nothing
  --no-setup    stop after `uv sync`, do not run the setup wizard
  --gui         also build the overlay: captions, dictation, tray (needs cargo)
EOF
}

# git, not the GitHub API: no rate limit, no JSON, and it honours VINOWHISPER_REPO.
latest_release() {
    git ls-remote --tags --refs "$1" 'v*' \
        | awk '{ sub("refs/tags/", "", $2); print $2 }' \
        | { grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' || true; } \
        | sort -V \
        | tail -n 1
}

# Empty when piped into bash: there is no checkout to use, only this text.
own_checkout() {
    local script="${BASH_SOURCE[0]:-}"
    [[ -n "$script" && -f "$script" ]] || return 1
    local root
    root="$(cd "$(dirname "$script")/.." && pwd)"
    grep -q '^name = "vinowhisper"' "$root/pyproject.toml" 2>/dev/null || return 1
    printf '%s\n' "$root"
}

# All in one function, called on the last line, so a download cut short runs nothing.
main() {
    REPO_URL="${VINOWHISPER_REPO:-https://github.com/karanshukla/vinoWhisper}"
    INSTALL_DIR="${VINOWHISPER_DIR:-$HOME/.local/share/vinowhisper/src}"
    REF=""
    SETUP_ARGS=()
    DRY_RUN=0
    RUN_SETUP=1
    GUI=0

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --gui)      GUI=1; shift ;;
            --dir)      INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
            --ref)      REF="${2:?--ref needs a branch or tag}"; shift 2 ;;
            --yes|-y)   SETUP_ARGS+=(--yes); shift ;;
            --dry-run|-n) DRY_RUN=1; SETUP_ARGS+=(--dry-run); shift ;;
            --no-setup) RUN_SETUP=0; shift ;;
            -h|--help)  usage; exit 0 ;;
            *) die "unknown argument: $1 (try --help)" ;;
        esac
    done

    # --- sanity -----------------------------------------------------------

    [[ "$(uname -s)" == "Linux" ]] || die "vinoWhisper is Linux-only (PipeWire/PulseAudio + Intel NPU)."

    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        say "Detected ${PRETTY_NAME:-$ID}"
    else
        warn "no /etc/os-release; the setup wizard will use generic package advice"
    fi

    if ! grep -qi 'intel' /proc/cpuinfo 2>/dev/null; then
        warn "this does not look like an Intel CPU — there will be no NPU, and the"
        warn "CPU fallback is much slower than the design assumes"
    fi

    # --- uv ---------------------------------------------------------------

    if command -v uv >/dev/null 2>&1; then
        say "uv $(uv --version 2>/dev/null | awk '{print $2}') already installed"
    else
        say "Installing uv $UV_VERSION (https://astral.sh/uv)"
        command -v curl >/dev/null 2>&1 || die "curl is required to install uv"
        run sh -c "curl -LsSf https://astral.sh/uv/$UV_VERSION/install.sh | sh"
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        command -v uv >/dev/null 2>&1 || [[ $DRY_RUN -eq 1 ]] || die "uv installed but not on PATH"
    fi

    # --- checkout ---------------------------------------------------------

    local checkout=""
    if [[ ! -d "$INSTALL_DIR/.git" ]]; then
        checkout="$(own_checkout || true)"
    fi

    if [[ -z "$checkout" && -z "$REF" ]]; then
        command -v git >/dev/null 2>&1 || die "git is required"
        REF="$(latest_release "$REPO_URL")" \
            || die "could not list the release tags of $REPO_URL; pass --ref vX.Y.Z (or --ref main)"
        [[ -n "$REF" ]] || die "$REPO_URL has no vX.Y.Z release tag; pass --ref"
        say "Installing $REF, the latest release (--ref main for unreleased code)"
    fi

    if [[ -n "$checkout" ]]; then
        INSTALL_DIR="$checkout"
        say "Using this checkout at $INSTALL_DIR"
    elif [[ -d "$INSTALL_DIR/.git" ]]; then
        say "Updating checkout at $INSTALL_DIR to $REF"
        # FETCH_HEAD, so a tag and a branch update the same way, tags included.
        run git -C "$INSTALL_DIR" fetch --quiet origin "$REF"
        run git -C "$INSTALL_DIR" -c advice.detachedHead=false checkout --quiet FETCH_HEAD
    else
        command -v git >/dev/null 2>&1 || die "git is required"
        say "Cloning $REPO_URL ($REF) into $INSTALL_DIR"
        run mkdir -p "$(dirname "$INSTALL_DIR")"
        run git -c advice.detachedHead=false clone --quiet --branch "$REF" "$REPO_URL" "$INSTALL_DIR"
    fi

    # --- environment ------------------------------------------------------

    say "Building the environment (uv sync)"
    say "  This pulls the OpenVINO wheels; expect a few GB and a few minutes."
    # --locked: install exactly what uv.lock pins, and fail rather than re-resolve.
    run uv sync --locked --extra export --project "$INSTALL_DIR"

    # --- desktop overlay (optional) ----------------------------------------

    if [[ $GUI -eq 1 ]]; then
        if command -v cargo >/dev/null 2>&1; then
            say "Building the desktop overlay (a few minutes the first time)"
            run cargo build --release --locked --manifest-path "$INSTALL_DIR/gui/Cargo.toml"
            run install -Dm755 "$INSTALL_DIR/gui/target/release/vinowhisper-gui" \
                "$HOME/.local/bin/vinowhisper-gui"
            # The shortcut portal needs the launcher entry
            run "$HOME/.local/bin/vinowhisper-gui" --install --autostart
        else
            warn "--gui needs a Rust toolchain (cargo), which is not installed:"
            warn "  https://rustup.rs, or your distro's rust/cargo package, then re-run with --gui"
        fi
    fi

    # --- hardware-specific setup -------------------------------------------

    if [[ $RUN_SETUP -eq 0 ]]; then
        say "Skipping the setup wizard (--no-setup)."
        say "Run it later with: uv run --project $INSTALL_DIR vinowhisper-setup"
        exit 0
    fi

    say "Handing over to vinowhisper-setup"
    run uv run --project "$INSTALL_DIR" vinowhisper-setup "${SETUP_ARGS[@]}"
}

main "$@"
