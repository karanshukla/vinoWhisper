"""What was sending the audio (#156): MPRIS players and PipeWire streams, read with no bus here."""

import json

from vinowhisper import capture, playing

NAMES = [
    "org.freedesktop.DBus",
    ":1.7",
    "org.mpris.MediaPlayer2.firefox.instance_1_23",
    "org.mpris.MediaPlayer2.mpv",
]

# Copied from busctl --json=short against a real session bus, 2026-11-09.
FIREFOX = {
    "Metadata": {
        "type": "a{sv}",
        "data": {
            "mpris:trackid": {"type": "o", "data": "/track/1"},
            "xesam:title": {"type": "s", "data": "How NPUs work - YouTube"},
            "xesam:artist": {"type": "as", "data": ["Some Channel"]},
            "xesam:url": {"type": "s", "data": "https://example.com/watch?v=secret"},
            "mpris:artUrl": {"type": "s", "data": "file:///home/someone/.cache/art.png"},
        },
    },
    "PlaybackStatus": {"type": "s", "data": "Playing"},
}
MPV = {
    "Metadata": {"type": "a{sv}", "data": {"xesam:title": {"type": "s", "data": "old.mkv"}}},
    "PlaybackStatus": {"type": "s", "data": "Paused"},
}


def _bus(monkeypatch, players=None, identities=None):
    players = {NAMES[2]: FIREFOX, NAMES[3]: MPV} if players is None else players
    identities = identities or {NAMES[2]: "Mozilla Firefox", NAMES[3]: "mpv"}

    def busctl(*args):
        if args[-1] == "ListNames":
            return [list(NAMES)]
        if "GetAll" in args:
            return [players[args[1]]] if args[1] in players else None
        return identities.get(args[1])

    monkeypatch.setattr(playing, "_busctl", busctl)


def _streams(monkeypatch, streams):
    monkeypatch.setattr(capture, "backend", lambda: capture._PIPEWIRE_BACKEND)
    monkeypatch.setattr(
        capture,
        "_pw_dump",
        lambda: [
            {
                "info": {
                    "state": state,
                    "props": {
                        "media.class": "Stream/Output/Audio",
                        "object.serial": serial,
                        "application.name": app,
                        "media.name": "a tab title that is not read from here",
                    },
                }
            }
            for serial, app, state in streams
        ],
    )


def test_only_a_player_that_is_playing_is_named(monkeypatch):
    _bus(monkeypatch)
    assert playing.mpris_playing() == [
        {"app": "Mozilla Firefox", "title": "How NPUs work - YouTube", "artist": "Some Channel"}
    ]


def test_characterization_the_url_and_art_never_leave_the_player(monkeypatch):
    """characterization: only app, title and artist are kept, and that is the feature.

    The issue asks for "no personal information". A URL can carry a session token or a
    private link, and artUrl is a path under the user's home. "Enriching" the entry with
    either would put them in a file the user may share or sync.
    """
    _bus(monkeypatch)
    text = json.dumps(playing.mpris_playing())
    assert "example.com" not in text and "someone" not in text and "/track/1" not in text
    assert set(playing.mpris_playing()[0]) <= set(playing.KEYS)


def test_a_player_without_an_identity_goes_by_its_bus_name(monkeypatch):
    _bus(monkeypatch, identities={"none": ""})
    assert playing.mpris_playing()[0]["app"] == "firefox.instance_1_23"


def test_no_bus_no_busctl_or_a_broken_answer_is_nothing_playing(monkeypatch):
    monkeypatch.setattr(playing, "_busctl", lambda *args: None)
    assert playing.mpris_playing() == []
    monkeypatch.setattr(playing, "_busctl", lambda *args: "nonsense")
    assert playing.mpris_playing() == []
    monkeypatch.undo()
    monkeypatch.setattr(capture, "_which", lambda name: None)
    assert playing._busctl("call") is None


