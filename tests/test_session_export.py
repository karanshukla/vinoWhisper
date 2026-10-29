"""Exporting a session's summary or transcript to a file outside the bundle (#137).

Export is read-only: the bundle and sessions.json are never touched, and what
leaves keeps its [MM:SS] stamps and the machine-written marker.
"""

import json
import os
import shutil
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest

from vinowhisper import bundle, config, session, session_export

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NAME = "2026-10-27-140512.vwsession"
FOOTER = "Machine-written summary. The transcript is the record; check it at the time stamps."
TITLE = "Local captioning on the NPU"
ODF_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    home = tmp_path / "sessions"
    home.mkdir()
    monkeypatch.setattr(config, "TRANSCRIPT_DIR", home)
    with_summary = home / NAME
    shutil.copy(FIXTURES / "session_v1.vwsession", with_summary)
    without = home / "2026-10-27-150000.vwsession"
    shutil.copy(FIXTURES / "session_v1_nosummary.vwsession", without)
    return with_summary, without


@pytest.fixture
def out(tmp_path):
    directory = tmp_path / "out"
    directory.mkdir()
    return directory


def _export(capsys, *argv) -> tuple[int, str, str]:
    status = session.main(["export", *map(str, argv)])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def _odt_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("content.xml"))
    return "\n".join(
        "".join(node.itertext())
        for node in root.iter()
        if node.tag in (f"{ODF_TEXT}p", f"{ODF_TEXT}h")
    )


def _read(path: Path) -> str:
    return _odt_text(path) if path.suffix == ".odt" else path.read_text(encoding="utf-8")


@pytest.mark.parametrize("fmt", session_export.FORMATS)
@pytest.mark.parametrize("part", session_export.PARTS)
def test_each_part_and_format_keeps_the_stamps_and_the_marker(sessions, out, capsys, part, fmt):
    path, _ = sessions
    status, stdout, _ = _export(capsys, path, "--part", part, "--format", fmt, "-o", out)
    assert status == 0
    written = Path(stdout.strip())
    assert written.parent == out and written.suffix == f".{fmt}"
    text = _read(written)
    assert TITLE in text
    has_summary = part in ("summary", "both")
    has_transcript = part in ("transcript", "both")
    assert (FOOTER in text) is has_summary
    assert ("Captions run locally [00:00]" in text) is has_summary
    assert ("[01:10] Café, naïve, “quoted”" in text) is has_transcript
    assert ("and an ellipsis…" in text) is has_transcript
    # Minutes keep counting past an hour, in the summary's stamps and the transcript's.
    assert "[61:05]" in text
    # The transcript's header line is what the facts above it already say.
    assert "vinoWhisper transcript, " not in text


def test_markdown_carries_the_attestation_as_frontmatter(sessions, out, capsys):
    path, _ = sessions
    _, stdout, _ = _export(capsys, path, "-o", out)
    text = Path(stdout.strip()).read_text(encoding="utf-8")
    head, body = text.split("\n---\n", 1)
    assert head.startswith("---\n")
    facts = dict(line.split(": ", 1) for line in head.splitlines()[1:])
    assert {key: json.loads(value) for key, value in facts.items() if key != "tags"} == {
        "date": "2026-10-27T14:05:12+00:00",
        "duration": "1:01:40",
        "source": "system audio",
        "language": "en",
        "model": "openai/whisper-small.en",
        "summary_model": "HuggingFaceTB/SmolLM3-3B",
        "vinowhisper_session": NAME,
    }
    assert body.lstrip().startswith(f"# {TITLE}\n\n## Summary\n")
    # The summary's own "# title" became the document's, not a second heading.
    assert body.count(f"# {TITLE}") == 1
    assert "\n## Transcript\n" in body
    assert f"_{FOOTER}_" in body


def test_plain_text_has_a_header_block_and_no_markup(sessions, out, capsys):
    path, _ = sessions
    _, stdout, _ = _export(capsys, path, "--format", "txt", "-o", out)
    text = Path(stdout.strip()).read_text(encoding="utf-8")
    assert text.startswith(f"{TITLE}\n\nDate: 2026-10-27T14:05:12+00:00\nLength: 1:01:40\n")
    assert f"Session: {NAME}\n" in text
    assert "#" not in text and "---" not in text and "_Machine" not in text
    assert "\nSUMMARY\n" in text and "\nTRANSCRIPT\n" in text


