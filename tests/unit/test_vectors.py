"""Decoder tests driven by the vendored frame vectors in ``tests/vectors/`` (TEST-14..17).

The ``.expected.json`` files are written by hand from ``docs/specs/01-protocol.md`` so that a
decoder bug cannot be copied into its own expected values (TEST-16).
"""

import ast
import dataclasses
import json
import os
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from dyness_b3_bms_mqtt.protocol import (
    FrameClass,
    FrameCounters,
    ModuleFrame,
    StackFrame,
    classify,
    decode,
    is_poll,
)
from tests.vectors import loader
from tests.vectors.loader import SOURCE_PREFIX, VECTORS, TxLine, VectorLine, load, load_expected

TESTS = Path(__file__).resolve().parents[1]
REPO = TESTS.parent
EXAMPLES = ("t1-examples", "t2-examples")
CAPTURES = ("t1-listen-only-sniff", "t2-poll-60s")
SPEC_MODULE_CODES = frozenset({0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x21, 0x22, 0x23, 0x24})
SPEC_STACK_IDS = frozenset({0x351, 0x355, 0x356, 0x359, 0x35C, 0x35F, 0x70D})
SPEC_IGNORED_IDS = frozenset({0x305, 0x30F, 0x35A, 0x35E, 0x370, 0x371})
SPEC_HEARTBEAT_ID = 0x18F20103
DECODED = (FrameClass.MODULE, FrameClass.STACK)


def frames(lines: list[VectorLine | TxLine]) -> list[VectorLine]:
    return [line for line in lines if isinstance(line, VectorLine)]


def normalise(frame: ModuleFrame | StackFrame) -> dict[str, Any]:
    """Render a decoded frame in the shape of an ``.expected.json`` entry."""
    out: dict[str, Any] = {"frame": type(frame).__name__}
    for field in dataclasses.fields(frame):
        value = getattr(frame, field.name)
        if field.name == "code":
            value = f"0x{value:02x}"
        elif field.name == "can_id":
            value = f"0x{value:x}"
        elif isinstance(value, Mapping):
            value = {str(key): item for key, item in value.items()}
        elif isinstance(value, tuple):
            value = list(value)
        out[field.name] = value
    return out


def observed(line: VectorLine) -> dict[str, Any]:
    cls = classify(line.can_id, line.extended, line.data)
    frame = decode(line.can_id, line.extended, line.data)
    result: dict[str, Any] = {"class": cls.name}
    if frame is not None:
        result.update(normalise(frame))
    return result


@pytest.mark.req(
    "PROTO-02",
    "PROTO-07",
    "PROTO-08",
    "PROTO-09",
    "PROTO-10",
    "PROTO-11",
    "PROTO-12",
    "PROTO-13",
    "PROTO-14",
    "PROTO-15",
    "PROTO-17",
    "PROTO-19",
    "PROTO-20",
    "PROTO-21",
    "PROTO-22",
    "PROTO-23",
    "PROTO-24",
    "PROTO-32",
    "PROTO-33",
    "TEST-15",
    "TEST-16",
)
@pytest.mark.parametrize("name", [*EXAMPLES, "edge", "synthetic"])
def test_every_vector_matches_expected(name: str) -> None:
    path = VECTORS / f"{name}.txt"
    _, lines = load(path)
    expected = load_expected(path)
    received = frames(lines)

    keys = [line.key for line in received]
    assert len(keys) == len(set(keys)), "each vector file lists a frame once"
    assert set(keys) == set(expected), "every frame has exactly one expected entry"
    for line in received:
        actual = observed(line)
        assert actual == expected[line.key], f"line {line.lineno}: {line.key}"
        assert json.dumps(actual) == json.dumps(expected[line.key]), (
            f"line {line.lineno}: types, key order or the sign of zero differ"
        )


@pytest.mark.req("PROTO-01", "PROTO-02", "PROTO-04", "PROTO-32", "TEST-17")
def test_synthetic_frames_are_dropped_and_counted() -> None:
    _, lines = load(VECTORS / "synthetic.txt")
    received = frames(lines)
    counters = FrameCounters()
    classes: Counter[FrameClass] = Counter()
    for line in received:
        cls = classify(line.can_id, line.extended, line.data)
        assert decode(line.can_id, line.extended, line.data) is None, line.key
        counters.count(cls)
        classes[cls] += 1

    assert len(received) == 33
    assert classes == {
        FrameClass.MALFORMED: 6,
        FrameClass.ECHO: 2,
        FrameClass.HEARTBEAT: 1,
        FrameClass.UNKNOWN: 24,
    }
    assert (counters.malformed_frames, counters.echo_frames, counters.unknown_frames) == (6, 2, 24)
    assert any(line.can_id == 0x18F26677 for line in received)


