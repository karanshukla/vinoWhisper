import io
import json
import os
import re
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import bundle

FORMATS = ("md", "txt", "odt")
PARTS = ("both", "summary", "transcript")

ODT_MIMETYPE = b"application/vnd.oasis.opendocument.text"
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
# Leaves room for " (99)" and the extension under the usual 255-byte name limit.
_NAME_BYTES = 200
_UNSAFE = set('/\\:*?"<>|')
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


class ExportError(ValueError):
    pass


@dataclass
class Block:
    kind: str
    text: str
    level: int = 1


@dataclass
class Document:
    title: str
    facts: list[tuple[str, str, str]]
    sections: list[tuple[str, list[Block]]] = field(default_factory=list)
    summary_missing: bool = False

    @property
    def parts(self) -> list[str]:
        return [name.lower() for name, _ in self.sections]


@dataclass
class Exported:
    path: Path
    parts: list[str]
    summary_missing: bool
    state: str

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "parts": self.parts,
            "summary_missing": self.summary_missing,
            "state": self.state,
        }


def documents_dir() -> Path:
    home = Path.home()
    found = os.environ.get("XDG_DOCUMENTS_DIR")
    if not found:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        try:
            lines = (config / "user-dirs.dirs").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            match = re.match(r'\s*XDG_DOCUMENTS_DIR\s*=\s*"(.*)"\s*$', line)
            if match:
                found = match.group(1).replace("$HOME", str(home))
    path = Path(found).expanduser() if found else home / "Documents"
    # xdg-user-dirs points a disabled directory at $HOME itself.
    return home / "Documents" if path == home else path


def default_dir() -> Path:
    return documents_dir() / "vinoWhisper"


def _date(read: bundle.Bundle) -> str:
    started = read.session.get("started")
    if isinstance(started, str) and re.match(r"\d{4}-\d{2}-\d{2}", started):
        return started[:10]
    match = re.match(r"\d{4}-\d{2}-\d{2}", read.path.name)
    return match.group(0) if match else "Session"


def _when(read: bundle.Bundle) -> str:
    started = read.session.get("started")
    if isinstance(started, str) and len(started) >= 16:
        return started[:16].replace("T", " ")
    return _date(read)


