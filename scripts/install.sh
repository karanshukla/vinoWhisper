#!/usr/bin/env bash
# vinoWhisper bootstrap installer.
#
#   curl -fsSL https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/scripts/install.sh | bash
#
# or, from a checkout:
#
#   ./scripts/install.sh [--dir DIR] [--ref BRANCH] [--yes] [--dry-run]
#
# What it does, in order:
#
#   1. finds (or installs) uv, since the Python <3.14 pin and the OpenVINO
#      floor both live in pyproject.toml and only uv reads them
#   2. clones or updates the checkout
#   3. `uv sync`, which builds the environment against those pins
#   4. hands over to `vinowhisper-setup`, which does the parts that need to
#      look at your actual hardware: capture tool, NPU driver, model export,
#      systemd units, PATH symlinks
#   5. with --gui only: builds the caption overlay (gui/, Rust) and installs
#      it into ~/.local/bin with a launcher and a login autostart. Optional
#      on purpose; the terminal captions need none of it.
#
# Step 4 is where every distro-specific decision happens, and it prompts
# before running anything. This script deliberately does not install system
# packages itself — it does not know what you have, and the wizard does.
#
# `pip install vinowhisper` also works, and has since 2026-08-31, when stable
# OpenVINO 2026.3.1 could finally build the NPU static Whisper pipeline and the
# nightly pin came out (docs/install.md). This script is for everyone who would
# rather not assemble the rest by hand: a pip install gets the commands, not a
# model export, an NPU driver or systemd units.
set -euo pipefail

REPO_URL="${VINOWHISPER_REPO:-https://github.com/karanshukla/vinoWhisper}"
INSTALL_DIR="${VINOWHISPER_DIR:-$HOME/.local/share/vinowhisper/src}"
REF="main"
SETUP_ARGS=()
DRY_RUN=0

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
  --ref REF     branch or tag to install (default main)
  --yes         pass --yes to vinowhisper-setup: no prompts, sudo included
  --dry-run     print what would happen, change nothing
  --no-setup    stop after `uv sync`, do not run the setup wizard
  --gui         also build the caption overlay and tray icon (needs cargo)
EOF
}

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

# --- sanity ---------------------------------------------------------------

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

# --- uv -------------------------------------------------------------------

if command -v uv >/dev/null 2>&1; then
    say "uv $(uv --version 2>/dev/null | awk '{print $2}') already installed"
else
    say "Installing uv (https://astral.sh/uv)"
    command -v curl >/dev/null 2>&1 || die "curl is required to install uv"
    run sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 || [[ $DRY_RUN -eq 1 ]] || die "uv installed but not on PATH"
fi

# --- checkout -------------------------------------------------------------

if [[ -d "$INSTALL_DIR/.git" ]]; then
    say "Updating checkout at $INSTALL_DIR"
    run git -C "$INSTALL_DIR" fetch --quiet origin "$REF"
    run git -C "$INSTALL_DIR" checkout --quiet "$REF"
    run git -C "$INSTALL_DIR" pull --quiet --ff-only origin "$REF"
elif [[ -f "$(dirname "$0")/../pyproject.toml" ]]; then
    # Run from inside a checkout: use it rather than cloning a second one.
    INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
    say "Using this checkout at $INSTALL_DIR"
else
    command -v git >/dev/null 2>&1 || die "git is required"
    say "Cloning $REPO_URL into $INSTALL_DIR"
    run mkdir -p "$(dirname "$INSTALL_DIR")"
    run git clone --quiet --branch "$REF" "$REPO_URL" "$INSTALL_DIR"
fi

# --- environment ----------------------------------------------------------

say "Building the environment (uv sync)"
say "  This pulls the OpenVINO wheels; expect a few GB and a few minutes."
run uv sync --project "$INSTALL_DIR"

# --- caption overlay (optional) --------------------------------------------

if [[ $GUI -eq 1 ]]; then
    if command -v cargo >/dev/null 2>&1; then
        say "Building the caption overlay (a few minutes the first time)"
        run cargo build --release --locked --manifest-path "$INSTALL_DIR/gui/Cargo.toml"
        run install -Dm755 "$INSTALL_DIR/gui/target/release/vinowhisper-gui" \
            "$HOME/.local/bin/vinowhisper-gui"
        # Launcher, icon and login autostart. The launcher is also what the
        # desktop's shortcut portal needs before it will grant a shortcut.
        run "$HOME/.local/bin/vinowhisper-gui" --install --autostart
    else
        warn "--gui needs a Rust toolchain (cargo), which is not installed:"
        warn "  https://rustup.rs, or your distro's rust/cargo package, then re-run with --gui"
    fi
fi

# --- hardware-specific setup ---------------------------------------------

if [[ $RUN_SETUP -eq 0 ]]; then
    say "Skipping the setup wizard (--no-setup)."
    say "Run it later with: uv run --project $INSTALL_DIR vinowhisper-setup"
    exit 0
fi

say "Handing over to vinowhisper-setup"
run uv run --project "$INSTALL_DIR" vinowhisper-setup "${SETUP_ARGS[@]}"