@pytest.mark.req("PROTO-32", "TEST-15")
@pytest.mark.parametrize(
    "path", [VECTORS / "t2-examples.txt", VECTORS / "captures/t2-poll-60s.txt"]
)
def test_each_echo_follows_its_tx(path: Path) -> None:
    _, lines = load(path)
    pending: TxLine | None = None
    pairs = 0
    for line in lines:
        if isinstance(line, TxLine):
            assert pending is None, f"line {line.lineno}: TX before the previous echo"
            assert is_poll(line.can_id, True, line.data), f"line {line.lineno}: TX is a poll"
            pending = line
        elif classify(line.can_id, line.extended, line.data) is FrameClass.ECHO:
            assert pending is not None, f"line {line.lineno}: echo without a TX"
            assert (line.can_id, line.data) == (pending.can_id, pending.data)
            pending, pairs = None, pairs + 1
    assert pending is None
    assert pairs == sum(isinstance(line, TxLine) for line in lines) > 0


@pytest.mark.req("TEST-14", "TEST-15")
def test_vectors_cover_every_frame_type_for_modules_1_to_3() -> None:
    received: list[VectorLine] = []
    tx: list[TxLine] = []
    for name in EXAMPLES:
        header, lines = load(VECTORS / f"{name}.txt")
        assert header["source"].startswith(SOURCE_PREFIX)
        assert header["lines"]
        received += frames(lines)
        tx += [line for line in lines if isinstance(line, TxLine)]

    for module in (1, 2, 3):
        codes = {
            line.can_id & 0xFF
            for line in received
            if line.extended and (line.can_id & 0xFFFFFF00) == (0x18F21000 | (module << 8))
        }
        assert codes >= SPEC_MODULE_CODES, f"module {module} misses {SPEC_MODULE_CODES - codes}"
    standard = {line.can_id for line in received if not line.extended}
    assert standard >= SPEC_STACK_IDS | SPEC_IGNORED_IDS
    assert any(line.extended and line.can_id == SPEC_HEARTBEAT_ID for line in received)
    echoes = {(line.can_id, line.data) for line in received if is_poll(*_frame(line))}
    assert {(line.can_id, line.data) for line in tx} <= echoes
    assert tx


@pytest.mark.req("PROTO-33", "PROTO-04")
@pytest.mark.parametrize("name", CAPTURES)
def test_capture_replay_has_no_malformed_or_unknown(name: str) -> None:
    path = VECTORS / "captures" / f"{name}.txt"
    header, lines = load(path)
    expected = load_expected(path)
    assert header["source"] == f"{SOURCE_PREFIX}{name}.txt"

    counters = FrameCounters()
    classes = dict.fromkeys((cls.name for cls in FrameClass), 0)
    for line in frames(lines):
        cls = classify(*_frame(line))
        counters.count(cls)
        classes[cls.name] += 1
        assert (decode(*_frame(line)) is not None) == (cls in DECODED), line.lineno

    assert classes == expected["classes"]
    assert sum(isinstance(line, TxLine) for line in lines) == expected["tx"]
    assert counters.malformed_frames == counters.unknown_frames == 0


@pytest.mark.req("TEST-14")
def test_tests_never_reference_dyness_debug_at_runtime() -> None:
    needle = "-".join(("dyness", "debug"))
    label = (Path(loader.__file__).resolve(), SOURCE_PREFIX)
    offenders: list[str] = []
    for source in sorted(TESTS.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        docstrings = {id(node) for node in _docstrings(tree)}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and (needle in node.value or node.value.startswith(os.pardir))
                and (source, node.value) != label
            ):
                offenders.append(f"{source.relative_to(REPO)}:{node.lineno}: {node.value!r}")
    assert offenders == []
    assert VECTORS.is_relative_to(REPO)


def _frame(line: VectorLine) -> tuple[int, bool, bytes]:
    return line.can_id, line.extended, line.data


def _docstrings(tree: ast.Module) -> list[ast.Constant]:
    found: list[ast.Constant] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.append(first.value)
    return found
