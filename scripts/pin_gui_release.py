#!/usr/bin/env python3
"""Pin the release's overlay binary into the package, before the wheel is built.

Run by .github/workflows/release.yml, not by hand:

    python3 scripts/pin_gui_release.py vinowhisper-gui-x86_64-linux --version 0.4.0

Writes vinowhisper/gui_release.json, which `vinowhisper-setup --gui` checks the
downloaded binary against (see vinowhisper/overlay.py). Standard library only,
so it runs on a bare runner before any environment exists.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vinowhisper import overlay  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Pin the overlay binary into the wheel.")
    parser.add_argument("binary", type=Path, help="the release asset, as uploaded")
    parser.add_argument("--version", required=True, help="the release version, no leading v")
    parser.add_argument("--arch", default="x86_64")
    args = parser.parse_args()

    record = overlay.pin_record(args.binary, args.version, args.arch)
    overlay.PIN_FILE.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"pinned {args.binary} into {overlay.PIN_FILE}")
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