def test_titles_are_cleaned_and_capped(monkeypatch):
    noisy = {
        "Metadata": {
            "type": "a{sv}",
            "data": {"xesam:title": {"type": "s", "data": "a\x1b[31m\n  b" + "x" * 500}},
        },
        "PlaybackStatus": {"type": "s", "data": "Playing"},
    }
    _bus(monkeypatch, players={NAMES[2]: noisy})
    title = playing.mpris_playing()[0]["title"]
    assert title.startswith("a[31m b") and len(title) == playing.MAX_FIELD_CHARS


def test_an_app_making_sound_without_a_player_is_named_by_app_alone(monkeypatch):
    _bus(monkeypatch)
    _streams(
        monkeypatch,
        [(51, "Firefox", "running"), (52, "Some Game", "running"), (53, "Idle App", "idle")],
    )
    assert playing.now_playing() == [
        {"app": "Mozilla Firefox", "title": "How NPUs work - YouTube", "artist": "Some Channel"},
        {"app": "Some Game"},
    ]


def test_with_a_target_only_that_application_counts(monkeypatch):
    _bus(monkeypatch)
    _streams(monkeypatch, [(51, "Firefox", "running"), (52, "Some Game", "running")])
    assert playing.now_playing("52") == [{"app": "Some Game"}]
    assert [e["app"] for e in playing.now_playing("51")] == ["Mozilla Firefox"]


def test_pulseaudio_has_no_streams_to_name(monkeypatch):
    monkeypatch.setattr(capture, "backend", lambda: capture._PULSE_BACKEND)
    assert playing.stream_apps() == []


def test_a_title_replaces_the_bare_app_and_repeats_are_dropped():
    entries = []
    playing.merge(entries, [{"app": "Firefox"}])
    playing.merge(entries, [{"app": "Firefox"}, {"app": "Mozilla Firefox", "title": "One"}])
    playing.merge(entries, [{"app": "Mozilla Firefox", "title": "One"}, {"app": "firefox"}])
    playing.merge(entries, [{"app": "Mozilla Firefox", "title": "Two"}])
    assert entries == [
        {"app": "Mozilla Firefox", "title": "One"},
        {"app": "Mozilla Firefox", "title": "Two"},
    ]
    playing.merge(entries, [{"app": "x", "title": str(i)} for i in range(50)])
    assert len(entries) == playing.MAX_ENTRIES


def test_what_a_bundle_claims_was_playing_is_not_trusted():
    raw = [
        {"app": "Firefox", "title": "T", "url": "https://example.com"},
        {"title": "no app"},
        "text",
        {"app": ["not", "text"], "title": 3},
    ]
    assert playing.valid(raw) == [{"app": "Firefox", "title": "T"}, {"app": "not, text"}]
    assert playing.valid({"app": "Firefox"}) == []
    assert playing.valid(None) == []


def test_the_label_is_title_artist_app_and_counts_what_it_leaves_out():
    entries = [
        {"app": "Firefox", "title": "Talk", "artist": "Someone"},
        {"app": "mpv", "title": "film.mkv"},
        {"app": "Some Game"},
        {"app": "vlc", "title": "x"},
    ]
    assert playing.label(entries) == (
        "Talk by Someone (Firefox); film.mkv (mpv); Some Game; and 1 more"
    )
    assert playing.label([]) == ""


def test_the_tracker_samples_at_most_once_an_interval_and_survives_a_failure():
    now = [0.0]
    calls = []

    def sample(target):
        calls.append(target)
        if len(calls) == 2:
            raise RuntimeError("the bus went away")
        return [{"app": "Firefox", "title": f"Video {len(calls)}"}]

    tracker = playing.Tracker("51", sample=sample, clock=lambda: now[0], threaded=False)
    tracker.poke()
    tracker.poke()
    now[0] = 31.0
    tracker.poke()
    now[0] = 62.0
    tracker.poke()
    assert calls == ["51", "51", "51"]
    assert [e["title"] for e in tracker.entries()] == ["Video 1", "Video 3"]


def test_the_tracker_samples_off_the_callers_thread():
    import threading

    seen = []
    tracker = playing.Tracker(
        sample=lambda target: seen.append(threading.current_thread().name) or [{"app": "mpv"}]
    )
    tracker.poke()
    assert tracker.entries() == [{"app": "mpv"}]
    assert seen == ["playing"]
