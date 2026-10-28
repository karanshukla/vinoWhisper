"""The .vwsession bundle: the format vinoSearch reads (karanshukla/vinoSearch#143)."""

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vinowhisper import bundle

FIXTURES = Path(__file__).resolve().parent / "fixtures"

TRANSCRIPT = (
    "vinoWhisper transcript, 2026-10-27 14:05, system audio, NPU\n\n"
    "[00:00] Welcome to the talk about local captioning on the NPU.\n\n"
    "[01:10] Café, naïve, “quoted” — and an ellipsis…\n\n"
    "[61:05] Past an hour the minutes keep counting.\n"
)
SUMMARY = (
    "# Local captioning on the NPU\n\n"
    "A talk about captioning on the NPU.\n\n"
    "- Captions run locally [00:00]\n"
    "- Stamps keep counting minutes past an hour [61:05]\n\n"
    "_Machine-written summary. The transcript is the record; check it at the time stamps._\n"
)


def _new(path: Path, summary: str | None = None) -> bundle.Bundle:
    started = datetime(2026, 10, 27, 14, 5, 12, tzinfo=UTC)
    attestation = bundle.new_attestation(
        started, 3700, "system audio", "en", "transcribe", "NPU", "openai/whisper-small.en"
    )
    made = bundle.Bundle(path, TRANSCRIPT, attestation)
    if summary is not None:
        made.summary = summary
        made.attestation["files"][bundle.SUMMARY] = {
            "made_by": bundle.MADE_BY_SUMMARY,
            "model": "HuggingFaceTB/SmolLM3-3B",
            "export": "arankays/SmolLM3-3B-int4-cw-ov@136eac7690714ab0a1e65806eb24f98d993f0438",
            "device": "NPU",
            "generated": "2026-10-27T15:10:03+00:00",
        }
    return made


def test_characterization_the_layout_and_mimetype_are_format_1():
    """characterization: vinoSearch and file managers read these bytes.

    The mimetype is the first entry, stored uncompressed with no extra field, so
    its name sits at offset 30 and its value at 38, as in ODF and EPUB; that is
    what shared-mime-info's magic matches. Changing any of this, or the entry
    names, needs a new FORMAT ("vinowhisper-session/2"), since readers refuse a
    format they don't know rather than guess at it.
    """
    assert bundle.FORMAT == "vinowhisper-session/1"
    assert bundle.MIMETYPE == b"application/vnd.vinowhisper.session+zip"
    assert bundle.SUFFIX == ".vwsession"
    assert bundle.LAYOUT == ("mimetype", "transcript.txt", "summary.md", "attestation.json")
    assert bundle.MADE_BY_SUMMARY == "ai-generated-summary"


def test_the_mimetype_is_first_stored_and_at_the_magic_offset(tmp_path):
    path = tmp_path / "2026-10-27-140512.vwsession"
    bundle.write(_new(path, SUMMARY))
    raw = path.read_bytes()
    assert raw[30:38] == b"mimetype"
    assert raw[38 : 38 + len(bundle.MIMETYPE)] == bundle.MIMETYPE
    with zipfile.ZipFile(path) as archive:
        first = archive.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert first.extra == b""
        assert set(archive.namelist()) == set(bundle.LAYOUT)


def test_a_bundle_round_trips_and_its_hashes_verify(tmp_path):
    path = tmp_path / "s.vwsession"
    bundle.write(_new(path, SUMMARY))
    read = bundle.read(path)
    assert read.transcript == TRANSCRIPT
    assert read.summary == SUMMARY
    assert read.summary_title == "Local captioning on the NPU"
    assert bundle.verify(read) == []
    assert read.attestation["files"]["summary.md"]["made_by"] == "ai-generated-summary"


