"""Tests of frame classification, decoding and the poll guard (``protocol``): 01-protocol.md.

Expected values and tables are restated here from the spec rather than imported from the
module under test, so a wrong constant in ``protocol.py`` cannot confirm itself.
"""

import ast
import dataclasses
import enum
import inspect
import math
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dyness_b3_bms_mqtt import protocol
from dyness_b3_bms_mqtt.protocol import (
    BUS_ACTIVITY,
    FLAG_CATEGORY,
    FLAG_NAMES,
    FLAG_TABLE,
    CellExtremes,
    CellTemperatures,
    CellVoltages,
    ChargeLimits,
    ChargeRequest,
    EnergyThroughput,
    FlagCategory,
    FrameClass,
    FrameCounters,
    ModuleFrame,
    MosBoardCycles,
    PackStatus,
    PollFrame,
    StackCapacity,
    StackMeasurements,
    StackModuleCount,
    StackSocSoh,
    StackWords,
    StatusFlags,
    TempExtremes,
    build_poll,
    classify,
    decode,
    is_poll,
)

POLL = bytes((0x01, 0, 0, 0, 0, 0, 0, 0))
POLL_IDS = {1: 0x18F21177, 2: 0x18F21277, 3: 0x18F21377, 4: 0x18F21477}
PARAMETER_WRITE = 0x18F26677
SPEC_MODULE_CODES = frozenset({0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x21, 0x22, 0x23, 0x24})
SPEC_STACK_IDS = (0x351, 0x355, 0x356, 0x359, 0x35C, 0x35F, 0x70D)
SPEC_IGNORED_IDS = (0x305, 0x30F, 0x35A, 0x35E, 0x370, 0x371)
HEARTBEAT = 0x18F20103
PACK = bytes.fromhex("136e0f6f0cf40177")
SPEC_FLAGS = [
    ("CellV_CutH", 0, 0, "protection"),
    ("CellV_CutL", 0, 1, "protection"),
    ("PackV_CutH", 0, 2, "protection"),
    ("PackV_CutL", 0, 3, "protection"),
    ("Temp_CutH", 0, 4, "protection"),
    ("Temp_CutL", 0, 5, "protection"),
    ("Cur_CutH", 0, 6, "protection"),
    ("CellV_AlarmH", 1, 0, "alarm"),
    ("CellV_AlarmL", 1, 1, "alarm"),
    ("PackV_AlarmH", 1, 2, "alarm"),
    ("PackV_AlarmL", 1, 3, "alarm"),
    ("Temp_AlarmH", 1, 4, "alarm"),
    ("Temp_AlarmL", 1, 5, "alarm"),
    ("Cur_AlarmH", 1, 6, "alarm"),
    ("DisMOS_on", 2, 0, "status"),
    ("ChgMOS_on", 2, 1, "status"),
    ("Short", 2, 2, "fault"),
    ("PwrDown", 2, 3, "fault"),
    ("MOS_TempH", 2, 4, "fault"),
    ("InternalComErr", 2, 5, "fault"),
    ("Reverse", 2, 6, "fault"),
    ("Balancing", 2, 7, "status"),
    ("CellDiff_Cut", 3, 4, "protection"),
    ("CellDiff_Alarm", 3, 5, "alarm"),
    ("InvalidCell", 4, 0, "fault"),
    ("ChgMOS_Err", 4, 4, "fault"),
    ("DisMOS_Err", 4, 5, "fault"),
]
DETERMINISTIC = settings(derandomize=True, database=None, deadline=None, max_examples=1000)


class Module(enum.IntEnum):
    ONE = 1


def poll(module: object, module_count: object) -> PollFrame:
    """Call ``build_poll`` with arguments of any type, as a careless caller might."""
    return build_poll(cast(int, module), cast(int, module_count))


def assert_is_poll_of(frame: PollFrame, module: int) -> None:
    assert type(frame) is PollFrame
    assert frame.arbitration_id == POLL_IDS[module] == 0x18F21077 | (module << 8)
    assert frame.arbitration_id != PARAMETER_WRITE
    assert type(frame.data) is bytes
    assert frame.data == POLL
    assert frame.extended is True
    assert is_poll(frame.arbitration_id, True, frame.data)
    assert classify(frame.arbitration_id, True, frame.data) is FrameClass.ECHO


