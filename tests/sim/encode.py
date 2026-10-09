"""Frame encoders for the simulated stack, independent of the package (TEST-25).

Each function turns engineering values into the wire bytes described in
``docs/specs/01-protocol.md``: module frames are 29-bit and big-endian, inverter (stack)
frames are 11-bit and little-endian. Nothing here imports ``dyness_b3_bms_mqtt``, so a decoder
bug cannot be cancelled out by a matching encoder bug.

Conventions: cells are 1-based in the arguments and 0-based in ``0x22`` on the wire; module
currents are given in the published sign (positive when charging) and negated on the wire;
temperatures are ``raw / 10 - 40`` degrees C.
"""

import struct
from collections.abc import Sequence
from dataclasses import dataclass

MODULE_BASE = 0x18F21000
HEARTBEAT_ID = 0x18F20103
POLL_MASK = 0xFFFFF0FF
POLL_PATTERN = 0x18F21077
POLL_DATA = bytes([1, 0, 0, 0, 0, 0, 0, 0])
CONTROLLER_ERROR = 0x004
CELLS = 15


@dataclass(frozen=True, slots=True)
class Frame:
    """One frame to put on the bus; ``error`` marks a CAN error frame."""

    arbitration_id: int
    extended: bool
    data: bytes
    error: bool = False


def module_id(module: int, code: int) -> int:
    """Return the 29-bit identifier ``0x18F21mcc``."""
    return MODULE_BASE | module << 8 | code


def _module(module: int, code: int, fmt: str, *values: int) -> Frame:
    return Frame(module_id(module, code), True, struct.pack(fmt, *values))


def _stack(can_id: int, fmt: str, *values: int) -> Frame:
    return Frame(can_id, False, struct.pack(fmt, *values))


def _temp(celsius: float) -> int:
    return round(celsius * 10) + 400


def _tenths(value: float) -> int:
    return round(value * 10)


def cell_voltages(module: int, cells_mv: Sequence[int]) -> list[Frame]:
    """Return ``0x11``-``0x14``; slot 16 (``0x14`` word 4) is not fitted and reads 0."""
    if len(cells_mv) != CELLS:
        raise ValueError(f"expected {CELLS} cells, got {len(cells_mv)}")
    slots = [*cells_mv, 0]
    return [_module(module, 0x11 + n, ">HHHH", *slots[4 * n : 4 * n + 4]) for n in range(4)]


def cell_temperatures(module: int, temps_c: Sequence[float]) -> Frame:
    """Return ``0x15``: T1..T3; T4 is not fitted and reads 0."""
    t1, t2, t3 = temps_c
    return _module(module, 0x15, ">HHHH", _temp(t1), _temp(t2), _temp(t3), 0)


def mos_board_cycles(module: int, mos_c: float, board_c: float, cycles: int) -> Frame:
    """Return ``0x16``."""
    return _module(module, 0x16, ">HHH2x", _temp(mos_c), _temp(board_c), cycles)


def energy(module: int, kwh: float) -> Frame:
    """Return ``0x17``: lifetime throughput in units of 0.01 kWh."""
    return _module(module, 0x17, ">I4x", round(kwh * 100))


def pack_status(
    module: int, pack_v: float, current_a: float, avg_cell_mv: int, max_discharge_a: float
) -> Frame:
    """Return ``0x21``; ``current_a`` is positive when charging and is negated on the wire."""
    current_raw = round(-current_a * 10) + 4000
    return _module(
        module,
        0x21,
        ">HHHH",
        round(pack_v * 100),
        current_raw,
        avg_cell_mv,
        _tenths(max_discharge_a),
    )


def cell_extremes(module: int, cells_mv: Sequence[int], max_charge_a: float) -> Frame:
    """Return ``0x22``: highest and lowest cell with 0-based cell numbers on the wire."""
    high = max(range(len(cells_mv)), key=lambda n: cells_mv[n])
    low = min(range(len(cells_mv)), key=lambda n: cells_mv[n])
    return _module(
        module,
        0x22,
        ">HBHBH",
        cells_mv[high],
        high,
        cells_mv[low],
        low,
        _tenths(max_charge_a),
    )


def temp_extremes(
    module: int,
    max_c: float,
    max_sensor: int,
    min_c: float,
    min_sensor: int,
    soh: int,
    soc: int,
) -> Frame:
    """Return ``0x23``."""
    return _module(
        module, 0x23, ">HBHBBB", _temp(max_c), max_sensor, _temp(min_c), min_sensor, soh, soc
    )


def status_flags(module: int, raw: bytes) -> Frame:
    """Return ``0x24`` carrying ``raw`` (8 bytes) unchanged."""
    if len(raw) != 8:
        raise ValueError("0x24 carries exactly 8 bytes")
    return Frame(module_id(module, 0x24), True, bytes(raw))


def heartbeat() -> Frame:
    """Return the 10 Hz heartbeat ``0x18F20103``, all zeros."""
    return Frame(HEARTBEAT_ID, True, bytes(8))


def charge_limits(chg_v: float, chg_a: float, dis_a: float, dis_v: float) -> Frame:
    """Return ``0x351``."""
    return _stack(0x351, "<HhhH", _tenths(chg_v), _tenths(chg_a), _tenths(dis_a), _tenths(dis_v))


def stack_soc_soh(soc: int, soh: int) -> Frame:
    """Return ``0x355``."""
    return _stack(0x355, "<HH4x", soc, soh)


def stack_measurements(voltage_v: float, current_a: float, temp_c: float) -> Frame:
    """Return ``0x356``; the current is positive when charging, unmodified."""
    return _stack(0x356, "<Hhh2x", round(voltage_v * 100), _tenths(current_a), _tenths(temp_c))


def stack_module_count(count: int) -> Frame:
    """Return ``0x359`` with the module count in byte 4, as captured."""
    return Frame(0x359, False, bytes([0, 0, 0, 0, count, 0, 0, 1]))


def charge_request(request: int) -> Frame:
    """Return ``0x35C``."""
    return _stack(0x35C, "<B7x", request)


def stack_capacity(capacity_ah: int) -> Frame:
    """Return ``0x35F`` with the capacity in bytes 4-5 and the captured bytes 2-3."""
    return _stack(0x35F, "<2xHH2x", 0x0388, capacity_ah)


def stack_words(words: Sequence[int]) -> Frame:
    """Return ``0x70D``: four little-endian words."""
    return _stack(0x70D, "<HHHH", *words)


def error_frame(error_class: int = CONTROLLER_ERROR) -> Frame:
    """Return a CAN error frame of ``error_class`` (controller problem by default)."""
    return Frame(error_class, False, bytes([0, 0x04, 0, 0, 0, 0, 0, 0]), error=True)


def polled_module(arbitration_id: int, extended: bool, data: bytes) -> int | None:
    """Return the module a received poll addresses, or ``None`` if it is not a poll."""
    if extended and arbitration_id & POLL_MASK == POLL_PATTERN and data == POLL_DATA:
        return (arbitration_id >> 8) & 0xF
    return None