def test_an_edit_after_the_fact_shows(tmp_path):
    path = tmp_path / "s.vwsession"
    made = _new(path, SUMMARY)
    bundle.write(made)
    attestation = bundle.read(path).attestation
    tampered = tmp_path / "t.vwsession"
    with zipfile.ZipFile(tampered, "w") as archive:
        archive.writestr("mimetype", bundle.MIMETYPE, zipfile.ZIP_STORED)
        archive.writestr("transcript.txt", TRANSCRIPT.replace("NPU.", "GPU."))
        archive.writestr("summary.md", SUMMARY)
        archive.writestr("attestation.json", json.dumps(attestation))
    assert bundle.verify(bundle.read(tampered)) == ["transcript.txt"]


def test_an_unknown_format_is_refused_not_guessed(tmp_path):
    path = tmp_path / "s.vwsession"
    made = _new(path)
    made.attestation["format"] = "vinowhisper-session/2"
    bundle.write(made)
    with pytest.raises(bundle.BundleError, match="unknown format"):
        bundle.read(path)


def test_a_plain_zip_is_not_a_session(tmp_path):
    path = tmp_path / "s.vwsession"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("transcript.txt", "hi")
    with pytest.raises(bundle.BundleError):
        bundle.read(path)


def test_writing_is_atomic_private_and_leaves_no_temp(tmp_path):
    path = tmp_path / "s.vwsession"
    bundle.write(_new(path))
    bundle.set_summary(bundle.read(path), SUMMARY, "NPU")
    assert [p.name for p in tmp_path.iterdir()] == ["s.vwsession"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert bundle.read(path).summary == SUMMARY


def test_temp_files_never_look_like_bundles(tmp_path):
    path = tmp_path / "s.vwsession"
    real = bundle.tempfile.mkstemp

    seen = []

    def spy(**kwargs):
        fd, name = real(**kwargs)
        seen.append(Path(name).name)
        return fd, name

    bundle.tempfile.mkstemp = spy
    try:
        bundle.write(_new(path))
    finally:
        bundle.tempfile.mkstemp = real
    [temp] = seen
    assert temp.startswith(".") and temp.endswith(".tmp")
    assert not bundle.is_bundle_name(Path(temp))


def test_dropping_a_summary_removes_it_from_the_attestation(tmp_path):
    path = tmp_path / "s.vwsession"
    bundle.write(_new(path, SUMMARY))
    bundle.drop_summary(bundle.read(path))
    read = bundle.read(path)
    assert read.summary is None
    assert "summary.md" not in read.attestation["files"]
    assert bundle.verify(read) == []


def test_a_loose_transcript_is_bundled_from_what_its_name_and_header_say(tmp_path):
    loose = tmp_path / "2026-10-27-140512.txt"
    loose.write_text(TRANSCRIPT, encoding="utf-8")
    path = bundle.bundle_transcript(loose)
    assert not loose.exists()
    read = bundle.read(path)
    assert read.session["started"].startswith("2026-10-27T14:05:12")
    assert read.session["duration_s"] == 61 * 60 + 5
    assert read.session["source"] == "system audio"
    assert read.session["language"] is None
    assert read.attestation["files"]["transcript.txt"]["device"] == "NPU"
    assert read.attestation["files"]["transcript.txt"]["model"] is None


def test_stamps_past_an_hour_keep_counting_minutes():
    assert [seconds for _, seconds in bundle.stamps(TRANSCRIPT)] == [0, 70, 3665]


@pytest.mark.parametrize("name", ["session_v1.vwsession", "session_v1_nosummary.vwsession"])
def test_the_fixtures_vinosearch_copies_are_what_the_writer_makes(tmp_path, name):
    """The fixtures are shared with vinoSearch's tests; regenerate them, don't hand-edit.

    VINOWHISPER_BLESS_FIXTURES=1 rewrites them.
    """
    import os

    path = tmp_path / name
    made = _new(path, SUMMARY if "nosummary" not in name else None)
    made.attestation["generator"]["version"] = "1.1.0"
    bundle.write(made)
    fixture = FIXTURES / name
    if os.environ.get("VINOWHISPER_BLESS_FIXTURES"):
        fixture.write_bytes(path.read_bytes())
    assert fixture.read_bytes() == path.read_bytes()