def expected_module_range_class(can_id: int, extended: bool, data: bytes) -> FrameClass:
    """Classify a ``0x18F21???`` frame from the 01-protocol.md section 2 table."""
    module, code = (can_id >> 8) & 0xF, can_id & 0xFF
    if extended and code == 0x77 and data == POLL:
        return FrameClass.ECHO
    if extended and 1 <= module <= 4 and code in SPEC_MODULE_CODES:
        return FrameClass.MODULE if len(data) == 8 else FrameClass.MALFORMED
    return FrameClass.UNKNOWN


IDS = st.one_of(
    st.integers(0, 0x1FFFFFFF),
    st.integers(0x18F20000, 0x18F2FFFF),
    st.integers(0, 0x7FF),
    st.sampled_from([*SPEC_STACK_IDS, *SPEC_IGNORED_IDS, HEARTBEAT, PARAMETER_WRITE]),
)
DATA = st.one_of(st.binary(max_size=64), st.binary(min_size=8, max_size=8), st.just(POLL))


@pytest.mark.req("PROTO-02", "PROTO-03")
@DETERMINISTIC
@given(can_id=IDS, extended=st.booleans(), data=DATA)
def test_classify_and_decode_never_raise(can_id: int, extended: bool, data: bytes) -> None:
    cls = classify(can_id, extended, data)
    frame = decode(can_id, extended, data)
    assert isinstance(cls, FrameClass)
    assert (frame is not None) == (cls in (FrameClass.MODULE, FrameClass.STACK))
    assert classify(can_id, extended, bytearray(data)) is cls
    assert decode(can_id, extended, bytearray(data)) == frame


@pytest.mark.req("PROTO-02", "PROTO-03", "PROTO-04", "PROTO-07", "PROTO-32")
def test_module_range_never_raises() -> None:
    payloads = [b"", b"\x01", POLL[:7], POLL, bytes(8), b"\xff" * 8, b"\xff" * 9, b"\xa5" * 64]
    for can_id in range(0x18F21000, 0x18F22000):
        for extended in (True, False):
            for data in payloads:
                cls = classify(can_id, extended, data)
                assert cls is expected_module_range_class(can_id, extended, data), (
                    f"{can_id:08X} {extended} {data.hex()}"
                )
                frame = decode(can_id, extended, data)
                assert (frame is not None) == (cls is FrameClass.MODULE)
                if frame is not None:
                    assert isinstance(frame, ModuleFrame)
                    assert (frame.module, frame.code) == ((can_id >> 8) & 0xF, can_id & 0xFF)


@pytest.mark.req("PROTO-01", "PROTO-23", "PROTO-24")
def test_identifier_and_extended_flag_both_matter() -> None:
    for can_id in SPEC_STACK_IDS:
        assert classify(can_id, False, bytes(8)) is FrameClass.STACK
        assert classify(can_id, False, bytes(7)) is FrameClass.MALFORMED
        assert classify(can_id, True, bytes(8)) is FrameClass.UNKNOWN
        assert classify(can_id, True, bytes(7)) is FrameClass.UNKNOWN
        assert decode(can_id, True, bytes(8)) is None
    for can_id in SPEC_IGNORED_IDS:
        assert classify(can_id, False, bytes(8)) is FrameClass.IGNORED
        assert classify(can_id, True, bytes(8)) is FrameClass.UNKNOWN
        assert decode(can_id, False, bytes(8)) is None
    assert classify(HEARTBEAT, True, bytes(8)) is FrameClass.HEARTBEAT
    assert classify(HEARTBEAT, True, b"\x01") is FrameClass.HEARTBEAT
    assert classify(HEARTBEAT, False, bytes(8)) is FrameClass.UNKNOWN
    assert classify(0x18F21121, True, PACK) is FrameClass.MODULE
    assert classify(0x18F21121, False, PACK) is FrameClass.UNKNOWN
    assert decode(0x18F21121, False, PACK) is None
    assert classify(0x18F21177, True, POLL) is FrameClass.ECHO
    assert classify(0x18F21177, False, POLL) is FrameClass.UNKNOWN
    assert not is_poll(0x18F21177, False, POLL)


@pytest.mark.req("PROTO-04", "PROTO-05")
def test_bus_activity_excludes_only_echo() -> None:
    assert {cls.name for cls in FrameClass} == {
        "ECHO",
        "MODULE",
        "STACK",
        "HEARTBEAT",
        "IGNORED",
        "MALFORMED",
        "UNKNOWN",
    }
    assert frozenset(FrameClass) - BUS_ACTIVITY == {FrameClass.ECHO}
    assert FrameClass.ECHO not in BUS_ACTIVITY

    counters = FrameCounters()
    for cls, times in zip(FrameClass, range(1, 8), strict=True):
        for _ in range(times):
            counters.count(cls)
    by_name = {cls.name: times for cls, times in zip(FrameClass, range(1, 8), strict=True)}
    assert counters == FrameCounters(
        echo_frames=by_name["ECHO"],
        malformed_frames=by_name["MALFORMED"],
        unknown_frames=by_name["UNKNOWN"],
    )