def safe_name(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = "".join(" " if c in _UNSAFE or unicodedata.category(c)[0] in "CZ" else c for c in text)
    text = " ".join(text.split()).strip(" .")
    while len(text.encode("utf-8")) > _NAME_BYTES:
        text = text[:-1]
    return text.rstrip(" .")


def default_name(read: bundle.Bundle, part: str, fmt: str) -> str:
    title = safe_name(read.summary_title or "") if read.summary is not None else ""
    if read.summary is None or (part == "transcript" and not title):
        stem = "Transcript"
    elif not title:
        stem = "Summary" if part == "summary" else "Summary and transcript"
    elif part == "summary":
        stem = title
    elif part == "transcript":
        stem = f"{title} (transcript)"
    else:
        stem = f"{title} (summary and transcript)"
    return safe_name(f"{_date(read)} {stem}") + f".{fmt}"


def _duration(seconds: object) -> str | None:
    if not isinstance(seconds, int) or seconds < 0:
        return None
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _facts(read: bundle.Bundle, with_summary: bool) -> list[tuple[str, str, str]]:
    files = read.attestation.get("files", {})
    started = read.session.get("started")
    found: list[tuple[str, str, object]] = [
        ("date", "Date", started),
        ("duration", "Length", _duration(read.session.get("duration_s"))),
        ("source", "Source", read.session.get("source")),
        ("language", "Language", read.session.get("language")),
        ("model", "Transcribed by", files.get(bundle.TRANSCRIPT, {}).get("model")),
    ]
    if with_summary:
        found.append(("summary_model", "Summarised by", files.get(bundle.SUMMARY, {}).get("model")))
    found.append(("vinowhisper_session", "Session", read.path.name))
    return [(key, label, str(value)) for key, label, value in found if value]


def _summary_blocks(summary: str, title: str | None) -> list[Block]:
    blocks: list[Block] = []
    skipped_title = False
    for raw in summary.splitlines():
        line = raw.strip()
        if not line:
            continue
        hashes = len(line) - len(line.lstrip("#"))
        if 1 <= hashes <= 6 and line[hashes:].startswith(" "):
            text = line[hashes:].strip()
            # The summary's own title is the document's.
            if not skipped_title and hashes == 1 and text == title:
                skipped_title = True
                continue
            blocks.append(Block("heading", _BOLD.sub(r"\1", text), min(hashes + 1, 6)))
        elif line.startswith(("- ", "* ")):
            blocks.append(Block("bullet", _BOLD.sub(r"\1", line[2:].strip())))
        elif len(line) > 2 and line.startswith("_") and line.endswith("_"):
            blocks.append(Block("note", line.strip("_").strip()))
        else:
            blocks.append(Block("paragraph", _BOLD.sub(r"\1", line)))
    return blocks


def _transcript_blocks(transcript: str) -> list[Block]:
    paragraphs = [p.strip() for p in transcript.split("\n\n") if p.strip()]
    # The header line is what the facts above already say.
    if paragraphs and paragraphs[0].startswith("vinoWhisper transcript, "):
        paragraphs = paragraphs[1:]
    return [Block("paragraph", " ".join(p.split("\n"))) for p in paragraphs]


def build(read: bundle.Bundle, part: str) -> Document:
    if part == "summary" and read.summary is None:
        raise ExportError(f"{read.path.name} has no summary yet; export --part transcript instead")
    with_summary = part in ("summary", "both") and read.summary is not None
    title = read.summary_title if read.summary is not None else None
    document = Document(
        title=title or f"Transcript, {_when(read)}",
        facts=_facts(read, with_summary),
        summary_missing=part == "both" and read.summary is None,
    )
    if with_summary:
        document.sections.append(("Summary", _summary_blocks(read.summary or "", title)))
    if part in ("transcript", "both"):
        document.sections.append(("Transcript", _transcript_blocks(read.transcript)))
    return document


def to_markdown(document: Document) -> str:
    lines = ["---"]
    lines += [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, _, value in document.facts]
    lines += ["tags: [vinowhisper]", "---", "", f"# {document.title}"]
    for name, blocks in document.sections:
        lines += ["", f"## {name}"]
        previous = ""
        for block in blocks:
            # Bullets in a row stay one list.
            if not (block.kind == "bullet" and previous == "bullet"):
                lines.append("")
            if block.kind == "heading":
                lines.append(f"{'#' * block.level} {block.text}")
            elif block.kind == "bullet":
                lines.append(f"- {block.text}")
            elif block.kind == "note":
                lines.append(f"_{block.text}_")
            else:
                lines.append(block.text)
            previous = block.kind
    return "\n".join(lines) + "\n"


def to_text(document: Document) -> str:
    lines = [document.title, ""]
    lines += [f"{label}: {value}" for _, label, value in document.facts]
    for name, blocks in document.sections:
        lines += ["", "", name.upper()]
        previous = ""
        for block in blocks:
            if not (block.kind == "bullet" and previous == "bullet"):
                lines.append("")
            lines.append(f"- {block.text}" if block.kind == "bullet" else block.text)
            previous = block.kind
    return "\n".join(lines) + "\n"


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _xml(text: str) -> str:
    return _escape(_XML_INVALID.sub("", text)).replace("\t", "<text:tab/>")


_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"'
)

_HEADINGS = "".join(
    f'<style:style style:name="Heading_20_{n}" style:display-name="Heading {n}" '
    f'style:family="paragraph" style:parent-style-name="Heading" '
    f'style:default-outline-level="{n}" style:class="text">'
    f'<style:text-properties fo:font-size="{size}pt"/></style:style>\n'
    for n, size in ((1, 18), (2, 15), (3, 13), (4, 12), (5, 11), (6, 11))
)

