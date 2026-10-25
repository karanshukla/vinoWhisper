#!/usr/bin/env bash
# Install the built wheel into a clean venv and run every entry point's --help.
# Usage: scripts/smoke_wheel.sh dist/vinowhisper-*.whl
#
# --no-deps plus the pure-Python runtime deps: OpenVINO is ~400MB and every
# command imports it lazily, so --help must work without it.
set -euo pipefail

wheel="${1:?usage: smoke_wheel.sh WHEEL}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
venv="$tmp/venv"
uv venv --quiet "$venv"
uv pip install --quiet --python "$venv/bin/python" --no-deps "$wheel"
uv pip install --quiet --python "$venv/bin/python" numpy requests rich flask werkzeug

# Every pin in the source tree must be in the wheel: a dropped one fails quietly.
# Run from the repo root; tray/gui_release.json exist only once release.yml writes them.
for pin in vinowhisper/*.json; do
    "$venv/bin/python" - "$(basename "$pin")" <<'PY'
import sys
from importlib.resources import files

assert files("vinowhisper").joinpath(sys.argv[1]).is_file(), f"the wheel has no {sys.argv[1]}"
PY
done

for name in caption dictate server replay doctor setup; do
    echo "vinowhisper-$name --help"
    "$venv/bin/vinowhisper-$name" --help >/dev/null
done
echo "python -m vinowhisper.integrity --help"
"$venv/bin/python" -m vinowhisper.integrity --help >/dev/null

version="$("$venv/bin/vinowhisper-caption" --version)"
echo "$version"
expected="$("$venv/bin/python" -c 'import importlib.metadata as m; print(m.version("vinowhisper"))')"
[[ "$version" == "vinowhisper $expected" ]] || { echo "version mismatch: $version vs $expected" >&2; exit 1; }