def test_the_odt_is_a_well_formed_opendocument(sessions, out, capsys):
    path, _ = sessions
    _, stdout, _ = _export(capsys, path, "--format", "odt", "-o", out)
    written = Path(stdout.strip())
    with zipfile.ZipFile(written) as archive:
        first = archive.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
        assert archive.read(first) == b"application/vnd.oasis.opendocument.text"
        names = set(archive.namelist())
        manifest = ElementTree.fromstring(archive.read("META-INF/manifest.xml"))
        listed = {
            entry.get("{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}full-path")
            for entry in manifest
        }
        assert listed - {"/"} == names - {"mimetype", "META-INF/manifest.xml"}
        for name in ("content.xml", "styles.xml", "meta.xml"):
            ElementTree.fromstring(archive.read(name))
        meta = archive.read("meta.xml").decode()
    # The magic `file` and file managers read: "mimetype" at 30, its value at 38.
    raw = written.read_bytes()
    assert raw[30:38] == b"mimetype" and raw[38:77] == b"application/vnd.oasis.opendocument.text"
    assert f'meta:name="vinowhisper_session">{NAME}<' in meta
    content = zipfile.ZipFile(written).read("content.xml").decode()
    assert 'text:list text:style-name="Bullets"' in content
    assert content.count("<text:list-item>") == 2


def test_the_same_export_twice_is_the_same_bytes(sessions, out, capsys):
    path, _ = sessions
    first = out / "a.odt"
    second = out / "b.odt"
    _export(capsys, path, "-o", first)
    _export(capsys, path, "-o", second)
    assert first.read_bytes() == second.read_bytes()


def test_a_summary_that_is_not_there_is_a_clear_error(sessions, out, capsys):
    _, without = sessions
    status, stdout, stderr = _export(capsys, without, "--part", "summary", "-o", out)
    assert status == 1 and stdout == ""
    assert "has no summary yet; export --part transcript instead" in stderr
    assert list(out.iterdir()) == []


def test_both_without_a_summary_exports_the_transcript_and_says_so(sessions, out, capsys):
    _, without = sessions
    status, stdout, stderr = _export(capsys, without, "--part", "both", "-o", out, "--json")
    assert status == 0
    report = json.loads(stdout)
    assert report["parts"] == ["transcript"]
    assert report["summary_missing"] is True and report["state"] == "written"
    assert "exported the transcript only" in stderr
    written = Path(report["path"])
    assert written.name == "2026-10-27 Transcript.md"
    text = written.read_text(encoding="utf-8")
    assert "## Summary" not in text and "summary_model" not in text
    assert "# Transcript, 2026-10-27 14:05" in text


def test_a_file_is_never_replaced_without_force(sessions, out, capsys):
    path, _ = sessions
    target = out / "notes.md"
    target.write_text("mine", encoding="utf-8")
    status, _, stderr = _export(capsys, path, "-o", target)
    assert status == 1 and "already exists; --force replaces it" in stderr
    assert target.read_text(encoding="utf-8") == "mine"
    assert _export(capsys, path, "-o", target, "--force")[0] == 0
    assert TITLE in target.read_text(encoding="utf-8")
    assert sorted(p.name for p in out.iterdir()) == ["notes.md"]


def test_into_a_directory_a_taken_name_is_numbered_not_replaced(sessions, out, capsys):
    path, _ = sessions
    names = [Path(_export(capsys, path, "-o", out)[1].strip()).name for _ in range(3)]
    stem = f"2026-10-27 {TITLE} (summary and transcript)"
    assert names == [f"{stem}.md", f"{stem} (2).md", f"{stem} (3).md"]