@pytest.mark.req("PROTO-06")
def test_module_4_decodes_without_module_count() -> None:
    assert decode(0x18F21421, True, PACK) == PackStatus(
        module=4,
        code=0x21,
        pack_v=49.74,
        current_raw_a=-4.9,
        avg_cell_mv=3316,
        max_discharge_a=37.5,
    )
    assert decode(0x18F21411, True, bytes.fromhex("0cf90cf80cf40cfa")) == CellVoltages(
        module=4, code=0x11, cells_mv={1: 3321, 2: 3320, 3: 3316, 4: 3322}
    )
    assert list(inspect.signature(decode).parameters) == ["can_id", "extended", "data"]
    assert list(inspect.signature(classify).parameters) == ["can_id", "extended", "data"]

    tree = ast.parse(Path(protocol.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not any("config" in name or "dyness" in name for name in imported), imported


@pytest.mark.req("PROTO-07", "PROTO-08", "PROTO-11", "PROTO-13", "PROTO-14", "PROTO-18")
def test_worked_example_details() -> None:
    frame = decode(0x18F21121, True, PACK)
    assert frame == PackStatus(
        module=1,
        code=0x21,
        pack_v=49.74,
        current_raw_a=-4.9,
        avg_cell_mv=3316,
        max_discharge_a=37.5,
    )
    assert isinstance(frame, PackStatus)
    assert repr(frame.pack_v) == "49.74"
    assert repr(frame.current_raw_a) == "-4.9"

    module_2 = decode(0x18F21221, True, bytes.fromhex("13620f760cec0000"))
    assert isinstance(module_2, PackStatus)
    assert (module_2.module, module_2.code, module_2.current_raw_a) == (2, 0x21, -4.2)
    module_3 = decode(0x18F21321, True, bytes.fromhex("136f0f780cf40000"))
    assert isinstance(module_3, PackStatus)
    stack = decode(0x356, False, bytes.fromhex("6a138300b4000000"))
    assert isinstance(stack, StackMeasurements)
    module_sum = round(frame.current_raw_a + module_2.current_raw_a + module_3.current_raw_a, 1)
    assert module_sum == -13.1
    assert stack.current_a == 13.1 == -module_sum

    zero = decode(0x18F21121, True, bytes.fromhex("136e0fa00cf40000"))
    assert isinstance(zero, PackStatus)
    assert zero.current_raw_a == 0.0
    assert math.copysign(1.0, zero.current_raw_a) == 1.0
    assert math.copysign(1.0, zero.max_discharge_a) == 1.0

    for wire, number in ((0, 1), (5, 6), (14, 15), (15, None), (255, None)):
        data = bytes((0x0C, 0xF8, wire, 0x0C, 0xE0, wire, 0x01, 0x77))
        extremes = decode(0x18F21122, True, data)
        assert extremes == CellExtremes(
            module=1,
            code=0x22,
            cell_max_mv=3320,
            cell_max_no=number,
            cell_min_mv=3296,
            cell_min_no=number,
            max_charge_a=37.5,
        )

    assert decode(0x18F21115, True, bytes.fromhex("000001900190ffff")) == CellTemperatures(
        module=1, code=0x15, temps_c=(-40.0, 0.0, 0.0)
    )
    assert decode(0x18F21116, True, bytes.fromhex("027e0253040b0000")) == MosBoardCycles(
        module=1, code=0x16, mos_c=23.8, board_c=19.5, cycles=1035
    )
    assert decode(0x18F21117, True, bytes.fromhex("0005874000000000")) == EnergyThroughput(
        module=1, code=0x17, energy_kwh=3623.04
    )
    assert decode(0x18F21123, True, bytes.fromhex("024401023e026125")) == TempExtremes(
        module=1,
        code=0x23,
        temp_max_c=18.0,
        temp_max_sensor_raw=1,
        temp_min_c=17.4,
        temp_min_sensor_raw=2,
        soh=97,
        soc=37,
    )
    cells = decode(0x18F21114, True, bytes.fromhex("0cf30cf80cf80cf9"))
    assert isinstance(cells, CellVoltages)
    assert dict(cells.cells_mv) == {13: 3315, 14: 3320, 15: 3320}


@pytest.mark.req("PROTO-15", "PROTO-16")
def test_flag_table_and_categories() -> None:
    table = [(name, byte, bit, str(category)) for name, byte, bit, category in FLAG_TABLE]
    assert table == SPEC_FLAGS
    assert tuple(name for name, *_ in SPEC_FLAGS) == FLAG_NAMES
    assert dict(FLAG_CATEGORY) == {name: category for name, _, _, category in SPEC_FLAGS}
    assert {str(category) for category in FlagCategory} == {
        "protection",
        "alarm",
        "status",
        "fault",
    }
    assert all(type(category) is FlagCategory for category in FLAG_CATEGORY.values())
    with pytest.raises(TypeError):
        FLAG_CATEGORY["Short"] = FlagCategory.STATUS  # type: ignore[index]

    for name, byte, bit, _ in SPEC_FLAGS:
        data = bytearray(8)
        data[byte] = 1 << bit
        frame = decode(0x18F21324, True, bytes(data))
        assert isinstance(frame, StatusFlags)
        assert list(frame.flags) == list(FLAG_NAMES)
        assert {flag for flag, on in frame.flags.items() if on} == {name}
        assert frame.flags_raw == bytes(data).hex()

    named = {(byte, bit) for _, byte, bit, _ in SPEC_FLAGS}
    for byte in range(8):
        for bit in range(8):
            if (byte, bit) in named:
                continue
            data = bytearray(8)
            data[byte] = 1 << bit
            frame = decode(0x18F21124, True, bytes(data))
            assert isinstance(frame, StatusFlags)
            assert not any(frame.flags.values()), (byte, bit)
            assert frame.flags_raw == bytes(data).hex()


@pytest.mark.req("PROTO-17", "PROTO-18", "PROTO-19", "PROTO-20", "PROTO-21", "PROTO-22")
def test_stack_frames_need_no_reinterpretation() -> None:
    fields = {
        cls.__name__: [field.name for field in dataclasses.fields(cls)]
        for cls in (
            ChargeLimits,
            StackSocSoh,
            StackMeasurements,
            StackModuleCount,
            ChargeRequest,
            StackCapacity,
            StackWords,
        )
    }
    assert fields == {
        "ChargeLimits": ["can_id", "chg_v", "chg_a", "dis_a", "dis_v"],
        "StackSocSoh": ["can_id", "soc", "soh"],
        "StackMeasurements": ["can_id", "voltage_v", "current_a", "temp_c"],
        "StackModuleCount": ["can_id", "module_count", "raw_hex"],
        "ChargeRequest": ["can_id", "request_raw", "charge_enable", "discharge_enable"],
        "StackCapacity": ["can_id", "capacity_ah"],
        "StackWords": ["can_id", "words"],
    }

    assert decode(0x356, False, bytes.fromhex("6a138300b4000000")) == StackMeasurements(
        can_id=0x356, voltage_v=49.7, current_a=13.1, temp_c=18.0
    )
    assert decode(0x351, False, bytes.fromhex("12029bfb89fec201")) == ChargeLimits(
        can_id=0x351, chg_v=53.0, chg_a=-112.5, dis_a=-37.5, dis_v=45.0
    )
    assert decode(0x355, False, bytes.fromhex("0101c80000000000")) == StackSocSoh(
        can_id=0x355, soc=257, soh=200
    )
    assert decode(0x359, False, bytes.fromhex("ffffffff02ffffff")) == StackModuleCount(
        can_id=0x359, module_count=2, raw_hex="ffffffff02ffffff"
    )
    for raw, charge, discharge in ((0xC0, True, True), (0x80, True, False), (0x40, False, True)):
        assert decode(0x35C, False, bytes((raw, 0, 0, 0, 0, 0, 0, 0))) == ChargeRequest(
            can_id=0x35C, request_raw=raw, charge_enable=charge, discharge_enable=discharge
        )
    assert decode(0x35C, False, bytes.fromhex("3f00000000000000")) == ChargeRequest(
        can_id=0x35C, request_raw=0x3F, charge_enable=False, discharge_enable=False
    )
    assert decode(0x35F, False, bytes.fromhex("ffff8803e100ffff")) == StackCapacity(
        can_id=0x35F, capacity_ah=225
    )
    assert decode(0x70D, False, bytes.fromhex("64000f006900e100")) == StackWords(
        can_id=0x70D, words=(100, 15, 105, 225)
    )


@pytest.mark.req("PROTO-25", "PROTO-26", "PROTO-27", "PROTO-29")
def test_build_poll_exhaustive_grid() -> None:
    built = set()
    for module in range(-1, 21):
        for module_count in range(-1, 7):
            if 1 <= module_count <= 4 and 1 <= module <= module_count:
                frame = build_poll(module, module_count)
                assert_is_poll_of(frame, module)
                built.add(frame.arbitration_id)
            else:
                with pytest.raises(ValueError):
                    build_poll(module, module_count)
    assert built == set(POLL_IDS.values())

    for bad in (True, False, 1.0, 2.0, "1", None, 0x56, b"\x01", Module.ONE, [1]):
        with pytest.raises(ValueError):
            poll(bad, 3)
        with pytest.raises(ValueError):
            poll(1, bad)
    with pytest.raises(ValueError):
        poll(True, True)


@pytest.mark.req("PROTO-25", "PROTO-27", "PROTO-28")
def test_parameter_write_frame_is_unconstructible() -> None:
    assert PARAMETER_WRITE == 0x18F21000 + (0x56 << 8) + 0x77
    for module_count in range(-1, 0x100):
        with pytest.raises(ValueError):
            build_poll(0x56, module_count)

    accepted = set()
    for can_id in range(0x18F20000, 0x18F30000):
        try:
            PollFrame(can_id, POLL)
        except ValueError:
            continue
        accepted.add(can_id)
    assert accepted == set(POLL_IDS.values())
    for can_id in (PARAMETER_WRITE, 0x177, 0x18F21077, 0x18F21577, 0x18F21F77):
        with pytest.raises(ValueError):
            PollFrame(can_id, POLL)
    for data in (bytes(8), POLL[:7], POLL + b"\x00", b"\x01\x00\x00\x00\x00\x00\x00\x01"):
        with pytest.raises(ValueError):
            PollFrame(0x18F21177, data)
    with pytest.raises(ValueError):
        PollFrame(0x18F21177, cast(bytes, bytearray(POLL)))
    with pytest.raises(ValueError):
        PollFrame(cast(int, float(0x18F21177)), POLL)

    frame = build_poll(1, 1)
    with pytest.raises(ValueError):
        dataclasses.replace(frame, arbitration_id=PARAMETER_WRITE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.arbitration_id = PARAMETER_WRITE  # type: ignore[misc]


@pytest.mark.req("PROTO-26", "PROTO-27", "PROTO-28")
def test_guard_is_not_an_assert() -> None:
    tree = ast.parse(Path(protocol.__file__).read_text(encoding="utf-8"))
    assert not [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)]

    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in ("build_poll", "__post_init__")
    }
    assert set(functions) == {"build_poll", "__post_init__"}
    for function in functions.values():
        guards = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.If) and any(isinstance(s, ast.Raise) for s in node.body)
        ]
        assert guards, f"{function.name} has no explicit if ... raise"

    build = functions["build_poll"]
    operators = [type(node.op) for node in ast.walk(build) if isinstance(node, ast.BinOp)]
    assert ast.BitOr in operators
    assert ast.Add not in operators
    type_checks = [
        node
        for node in ast.walk(build)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Call)
        and isinstance(node.left.func, ast.Name)
        and node.left.func.id == "type"
        and all(isinstance(op, ast.Is | ast.IsNot) for op in node.ops)
    ]
    assert len(type_checks) >= 2

    script = (
        "from dyness_b3_bms_mqtt.protocol import POLL_DATA, PollFrame, build_poll\n"
        "for call in (lambda: build_poll(0x56, 4), lambda: build_poll(5, 4),\n"
        "             lambda: PollFrame(0x18F26677, POLL_DATA)):\n"
        "    try:\n"
        "        call()\n"
        "    except ValueError:\n"
        "        continue\n"
        "    raise SystemExit('guard removed under -O')\n"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


ANYTHING = st.one_of(
    st.integers(-3, 8),
    st.integers(),
    st.booleans(),
    st.floats(allow_nan=True),
    st.text(max_size=3),
    st.none(),
    st.just(0x56),
    st.binary(max_size=2),
    st.sampled_from(list(Module)),
)


@pytest.mark.req("TEST-28", "PROTO-26", "PROTO-27", "PROTO-28")
@DETERMINISTIC
@given(module=ANYTHING, module_count=ANYTHING)
def test_build_poll_property(module: object, module_count: object) -> None:
    valid = (
        type(module) is int
        and type(module_count) is int
        and 1 <= module_count <= 4
        and 1 <= module <= module_count
    )
    if valid:
        assert isinstance(module, int)
        assert_is_poll_of(poll(module, module_count), module)
    else:
        with pytest.raises(ValueError):
            poll(module, module_count)