_STYLES = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-styles {_NS} office:version="1.3">
<office:styles>
<style:default-style style:family="paragraph"><style:text-properties fo:font-size="11pt"/></style:default-style>
<style:style style:name="Standard" style:family="paragraph" style:class="text"/>
<style:style style:name="Text_20_body" style:display-name="Text body" style:family="paragraph" style:parent-style-name="Standard" style:class="text"><style:paragraph-properties fo:margin-top="0cm" fo:margin-bottom="0.25cm" fo:line-height="115%"/></style:style>
<style:style style:name="Heading" style:family="paragraph" style:parent-style-name="Standard" style:next-style-name="Text_20_body" style:class="text"><style:paragraph-properties fo:margin-top="0.42cm" fo:margin-bottom="0.21cm" fo:keep-with-next="always"/><style:text-properties fo:font-weight="bold"/></style:style>
<style:style style:name="Title" style:family="paragraph" style:parent-style-name="Heading" style:class="chapter"><style:text-properties fo:font-size="22pt"/></style:style>
{_HEADINGS}<style:style style:name="Facts" style:family="paragraph" style:parent-style-name="Standard" style:class="text"><style:paragraph-properties fo:margin-bottom="0.1cm"/><style:text-properties fo:font-size="10pt" fo:color="#555555"/></style:style>
<style:style style:name="Note" style:family="paragraph" style:parent-style-name="Text_20_body" style:class="text"><style:text-properties fo:font-size="10pt" fo:font-style="italic"/></style:style>
</office:styles>
</office:document-styles>
"""

_AUTOMATIC = """<office:automatic-styles>
<text:list-style style:name="Bullets"><text:list-level-style-bullet text:level="1" text:bullet-char="•"><style:list-level-properties text:list-level-position-and-space-mode="label-alignment"><style:list-level-label-alignment text:label-followed-by="listtab" text:list-tab-stop-position="0.64cm" fo:text-indent="-0.64cm" fo:margin-left="0.64cm"/></style:list-level-properties></text:list-level-style-bullet></text:list-style>
</office:automatic-styles>
"""

_MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">
<manifest:file-entry manifest:full-path="/" manifest:version="1.3" manifest:media-type="application/vnd.oasis.opendocument.text"/>
<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
<manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>
<manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>
</manifest:manifest>
"""


def _content(document: Document) -> str:
    out = [f'<text:p text:style-name="Title">{_xml(document.title)}</text:p>']
    out += [
        f'<text:p text:style-name="Facts">{_xml(label)}: {_xml(value)}</text:p>'
        for _, label, value in document.facts
    ]
    for name, blocks in document.sections:
        out.append(
            f'<text:h text:style-name="Heading_20_1" text:outline-level="1">{_xml(name)}</text:h>'
        )
        in_list = False
        for block in blocks:
            if in_list and block.kind != "bullet":
                out.append("</text:list>")
                in_list = False
            if block.kind == "heading":
                out.append(
                    f'<text:h text:style-name="Heading_20_{block.level}" '
                    f'text:outline-level="{block.level}">{_xml(block.text)}</text:h>'
                )
            elif block.kind == "bullet":
                if not in_list:
                    out.append('<text:list text:style-name="Bullets">')
                    in_list = True
                out.append(
                    "<text:list-item>"
                    f'<text:p text:style-name="Text_20_body">{_xml(block.text)}</text:p>'
                    "</text:list-item>"
                )
            else:
                style = "Note" if block.kind == "note" else "Text_20_body"
                out.append(f'<text:p text:style-name="{style}">{_xml(block.text)}</text:p>')
        if in_list:
            out.append("</text:list>")
    body = "\n".join(out)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-content {_NS} '
        f'office:version="1.3">\n{_AUTOMATIC}<office:body><office:text>\n{body}\n'
        "</office:text></office:body></office:document-content>\n"
    )


def _meta(document: Document) -> str:
    facts = {key: value for key, _, value in document.facts}
    out = [
        f"<dc:title>{_xml(document.title)}</dc:title>",
        "<meta:generator>vinoWhisper</meta:generator>",
    ]
    if "date" in facts:
        out.append(f"<meta:creation-date>{_xml(facts['date'])}</meta:creation-date>")
    if "language" in facts:
        out.append(f"<dc:language>{_xml(facts['language'])}</dc:language>")
    out += [
        f'<meta:user-defined meta:name="{key}">{_xml(value)}</meta:user-defined>'
        for key, value in facts.items()
        if key != "date"
    ]
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-meta {_NS} office:version="1.3">'
        f"<office:meta>{''.join(out)}</office:meta></office:document-meta>\n"
    )


