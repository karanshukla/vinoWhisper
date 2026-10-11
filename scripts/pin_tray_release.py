#!/usr/bin/env python3
"""Pin the release's desktop binaries into the package, before the wheel is built.

Run by .github/workflows/release.yml, not by hand:

    python3 scripts/pin_tray_release.py vinowhisper-tray-x86_64-linux \
        vinowhisper-gui-x86_64-linux --version 1.0.0

Writes vinowhisper/tray_release.json and vinowhisper/gui_release.json, which
`vinowhisper-setup --gui` checks the downloads against (see vinowhisper/overlay.py).
Each asset's name says which file it goes in. Standard library only, so it runs on
a bare runner before any environment exists.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vinowhisper import overlay  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Pin the desktop binaries into the wheel.")
    parser.add_argument("binaries", type=Path, nargs="+", help="the release assets, as uploaded")
    parser.add_argument("--version", required=True, help="the release version, no leading v")
    parser.add_argument("--arch", default="x86_64")
    args = parser.parse_args()

    for binary in args.binaries:
        component = overlay.component_for_asset(binary.name, args.arch)
        record = overlay.pin_record(binary, args.version, args.arch, component)
        component.pin_file.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"pinned {binary} into {component.pin_file}")
        print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
