"""Read requirement text and tables from ``docs/specs/`` at test time.

Tests that check code against the spec itself (rather than a copy of it) use these helpers
to pull out one requirement's lines, a section, the body rows of a Markdown table, and the
payload keys of the ``06-mqtt.md`` schema tables.
"""

import re
from functools import cache
from pathlib import Path

SPECS = Path(__file__).resolve().parents[2] / "docs" / "specs"


@cache
def spec_text(name: str) -> str:
    """Return the text of spec file ``name``."""
    return (SPECS / name).read_text(encoding="utf-8")


def requirement(name: str, ident: str) -> list[str]:
    """Return the lines of requirement ``ident`` in spec ``name``, up to the next one."""
    lines = spec_text(name).splitlines()
    start = next(i for i, line in enumerate(lines) if f"**{ident}**" in line)
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].startswith(("* **", "#", "Example:", "Template codes:"))
        ),
        len(lines),
    )
    return lines[start:end]


def section(name: str, heading: str) -> list[str]:
    """Return the lines of the section under ``heading``, up to the next heading."""
    lines = spec_text(name).splitlines()
    start = lines.index(heading)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("#"))
    return lines[start:end]


def table_rows(lines: list[str]) -> list[list[str]]:
    """Return the cells of each table body row whose first cell starts with a code span."""
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in lines
        if re.match(r"\s*\| `", line)
    ]


def expand(key_cell: str, name_cell: str = "") -> list[tuple[str, str]]:
    """Expand a key cell (and its name) into ``(key, name)`` pairs.

    Handles a single key, a comma list and a ``first … last`` numbered range.
    """
    keys = re.findall(r"`([^`]+)`", key_cell)
    if "…" not in key_cell:
        return [(key, name_cell) for key in keys]
    first, last = keys
    lo_match = re.search(r"[0-9]+", first)
    hi_match = re.search(r"[0-9]+", last)
    assert lo_match and hi_match
    width = len(lo_match.group())
    name_first = name_cell.split(" … ")[0]
    name_match = re.search(r"[0-9]+", name_first)
    pairs = []
    for n in range(int(lo_match.group()), int(hi_match.group()) + 1):
        key = f"{first[: lo_match.start()]}{n:0{width}d}{first[lo_match.end() :]}"
        name = (
            f"{name_first[: name_match.start()]}{n}{name_first[name_match.end() :]}"
            if name_match
            else name_cell
        )
        pairs.append((key, name))
    return pairs


def payload_rows(ident: str) -> list[tuple[str, list[str]]]:
    """Return each key of the ``06-mqtt.md`` schema table ``ident`` with its row's cells."""
    return [
        (key, cells)
        for cells in table_rows(requirement("06-mqtt.md", ident))
        for key, _ in expand(cells[0])
    ]


def payload_keys(ident: str) -> set[str]:
    """Return the keys of the ``06-mqtt.md`` schema table ``ident`` (MQTT-16, -17 or -18)."""
    return {key for key, _ in payload_rows(ident)}