def to_odt(document: Document) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        # Like the bundle: mimetype first, stored, so `file` and the magic see an ODT.
        entries = [
            ("mimetype", ODT_MIMETYPE, zipfile.ZIP_STORED),
            ("META-INF/manifest.xml", _MANIFEST.encode(), zipfile.ZIP_DEFLATED),
            ("content.xml", _content(document).encode(), zipfile.ZIP_DEFLATED),
            ("styles.xml", _STYLES.encode(), zipfile.ZIP_DEFLATED),
            ("meta.xml", _meta(document).encode(), zipfile.ZIP_DEFLATED),
        ]
        for name, data, method in entries:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = method
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def render(document: Document, fmt: str) -> bytes:
    if fmt == "md":
        return to_markdown(document).encode("utf-8")
    if fmt == "txt":
        return to_text(document).encode("utf-8")
    if fmt == "odt":
        return to_odt(document)
    raise ExportError(f"unknown format {fmt!r}; one of {', '.join(FORMATS)}")


def format_for(out: Path | None, fmt: str | None) -> str:
    if fmt:
        return fmt
    suffix = out.suffix.lstrip(".").lower() if out is not None else ""
    return suffix if suffix in FORMATS else "md"


def _write(path: Path, data: bytes, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Leaving the private store on purpose: readable like any document.
        os.chmod(temp, 0o644)
        if force:
            os.replace(temp, path)
            return
        try:
            # A link fails if the name is taken, so two exports cannot clobber each other.
            os.link(temp, path)
        except FileExistsError:
            raise ExportError(f"{path} already exists; --force replaces it") from None
        except OSError:
            if path.exists():
                raise ExportError(f"{path} already exists; --force replaces it") from None
            os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _numbered(path: Path, n: int) -> Path:
    return path if n == 1 else path.with_name(f"{path.stem} ({n}){path.suffix}")


def _into_dir(out: Path | None) -> bool:
    return out is None or out.is_dir() or str(out).endswith(os.sep)


def target(read: bundle.Bundle, part: str, fmt: str, out: Path | None) -> Path:
    if out is not None and not _into_dir(out):
        return out
    return (out or default_dir()) / default_name(read, part, fmt)


def _from_session(path: Path, read: bundle.Bundle, fmt: str) -> bool:
    name = read.path.name
    try:
        if fmt == "odt":
            with zipfile.ZipFile(path) as archive:
                meta = archive.read("meta.xml").decode("utf-8", "replace")
            return f'meta:name="vinowhisper_session">{_escape(name)}<' in meta
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return False
    if fmt == "md":
        return f"vinowhisper_session: {json.dumps(name, ensure_ascii=False)}" in text
    return f"Session: {name}" in text


def export(
    read: bundle.Bundle,
    part: str,
    fmt: str,
    out: Path | None,
    force: bool = False,
    reuse: bool = False,
) -> Exported:
    document = build(read, part)
    data = render(document, fmt)
    first = target(read, part, fmt, out)
    if force or not _into_dir(out):
        _write(first, data, force)
        return Exported(first, document.parts, document.summary_missing, "written")
    # Into a directory nothing is replaced: a taken name gets " (2)", " (3)", ...
    for n in range(1, 100):
        candidate = _numbered(first, n)
        if reuse and candidate.is_file():
            if candidate.read_bytes() == data:
                return Exported(candidate, document.parts, document.summary_missing, "unchanged")
            # Edited since, or the summary changed: either way the file is the user's now.
            if _from_session(candidate, read, fmt):
                return Exported(candidate, document.parts, document.summary_missing, "edited")
            continue
        try:
            _write(candidate, data, False)
        except ExportError:
            if candidate.exists():
                continue
            raise
        return Exported(candidate, document.parts, document.summary_missing, "written")
    raise ExportError(f"{first} and 98 numbered copies already exist")
