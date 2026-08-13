"""Backend choice and the exact argv each one gets.

`record_argv` is a pure function precisely so this is checkable without an
audio server: the flags are load-bearing (stream.capture.sink is the
difference between capturing a video and capturing the mic) and a typo in them
is invisible until someone is staring at silent captions.
"""

import pytest

from vinowhisper import capture


@pytest.fixture
def on_path(monkeypatch):
    """Pretend exactly these binaries exist."""

    def install(*names: str):
        monkeypatch.setattr(
            capture, "_which", lambda name: f"/usr/bin/{name}" if name in names else None
        )

    return install


def test_pipewire_is_preferred_when_both_exist(on_path):
    on_path("pw-record", "parec")
    assert capture.backend().name == capture.PIPEWIRE


def test_pulse_is_used_when_pipewire_is_absent(on_path):
    on_path("parec")
    backend = capture.backend()
    assert backend.name == capture.PULSEAUDIO
    assert not backend.supports_app_capture


def test_no_backend_at_all_explains_how_to_install_one(on_path):
    on_path()
    with pytest.raises(capture.CaptureError) as excinfo:
        capture.backend()
    message = str(excinfo.value)
    assert "pw-record" in message and "parec" in message


def test_the_backend_can_be_forced(on_path, monkeypatch):
    on_path("pw-record", "parec")
    monkeypatch.setenv(capture.BACKEND_ENV, "pulseaudio")
    assert capture.backend().name == capture.PULSEAUDIO


def test_forcing_a_missing_backend_is_an_error(on_path, monkeypatch):
    on_path("pw-record")
    monkeypatch.setenv(capture.BACKEND_ENV, "pulseaudio")
    with pytest.raises(capture.CaptureError, match="not on PATH"):
        capture.backend()


def test_forcing_nonsense_is_an_error(on_path, monkeypatch):
    on_path("pw-record")
    monkeypatch.setenv(capture.BACKEND_ENV, "alsa")
    with pytest.raises(capture.CaptureError, match="not one of"):
        capture.backend()


def test_pipewire_system_audio_sets_the_capture_sink_property(monkeypatch):
    monkeypatch.setattr(
        capture, "default_sink", lambda: "alsa_output.pci-0000_00_1f.3.analog-stereo"
    )
    argv = capture.record_argv("output", None, capture._PIPEWIRE_BACKEND)
    assert argv[0] == "pw-record"
    assert "--raw" in argv and "16000" in argv and "f32" in argv
    # Without this property WirePlumber silently reconnects the stream to the
    # microphone (confirmed 2026-08-03), which is the whole bug it prevents.
    assert "{ stream.capture.sink = true }" in argv
    assert argv[argv.index("--target") + 1].startswith("alsa_output")
    assert argv[-1] == "-"


def test_pipewire_mic_does_not_set_the_capture_sink_property():
    argv = capture.record_argv("mic", None, capture._PIPEWIRE_BACKEND)
    assert "stream.capture.sink = true" not in " ".join(argv)
    assert "--target" not in argv


def test_pipewire_target_overrides_the_default_sink():
    argv = capture.record_argv("output", "1234", capture._PIPEWIRE_BACKEND)
    assert argv[argv.index("--target") + 1] == "1234"


def test_pulse_records_the_monitor_source(monkeypatch):
    monkeypatch.setattr(capture, "default_sink", lambda: "alsa_output.analog-stereo")
    argv = capture.record_argv("output", None, capture._PULSE_BACKEND)
    assert argv[0] == "parec"
    assert "--device=alsa_output.analog-stereo.monitor" in argv
    assert "--format=float32le" in argv
    assert "--rate=16000" in argv


def test_pulse_does_not_double_the_monitor_suffix(monkeypatch):
    monkeypatch.setattr(capture, "default_sink", lambda: "x")
    assert capture.monitor_source("already.monitor") == "already.monitor"
    assert capture.monitor_source(None) == "x.monitor"


def test_pulse_mic_uses_the_default_source():
    argv = capture.record_argv("mic", None, capture._PULSE_BACKEND)
    assert not any(part.startswith("--device=") for part in argv)


def test_default_sink_falls_back_to_pw_dump_when_pactl_is_missing(monkeypatch, on_path):
    """A PipeWire install without pipewire-pulse has no pactl at all."""
    on_path("pw-record", "pw-dump")
    monkeypatch.setattr(
        capture,
        "_pw_dump",
        lambda: [
            {
                "info": {"props": {"metadata.name": "default"}},
                "metadata": [
                    {"key": "default.audio.sink", "value": {"name": "alsa_output.usb-headset"}}
                ],
            }
        ],
    )
    assert capture.default_sink() == "alsa_output.usb-headset"


def test_default_sink_without_pactl_or_metadata_says_what_to_install(monkeypatch, on_path):
    on_path("pw-record")
    monkeypatch.setattr(capture, "_pw_dump", list)
    with pytest.raises(capture.CaptureError, match="could not determine the default sink"):
        capture.default_sink()


def test_pipewire_streams_are_application_nodes(monkeypatch, on_path):
    on_path("pw-record", "pw-dump")
    monkeypatch.setattr(
        capture,
        "_pw_dump",
        lambda: [
            {
                "info": {
                    "props": {
                        "media.class": "Stream/Output/Audio",
                        "object.serial": 1234,
                        "application.name": "Chromium",
                        "media.name": "Playback",
                    }
                }
            },
            {"info": {"props": {"media.class": "Audio/Sink", "object.serial": 9}}},
        ],
    )
    streams = capture.playback_streams()
    assert streams == [{"target": "1234", "app": "Chromium", "media": "Playback"}]


def test_pulse_lists_monitor_sources_not_sink_inputs(monkeypatch, on_path):
    """Listing sink-inputs would look targetable and isn't; parec can't record them."""
    on_path("parec", "pactl")
    monkeypatch.setattr(
        capture,
        "_run",
        lambda argv: (
            "0\talsa_output.analog-stereo.monitor\tmodule\ts16le\tRUNNING\n"
            "1\talsa_input.usb-mic\tmodule\ts16le\tSUSPENDED\n"
        ),
    )
    targets = capture.playback_streams()
    assert [target["target"] for target in targets] == ["alsa_output.analog-stereo.monitor"]


def test_monitor_channel_volumes_reads_the_node_property(monkeypatch):
    monkeypatch.setattr(capture, "default_sink", lambda: "sink")
    monkeypatch.setattr(
        capture,
        "_pw_dump",
        lambda: [{"info": {"props": {"node.name": "sink", "monitor.channel-volumes": "true"}}}],
    )
    assert capture.monitor_channel_volumes() is True
