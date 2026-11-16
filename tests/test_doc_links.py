"""Every `test_file.py::test_name` a comment cites must be a real test.

A rule that names its test then fails CI when the test is renamed or deleted.
See docs/comment-style.md.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ["vinowhisper", "scripts", "tests"]
CITATION = re.compile(r"`(test_\w+\.py)::(test_\w+)`")


def cited_tests(text: str) -> list[tuple[int, str, str]]:
    return [
        (line_no, file, name)
        for line_no, line in enumerate(text.splitlines(), 1)
        for file, name in CITATION.findall(line)
    ]


def is_test_fn(text: str, name: str) -> bool:
    return re.search(rf"^\s*(async\s+)?def {name}\(", text, re.MULTILINE) is not None


def resolves(tests_dir: Path, file: str, name: str) -> bool:
    path = tests_dir / file
    return path.is_file() and is_test_fn(path.read_text(), name)


def test_every_cited_test_exists() -> None:
    broken = [
        f"{path.relative_to(ROOT)}:{line_no}: `{file}::{name}`"
        for folder in SCANNED
        for path in sorted((ROOT / folder).rglob("*.py"))
        for line_no, file, name in cited_tests(path.read_text())
        if path != Path(__file__).resolve() and not resolves(ROOT / "tests", file, name)
    ]
    assert not broken, "no such test:\n" + "\n".join(broken)


def test_the_check_finds_a_cited_test_and_rejects_a_missing_one(tmp_path: Path) -> None:
    (tmp_path / "test_x.py").write_text("def test_real():\n    pass\n\ndef helper():\n    pass\n")
    line = "# (pinned by `test_x.py::test_real`, `test_x.py::test_gone`)"
    assert cited_tests(line) == [(1, "test_x.py", "test_real"), (1, "test_x.py", "test_gone")]
    assert resolves(tmp_path, "test_x.py", "test_real")
    assert not resolves(tmp_path, "test_x.py", "test_gone")
    assert not resolves(tmp_path, "test_y.py", "test_real")
