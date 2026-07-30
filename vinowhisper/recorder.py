"""Toggle-mode mic capture: first call starts recording, second call stops it."""

import subprocess
import time
from pathlib import Path

from . import config


class Recorder:
    def is_recording(self) -> bool:
        return config.PIDFILE.exists()

    def start(self) -> None:
        if self.is_recording():
            raise RuntimeError("already recording")
        proc = subprocess.Popen(
            [
                "pw-record",
                "--rate", str(config.SAMPLE_RATE_HZ),
                "--channels", "1",
                str(config.RECORDING_WAV),
            ],
        )
        config.PIDFILE.write_text(str(proc.pid))

    def stop(self, exit_timeout_s: float = 2.0) -> Path:
        if not self.is_recording():
            raise RuntimeError("not recording")
        pid = int(config.PIDFILE.read_text().strip())
        subprocess.run(["kill", "-INT", str(pid)], check=False)
        # pw-record needs a moment to flush and close the WAV header after
        # SIGINT; poll rather than reading the file immediately.
        deadline = time.monotonic() + exit_timeout_s
        while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        config.PIDFILE.unlink()
        return config.RECORDING_WAV
