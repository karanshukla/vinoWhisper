import sys

from rich.console import Console
from rich.text import Text

TITLE = "bold cyan"
HEADING = "bold magenta"
OK = "bold green"
WARN = "bold yellow"
FAIL = "bold red"
MUTED = "dim"
COMMAND = "cyan"
LABEL = "bold"


def emit(*parts: str | tuple[str, str], end: str = "\n", stderr: bool = False) -> None:
    # Built per call so it follows whatever sys.stdout is now; Rich drops the
    # colour itself when the stream is not a terminal or NO_COLOR is set.
    console = Console(file=sys.stderr if stderr else sys.stdout, highlight=False, markup=False)
    text = Text(end="")
    for part in parts:
        if isinstance(part, tuple):
            text.append(part[0], style=part[1])
        else:
            text.append(part)
    console.print(text, end=end, soft_wrap=True)
    console.file.flush()


def styled_line(line: str) -> tuple[str, str] | str:
    stripped = line.lstrip()
    if stripped.startswith("$ "):
        return (line, COMMAND)
    if stripped.startswith("⚠"):
        return (line, WARN)
    if stripped.startswith("✗"):
        return (line, FAIL)
    return line