def test_the_file_name_follows_the_part_and_the_format(sessions, out, capsys):
    path, without = sessions

    def name(bundle_path, part, fmt):
        printed = _export(
            capsys, bundle_path, "--part", part, "--format", fmt, "-o", out, "--print-path"
        )
        return Path(printed[1].strip()).relative_to(out).as_posix()

    assert name(path, "summary", "md") == f"2026-10-27 {TITLE}.md"
    assert name(path, "transcript", "odt") == f"2026-10-27 {TITLE} (transcript).odt"
    assert name(path, "both", "md") == f"2026-10-27 {TITLE} (summary and transcript).md"
    assert name(without, "transcript", "txt") == "2026-10-27 Transcript.txt"
    # A path is printed as given, and nothing is written either way.
    assert _export(capsys, path, "-o", out / "x.md", "--print-path")[1] == f"{out / 'x.md'}\n"
    assert list(out.iterdir()) == []


def test_the_format_follows_the_output_name_unless_given(sessions, out, capsys):
    path, _ = sessions
    _export(capsys, path, "-o", out / "notes.odt")
    assert zipfile.is_zipfile(out / "notes.odt")
    _export(capsys, path, "-o", out / "notes.txt", "--format", "md")
    assert (out / "notes.txt").read_text(encoding="utf-8").startswith("---\n")


@pytest.mark.parametrize(
    ("title", "name"),
    [
        ("../../etc/passwd", "2026-10-27 etc passwd.md"),
        ('a/b\\c:d*e?f"g<h>i|j', "2026-10-27 a b c d e f g h i j.md"),
        ("tab\tesc\x1bnul\x00rtl\u202eend", "2026-10-27 tab esc nul rtl end.md"),
        ("...", "2026-10-27 Summary.md"),
        ("é" * 300, "2026-10-27 " + "é" * 94 + ".md"),
    ],
)
def test_an_unsafe_title_makes_a_safe_file_name(tmp_path, out, capsys, title, name):
    started = datetime(2026, 10, 27, 14, 5, 12, tzinfo=UTC)
    attestation = bundle.new_attestation(
        started, 60, "microphone", "en", "transcribe", "NPU", "openai/whisper-small.en"
    )
    made = bundle.Bundle(
        tmp_path / NAME, "[00:00] Hello.\n", attestation, summary=f"# {title}\n\nText.\n"
    )
    bundle.write(made)
    status, stdout, _ = _export(capsys, made.path, "--part", "summary", "-o", out)
    assert status == 0
    written = Path(stdout.strip())
    assert written.parent == out
    assert written.name == name
    assert len(written.name.encode()) <= 255


def test_the_file_is_readable_by_others_and_written_atomically(sessions, out, capsys):
    path, _ = sessions
    old = os.umask(0o077)
    try:
        _, stdout, _ = _export(capsys, path, "-o", out)
    finally:
        os.umask(old)
    written = Path(stdout.strip())
    assert stat.S_IMODE(written.stat().st_mode) == 0o644
    assert [p.name for p in out.iterdir()] == [written.name]


def test_export_changes_nothing_in_the_bundle_or_the_state(sessions, out, capsys):
    path, without = sessions
    before = {p: p.read_bytes() for p in (path, without)}
    for part in session_export.PARTS:
        _export(capsys, path, "--part", part, "--format", "odt", "-o", out)
    assert {p: p.read_bytes() for p in (path, without)} == before
    assert not config.SESSIONS_STATE_FILE.exists()


