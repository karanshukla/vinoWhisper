#!/usr/bin/env bash
# The installed Flatpak, run: every command starts, the bundled Python loads OpenVINO and
# finds the CPU, and a client starts the server in a sandbox of its own and stops it.
# flatpak.yml runs this on the bundle it built; it needs a session bus, no NPU and no model.
#
#   dbus-run-session -- packaging/flatpak/smoke.sh [APP] [VERSION]
set -euo pipefail

APP="${1:-io.github.karanshukla.vinowhisper}"
VERSION="${2:-}"

run() {
    local command="$1"
    shift
    flatpak run --command="$command" "$APP" "$@"
}

for name in caption dictate server replay doctor setup cleanup session canary whisper \
    transcribe flatpak-update; do
    echo "vinowhisper-$name --help"
    run "vinowhisper-$name" --help >/dev/null
done
echo "vinowhisper-gui --help"
run vinowhisper-gui --help >/dev/null

tray="$(run vinowhisper-tray --version)"
python="$(run vinowhisper-caption --version)"
gui="$(run vinowhisper-gui --version)"
echo "$tray, $gui, $python"
[ "${tray#vinowhisper-tray }" = "${python#vinowhisper }" ] || {
    echo "the tray and the Python package are different versions" >&2
    exit 1
}
[ "${gui#vinowhisper-gui }" = "${python#vinowhisper }" ] || {
    echo "the settings window and the Python package are different versions" >&2
    exit 1
}
if [ -n "$VERSION" ] && [ "$python" != "vinowhisper $VERSION" ]; then
    echo "the $VERSION bundle's Python package is $python" >&2
    exit 1
fi
if [ -n "$VERSION" ] && [ "$tray" != "vinowhisper-tray $VERSION" ]; then
    echo "the $VERSION bundle is $tray" >&2
    exit 1
fi

run /app/lib/vinowhisper/python/bin/python3 - <<'PY'
import sys

import openvino
import openvino_genai
import openvino_tokenizers  # noqa: F401

from vinowhisper import config, sandbox

assert sys.version_info[:2] == (3, 13), sys.version
assert sandbox.flatpak_id(), "not in a sandbox"
assert f"/app/{sandbox.flatpak_id()}/" in str(config.server_socket()), config.server_socket()
devices = openvino.Core().available_devices
print(f"python {sys.version.split()[0]}, openvino {openvino.__version__}, "
      f"genai {openvino_genai.__version__}, devices {devices}")
assert "CPU" in devices, devices
PY

# The entries Flatpak exported for the host's menu, and the session file type.
exports="$(flatpak info --show-location "$APP")/export/share"
for file in "applications/$APP.desktop" "applications/$APP.settings.desktop" \
    "mime/packages/$APP.xml" "icons/hicolor/scalable/apps/$APP.svg"; do
    [ -f "$exports/$file" ] || { echo "not exported: $file" >&2; exit 1; }
done
grep -q "^Exec=.*flatpak run .*--command=vinowhisper-tray" "$exports/applications/$APP.desktop"

# A client finds nobody listening and starts the server in a sandbox of its own. With no
# model (a CI runner) it says so in its log and exits, which the client sees as a dropped
# connection; with one it answers. Either way it started, and --stop leaves none running.
run /app/lib/vinowhisper/python/bin/python3 - <<'PY'
import requests

from vinowhisper import client, config

try:
    health = client.TranscriptionClient().wait_ready()
    print(f"the server answered, on {health.get('device')}")
except requests.RequestException:
    print("the server started and found no model")
log = config.SERVER_LOG.read_text(encoding="utf-8")
assert "[vinowhisper-server] loading" in log, f"the server never started:\n{log}"
PY
run vinowhisper-server --stop
echo "ok"
