"""Entry point bound to the Meta+H KDE shortcut. Run once per press: starts
recording if idle, stops + transcribes + injects if already recording.
"""

import subprocess
import sys

import requests

from . import config
from .injector import TextInjector
from .recorder import Recorder


def notify(message: str) -> None:
    subprocess.run(["notify-send", "vinoWhisper", message], check=False)


def main() -> int:
    recorder = Recorder()

    if not recorder.is_recording():
        recorder.start()
        notify("Recording…")
        return 0

    wav_path = recorder.stop()
    notify("Transcribing…")

    with open(wav_path, "rb") as f:
        response = requests.post(f"{config.SERVER_URL}/transcribe", data=f.read())
    response.raise_for_status()
    text = response.json()["text"]

    TextInjector().type(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
