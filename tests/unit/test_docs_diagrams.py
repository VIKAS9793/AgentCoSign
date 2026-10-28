"""Diagrams in the published Markdown must render on GitHub.

GitHub draws ```mermaid blocks with Mermaid, whose parser ends a statement at
a semicolon and, in a sequence diagram, reads ``#`` as the start of a character
code, so a message that contains either breaks the whole diagram. Hidden
control or invisible characters break it the same way and cannot be seen in a
review.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| A semicolon anywhere in a diagram | Refused |
| A ``#`` in a sequence diagram | Refused (colour codes elsewhere are fine) |
| A control or invisible character | Refused |
| A file with no diagrams | Nothing to check |
"""

import re
import subprocess
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BLOCK = re.compile(r"```mermaid\n(.*?)```", re.S)
INVISIBLE = {"Cc", "Cf", "Zl", "Zp"}


def _tracked_markdown() -> list[Path]:
    listed = subprocess.run(  # noqa: S603 -- fixed git command, no user input
        ["git", "ls-files", "*.md"],  # noqa: S607 -- git on the path, as in CI
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [ROOT / name for name in listed.stdout.split()]


def problems(block: str) -> list[str]:
    """Why GitHub would fail to draw this diagram, one reason per bad line."""
    found = []
    sequence = block.lstrip().startswith("sequenceDiagram")
    for number, line in enumerate(block.split("\n"), 1):
        if ";" in line:
            found.append(f"line {number}: a semicolon ends the statement early")
        if sequence and "#" in line:
            found.append(f"line {number}: '#' starts a character code")
        hidden = [c for c in line if c != "\t" and unicodedata.category(c) in INVISIBLE]
        if hidden:
            found.append(f"line {number}: hidden character U+{ord(hidden[0]):04X}")
    return found


def _diagrams() -> list[tuple[str, str]]:
    return [
        (f"{path.relative_to(ROOT)}#{n}", match.group(1))
        for path in _tracked_markdown()
        for n, match in enumerate(BLOCK.finditer(path.read_text(encoding="utf-8")), 1)
    ]


@pytest.mark.edge
@pytest.mark.parametrize(("where", "block"), _diagrams())
def test_every_published_diagram_can_be_drawn(where: str, block: str) -> None:
    assert problems(block) == [], where


@pytest.mark.edge
@pytest.mark.parametrize(
    ("block", "reason"),
    [
        ("sequenceDiagram\n    G->>P: read again; then send", "semicolon"),
        ("sequenceDiagram\n    G->>P: order #42", "character code"),
        ("graph TD\n    A[one​two] --> B", "hidden character"),
        ("graph TD\n    A --> B\n    style A fill:#f4e1d2", None),
    ],
)
def test_the_check_names_what_breaks_a_diagram(block: str, reason: str | None) -> None:
    found = problems(block)
    if reason is None:
        assert found == []
    else:
        assert len(found) == 1
        assert reason in found[0]
