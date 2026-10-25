"""The capture subprocess's lifecycle, with a Python script standing in for pw-record.

No audio server: record_argv is swapped for a child that writes known float32
samples to stdout and then exits or waits, which is all the Recorder sees of
pw-record anyway.
"""

import sys
import time

import numpy as np
import pytest

from vinowhisper import capture, config, recorder


def fake_capture(monkeypatch, script: str, backend=capture._PIPEWIRE_BACKEND):
    monkeypatch.setattr(capture, "backend", lambda: backend)
    monkeypatch.setattr(
        capture, "record_argv", lambda source, target, chosen: [sys.executable, "-c", script]
    )


def writes(samples: int, then: str) -> str:
    return (
        "import sys, time\n"
        "import numpy as np\n"
        f"sys.stdout.buffer.write(np.arange({samples}, dtype='<f4').tobytes())\n"
        "sys.stdout.buffer.flush()\n"
        f"{then}\n"
    )


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_samples_reach_the_buffer_and_the_tap(monkeypatch):
    # Whole chunks: the reader blocks until a full chunk or EOF, as it does on pw-record.
    fake_capture(monkeypatch, writes(4800, "time.sleep(30)"))
    tapped = []
    with recorder.Recorder(tap=tapped.append) as rec:
        assert wait_for(lambda: rec.captured_s * config.SAMPLE_RATE_HZ >= 4800)
        rec.check_alive()
        window = rec.window(4800 / config.SAMPLE_RATE_HZ)
        proc = rec._proc
    np.testing.assert_array_equal(window, np.arange(4800, dtype=np.float32))
    assert sum(chunk.size for chunk in tapped) == 4800
    # Terminated and reaped on exit, not left running for 30s.
    assert proc.returncode is not None


def test_a_capture_that_dies_is_reported_with_its_status_and_argv(monkeypatch):
    fake_capture(monkeypatch, writes(100, "sys.exit(3)"))
    with recorder.Recorder() as rec:
        assert wait_for(lambda: rec._proc.poll() is not None)
        with pytest.raises(capture.CaptureError, match="exited with status 3") as raised:
            rec.check_alive()
    assert sys.executable in str(raised.value)


def test_a_ragged_tail_is_dropped_not_misread(monkeypatch):
    script = (
        "import sys, numpy as np\n"
        "sys.stdout.buffer.write(np.ones(10, dtype='<f4').tobytes() + b'\\x01\\x02')\n"
    )
    fake_capture(monkeypatch, script)
    with recorder.Recorder() as rec:
        assert wait_for(lambda: rec._proc.poll() is not None)
        assert wait_for(lambda: not rec._thread.is_alive())
        assert rec.captured_s * config.SAMPLE_RATE_HZ == 10


def test_a_missing_capture_tool_names_the_fix(monkeypatch):
    monkeypatch.setattr(capture, "backend", lambda: capture._PIPEWIRE_BACKEND)
    monkeypatch.setattr(capture, "record_argv", lambda *a: ["/nonexistent/pw-record"])
    with pytest.raises(capture.CaptureError, match="vinowhisper-setup"), recorder.Recorder():
        pass


def test_check_alive_outside_the_context_manager_is_an_error():
    with pytest.raises(capture.CaptureError, match="context manager"):
        recorder.Recorder().check_alive()


def test_an_unknown_source_is_refused():
    with pytest.raises(ValueError, match="output' or 'mic"):
        recorder.Recorder(source="speakers")


def test_pulseaudio_refuses_an_application_target(monkeypatch):
    fake_capture(monkeypatch, "pass", backend=capture._PULSE_BACKEND)
    with (
        pytest.raises(capture.CaptureError, match="--list-targets"),
        recorder.Recorder(source="output", target="firefox"),
    ):
        pass


def test_a_child_that_ignores_sigterm_is_killed(monkeypatch):
    script = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdout.buffer.write(bytes(6400))\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n"
    )
    fake_capture(monkeypatch, script)
    monkeypatch.setattr(recorder.subprocess.Popen, "wait", _short_first_wait())
    with recorder.Recorder() as rec:
        assert wait_for(lambda: rec.captured_s > 0)
        proc = rec._proc
    assert proc.returncode == -9


def _short_first_wait():
    """Popen.wait, but the first call times out at once so the test does not take 2s."""
    original = recorder.subprocess.Popen.wait
    calls = []

    def wait(self, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise recorder.subprocess.TimeoutExpired(self.args, timeout)
        return original(self, timeout=timeout)

    return wait
