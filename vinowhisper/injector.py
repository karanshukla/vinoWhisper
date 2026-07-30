"""Types text at the cursor via ydotool (uinput-based, works on KDE Wayland)."""

import subprocess


class TextInjector:
    def type(self, text: str) -> None:
        subprocess.run(["ydotool", "type", "--", text], check=True)