def test_the_default_place_is_the_documents_folder(sessions, tmp_path, monkeypatch, capsys):
    path, _ = sessions
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DOCUMENTS_DIR", raising=False)
    (home / ".config/user-dirs.dirs").write_text(
        '# written by xdg-user-dirs-update\nXDG_DOCUMENTS_DIR="$HOME/Dokumente"\n',
        encoding="utf-8",
    )
    _, stdout, _ = _export(capsys, path, "--part", "summary")
    assert Path(stdout.strip()) == home / "Dokumente/vinoWhisper" / f"2026-10-27 {TITLE}.md"

    # Disabled (pointed at $HOME) or never set up: ~/Documents.
    (home / ".config/user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/"\n', encoding="utf-8")
    assert session_export.documents_dir() == home / "Documents"
    (home / ".config/user-dirs.dirs").unlink()
    assert session_export.documents_dir() == home / "Documents"
    monkeypatch.setenv("XDG_DOCUMENTS_DIR", str(tmp_path / "docs"))
    assert session_export.default_dir() == tmp_path / "docs/vinoWhisper"


def test_an_unreadable_bundle_is_an_error_not_a_file(tmp_path, out, capsys):
    broken = tmp_path / NAME
    broken.write_bytes(b"not a zip")
    status, _, stderr = _export(capsys, broken, "-o", out)
    assert status == 1 and NAME in stderr
    assert list(out.iterdir()) == []


def test_characters_xml_cannot_hold_are_dropped_from_the_odt(tmp_path, out, capsys):
    attestation = bundle.new_attestation(None, None, None, None, None, None, None)
    made = bundle.Bundle(
        tmp_path / NAME, "[00:00] bell\x07 tab\there & <tag>\n", attestation, summary=None
    )
    bundle.write(made)
    _export(capsys, made.path, "--format", "odt", "-o", out / "x.odt")
    assert "[00:00] bell tabhere & <tag>" in _odt_text(out / "x.odt")
    content = zipfile.ZipFile(out / "x.odt").read("content.xml").decode()
    assert "tab<text:tab/>here &amp; &lt;tag&gt;" in content


def test_reuse_keeps_an_earlier_export_and_says_whether_it_was_edited(sessions, out, capsys):
    path, _ = sessions

    def reuse(fmt="md"):
        status, stdout, _ = _export(
            capsys, path, "--part", "summary", "--format", fmt, "-o", out, "--reuse", "--json"
        )
        assert status == 0
        return json.loads(stdout)

    first = reuse()
    assert first["state"] == "written"
    assert reuse() == {**first, "state": "unchanged"}
    kept = Path(first["path"])
    kept.write_text(kept.read_text(encoding="utf-8") + "\nMy own notes.\n", encoding="utf-8")
    assert reuse() == {**first, "state": "edited"}
    assert kept.read_text(encoding="utf-8").endswith("My own notes.\n")
    assert [p.name for p in out.iterdir()] == [kept.name]

    odt = reuse("odt")
    assert reuse("odt")["state"] == "unchanged"
    # A word processor saving it rewrites content.xml and keeps the custom properties.
    saved = Path(odt["path"])
    with zipfile.ZipFile(saved) as archive:
        parts = {info.filename: archive.read(info) for info in archive.infolist()}
    parts["content.xml"] = parts["content.xml"].replace(b"Captions run", b"Captions ran")
    with zipfile.ZipFile(saved, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    assert reuse("odt")["state"] == "edited"


def test_reuse_does_not_take_another_sessions_file_for_its_own(sessions, out, capsys):
    path, without = sessions
    other = path.with_name("2026-10-27-170000.vwsession")
    shutil.copy(without, other)

    def reuse(bundle_path):
        argv = (bundle_path, "--part", "transcript", "-o", out, "--reuse", "--json")
        return json.loads(_export(capsys, *argv)[1])

    # Two sessions that day, neither summarised: both want "2026-10-27 Transcript.md".
    mine = reuse(without)
    theirs = reuse(other)
    assert Path(mine["path"]).name == "2026-10-27 Transcript.md"
    assert Path(theirs["path"]).name == "2026-10-27 Transcript (2).md"
    assert theirs["state"] == "written"
    assert reuse(other)["state"] == "unchanged"
    assert reuse(without) == {**mine, "state": "unchanged"}
    for fmt, needle in (("txt", "Session: "), ("md", "vinowhisper_session: ")):
        stranger = out / f"2026-10-27 Transcript (3).{fmt}"
        stranger.write_text(f"{needle}someone-else.vwsession\n", encoding="utf-8")
        assert not session_export._from_session(stranger, bundle.read(without), fmt)
    assert not session_export._from_session(out / "missing.odt", bundle.read(path), "odt")


def test_force_and_reuse_together_are_refused(sessions, out, capsys):
    path, _ = sessions
    status, _, stderr = _export(capsys, path, "-o", out, "--force", "--reuse")
    assert status == 1 and "pick one" in stderr
    assert list(out.iterdir()) == []
