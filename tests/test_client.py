"""The transcription client talks to localhost only, never through a proxy."""

from vinowhisper import config
from vinowhisper.client import TranscriptionClient


def test_proxy_settings_in_the_environment_are_ignored(monkeypatch):
    # requests applies http_proxy even to 127.0.0.1 unless no_proxy lists it,
    # which would send the audio and transcripts to the proxy.
    monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    session = TranscriptionClient()._session
    settings = session.merge_environment_settings(
        f"{config.SERVER_URL}/transcribe", {}, None, None, None
    )
    assert not settings["proxies"]
