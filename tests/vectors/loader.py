"""Reader for the vendored vector files in ``tests/vectors/``.

Every vector file keeps the capture line format of ``dyness-debug/data/2026-10-08/``
(TEST-15): ``<time> <ID hex> <X|S> <data bytes hex>`` for a received frame (``X`` = 29-bit
extended, ``S`` = 11-bit standard) and ``<time> TX <ID hex> <data bytes hex>`` for a frame
the capture tool transmitted. Lines starting with ``#`` are comments; the leading run of
``# key: value`` comments (lower-case key) is the header naming the source file and line range.

Each ``<name>.txt`` has a sibling ``<name>.expected.json`` written by hand from
``docs/specs/01-protocol.md`` and keyed by :attr:`VectorLine.key`.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VECTORS = Path(__file__).resolve().parent
SOURCE_PREFIX = "dyness-debug/data/2026-10-08/"
"""Header label naming the capture a vendored file came from; never resolved as a path."""
HEADER_FIELD = re.compile(r"#\s*(?P<key>[a-z]+):\s*(?P<value>.*)")


@dataclass(frozen=True, slots=True)
class VectorLine:
    """A received frame: ``key`` is the line without its timestamp, ``<ID> <X|S> <bytes>``."""

    lineno: int
    time: float
    can_id: int
    extended: bool
    data: bytes
    key: str


@dataclass(frozen=True, slots=True)
class TxLine:
    """A frame the capture tool transmitted (always extended)."""

    lineno: int
    time: float
    can_id: int
    data: bytes


def load(path: Path) -> tuple[dict[str, str], list[VectorLine | TxLine]]:
    """Parse a vector file into its header and its frame lines, in file order.

    Raises ``ValueError`` naming the line number for any line that is neither a comment,
    blank, a received frame nor a ``TX`` line.
    """
    header: dict[str, str] = {}
    lines: list[VectorLine | TxLine] = []
    in_header = True
    for lineno, raw in enumerate(path.read_text(encoding="ascii").splitlines(), start=1):
        text = raw.strip()
        if text.startswith("#"):
            field = HEADER_FIELD.fullmatch(text)
            if in_header and field:
                header[field["key"]] = field["value"]
            else:
                in_header = False
            continue
        if not text:
            continue
        in_header = False
        lines.append(_parse(lineno, text))
    return header, lines


def load_expected(path: Path) -> dict[str, Any]:
    """Return the ``.expected.json`` sibling of the vector file ``path``.

    Raises ``ValueError`` if any JSON object in it repeats a key, which ``json`` would
    otherwise resolve silently by keeping the last value.
    """
    expected: dict[str, Any] = json.loads(
        path.with_suffix(".expected.json").read_text(encoding="utf-8"),
        object_pairs_hook=_unique_keys,
    )
    return expected


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"duplicate keys in expected file: {duplicates}")
    return dict(pairs)


def _parse(lineno: int, text: str) -> VectorLine | TxLine:
    tokens = text.split()
    try:
        if len(tokens) >= 3 and tokens[1] == "TX":
            return TxLine(lineno, float(tokens[0]), int(tokens[2], 16), _bytes(tokens[3:]))
        if len(tokens) >= 3 and tokens[2] in ("X", "S"):
            return VectorLine(
                lineno=lineno,
                time=float(tokens[0]),
                can_id=int(tokens[1], 16),
                extended=tokens[2] == "X",
                data=_bytes(tokens[3:]),
                key=" ".join(tokens[1:]),
            )
    except ValueError as exc:
        raise ValueError(f"line {lineno}: {exc}: {text!r}") from exc
    raise ValueError(f"line {lineno}: not a frame line: {text!r}")


def _bytes(tokens: list[str]) -> bytes:
    if any(len(token) != 2 for token in tokens):
        raise ValueError("data bytes must be two hex digits each")
    return bytes.fromhex("".join(tokens))
