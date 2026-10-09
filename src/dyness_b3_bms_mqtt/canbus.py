"""The CAN transport: the only code that talks to python-can (02-can-transport.md).

:class:`PythonCanTransport` opens the bus for ``CAN_INTERFACE`` with exactly the keyword
arguments of the spec table (:func:`bus_kwargs`), delivers accepted data frames as
:class:`RxFrame` and error frames as :class:`RxErrorFrame`, transmits the module poll and
nothing else (:meth:`PythonCanTransport.send_poll`), shuts the adapter down on every close and
reports the kernel's view of the link through ``linkstate``. Every fault it raises is a
:class:`TransportFault` built by :func:`map_exception`; it never decides how to react to one,
which is the health machine's job (``03-health-and-recovery.md``).

The rest of the application depends only on the :class:`CanTransport` protocol, so tests can
substitute a fake (``tests/fakes/transport.py``) or run this class over python-can's
``virtual`` interface against the simulated stack.
"""

import errno as errno_codes
import importlib
import logging
import socket
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Any, Protocol, Self

import can

from dyness_b3_bms_mqtt import protocol
from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.linkstate import LinkState, query_link_state, unknown_link_state

RECV_TIMEOUT_S = 0.2
"""The main loop's fixed ``recv`` timeout (CAN-12)."""

REOPEN_GAP_S = 2.0
"""Pause between ``close`` and ``open`` in :meth:`PythonCanTransport.reopen` (CAN-26)."""

SEND_TIMEOUT_S = 0.5
"""Upper bound on one ``bus.send`` so a full transmit queue cannot stall the loop (CAN-20)."""

CAN_ERR_MASK = 0x1FFFFFFF
"""Every Linux error class; also the mask applied to an error frame's identifier (CAN-11)."""

SOL_CAN_RAW: int = getattr(socket, "SOL_CAN_RAW", 101)
CAN_RAW_ERR_FILTER: int = getattr(socket, "CAN_RAW_ERR_FILTER", 2)

GS_CAN_MODE_LISTEN_ONLY = 1 << 0
GS_CAN_MODE_HW_TIMESTAMP = 1 << 4
"""``gs_usb.constants`` mode flags, repeated so the optional package is imported only for use."""

_EXTENDED_MASK = 0x1FFFFFFF
_STANDARD_MASK = 0x7FF

ACCEPT_FILTERS: tuple[Mapping[str, int | bool], ...] = (
    {
        "can_id": protocol.MODULE_ID_BASE,
        "can_mask": protocol.MODULE_ID_MASK & _EXTENDED_MASK,
        "extended": True,
    },
    {"can_id": protocol.HEARTBEAT_ID, "can_mask": _EXTENDED_MASK, "extended": True},
    *(
        {"can_id": can_id, "can_mask": _STANDARD_MASK, "extended": False}
        for can_id in sorted(protocol.STACK_IDS)
    ),
)
"""The acceptance set of 02-can-transport.md §3, in python-can ``can_filters`` form."""

_log = logging.getLogger(__name__)


class FaultKind(StrEnum):
    """What went wrong, per the exception-mapping table (02-can-transport.md §8)."""

    DEVICE_GONE = auto()
    LINK_DOWN = auto()
    TX_BLOCKED = auto()
    OS_ERROR = auto()
    OPEN_FAILED = auto()
    CAN_ERROR = auto()
    LISTEN_ONLY_REFUSED = auto()
    NOT_OPEN = auto()


class TransportFault(Exception):
    """The transport's only fault exception (CAN-32).

    ``op`` is ``open``, ``recv`` or ``send``; ``errno`` is the OS error number when one could
    be found, else ``None``. The application hands every fault to the health machine.
    """

    def __init__(self, op: str, kind: FaultKind, errno: int | None, message: str) -> None:
        """Store the fault fields."""
        super().__init__(op, kind, errno, message)
        self.op = op
        self.kind = kind
        self.errno = errno
        self.message = message

    def __str__(self) -> str:
        """Return ``<op>: <kind>: <message>``."""
        return f"{self.op}: {self.kind}: {self.message}"


class ListenOnlyError(RuntimeError):
    """``send_poll`` was called in listen-only mode: a programming error, not a fault (CAN-14)."""


@dataclass(frozen=True, slots=True)
class RxFrame:
    """An accepted data frame; ``at`` is the injected monotonic clock when ``recv`` got it."""

    arbitration_id: int
    extended: bool
    data: bytes
    at: float


@dataclass(frozen=True, slots=True)
class RxErrorFrame:
    """A CAN error frame; ``error_class`` holds the Linux ``can/error.h`` class bits."""

    error_class: int
    data: bytes
    at: float


class CanTransport(Protocol):
    """The transport interface the rest of the application depends on (CAN-01)."""

    listen_only: bool

    def open(self) -> None:
        """Open the bus; raise ``TransportFault(op="open")`` on failure."""
        ...

    def recv(self, timeout: float) -> RxFrame | RxErrorFrame | None:
        """Return the next accepted frame or error frame, or ``None`` after ``timeout``."""
        ...

    def send_poll(self, module: int) -> None:
        """Transmit the poll for ``module``."""
        ...

    def reopen(self) -> None:
        """Close, wait :data:`REOPEN_GAP_S`, open."""
        ...

    def close(self) -> None:
        """Shut the bus down; idempotent and never raises."""
        ...

    def link_state(self) -> LinkState:
        """Return the kernel's view of the link."""
        ...

    def __enter__(self) -> Self:
        """Return the transport."""
        ...

    def __exit__(self, *exc: object) -> None:
        """Close the transport."""
        ...


class BusLike(Protocol):
    """The part of ``can.BusABC`` the transport uses."""

    def recv(self, timeout: float | None = None) -> can.Message | None:
        """Return the next message or ``None`` after ``timeout``."""
        ...

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        """Transmit ``msg``."""
        ...

    def shutdown(self) -> None:
        """Release the adapter."""
        ...


type BusFactory = Callable[..., BusLike]
type LinkQuery = Callable[[str, float], LinkState]


def accepts(arbitration_id: int, extended: bool) -> bool:
    """Return whether a data frame is in the acceptance set (CAN-09)."""
    for entry in ACCEPT_FILTERS:
        mask = int(entry["can_mask"])
        if entry["extended"] is extended and arbitration_id & mask == int(entry["can_id"]) & mask:
            return True
    return False


def bus_kwargs(settings: Settings) -> dict[str, Any]:
    """Return the ``can.Bus`` keyword arguments for ``CAN_INTERFACE`` (CAN-04, CAN-08)."""
    interface = settings.can_interface
    channel = settings.can_channel
    if interface == "socketcan":
        return {
            "interface": interface,
            "channel": channel,
            "can_filters": [dict(entry) for entry in ACCEPT_FILTERS],
            "receive_own_messages": False,
        }
    if interface == "gs_usb":
        index = int(channel)
        return {
            "interface": interface,
            "channel": index,
            "index": index,
            "bitrate": settings.can_bitrate,
        }
    if interface == "slcan":
        return {"interface": interface, "channel": channel, "bitrate": settings.can_bitrate}
    if interface == "virtual":
        return {"interface": interface, "channel": channel, "receive_own_messages": False}
    raise ValueError(f"unsupported CAN_INTERFACE {interface!r}")


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield ``exc`` and then its ``__cause__`` / ``__context__`` chain, without cycles."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _errno_of(exc: BaseException) -> int | None:
    """Return the errno of ``exc``, its ``error_code``, or the first chained ``OSError``'s."""
    if isinstance(exc, OSError) and type(exc.errno) is int:
        return exc.errno
    code = getattr(exc, "error_code", None)
    if type(code) is int:
        return code
    for linked in _chain(exc):
        if isinstance(linked, OSError) and type(linked.errno) is int:
            return linked.errno
    return None


def _is_usb_gone(exc: BaseException, code: int | None) -> bool:
    """Return whether a pyusb ``USBError`` in the chain says the device has gone."""
    for linked in _chain(exc):
        if type(linked).__name__ == "USBError":
            usb_code = linked.errno if isinstance(linked, OSError) else None
            if code in (errno_codes.ENODEV, errno_codes.EIO) or usb_code in (
                errno_codes.ENODEV,
                errno_codes.EIO,
            ):
                return True
            if "No such device" in str(linked):
                return True
    return False


def _is_send_timeout(exc: BaseException) -> bool:
    if isinstance(exc, can.CanTimeoutError):
        return True
    if isinstance(exc, can.CanOperationError):
        text = str(exc).lower()
        return "timed out" in text or "buffer full" in text
    return False


def _kind(op: str, exc: BaseException, code: int | None) -> FaultKind | None:
    if code in (errno_codes.ENODEV, errno_codes.ENXIO) or _is_usb_gone(exc, code):
        return FaultKind.DEVICE_GONE
    if code == errno_codes.ENETDOWN:
        return FaultKind.LINK_DOWN
    if op == "send" and (
        code in (errno_codes.ENOBUFS, errno_codes.EAGAIN)
        or (code is None and _is_send_timeout(exc))
    ):
        return FaultKind.TX_BLOCKED
    if code is not None:
        return FaultKind.OS_ERROR
    if isinstance(exc, can.CanInitializationError):
        return FaultKind.OPEN_FAILED
    if isinstance(exc, can.CanError):
        return FaultKind.CAN_ERROR
    if isinstance(exc, OSError):
        return FaultKind.OS_ERROR
    return None


def map_exception(op: str, exc: BaseException) -> TransportFault | None:
    """Map an exception from ``open``, ``recv`` or ``send`` to a fault (CAN-32).

    The errno is taken from the exception itself, its ``error_code`` attribute, or the first
    ``OSError`` in its ``__cause__`` / ``__context__`` chain, in that order. Returns ``None``
    for exceptions the transport does not catch (``ListenOnlyError``, ``ValueError`` and any
    other non-OS, non-python-can exception).
    """
    if isinstance(exc, ListenOnlyError | TransportFault):
        return None
    code = _errno_of(exc)
    kind = _kind(op, exc, code)
    if kind is None:
        return None
    return TransportFault(op, kind, code, str(exc) or type(exc).__name__)


def _shutdown_quietly(bus: BusLike) -> None:
    """Shut down a bus whose open failed; a second failure is logged, not raised."""
    try:
        bus.shutdown()
    except Exception as exc:
        _log.warning("can bus shutdown after a failed open raised: %s", exc)


def _default_bus_factory(**kwargs: Any) -> BusLike:
    """Construct a python-can bus, looking ``can.Bus`` up at call time."""
    bus: BusLike = can.Bus(**kwargs)
    return bus


class PythonCanTransport:
    """:class:`CanTransport` over python-can.

    ``clock`` is the monotonic clock stamped on received frames and used for ``recv``
    deadlines; ``sleep`` is used only by :meth:`reopen`. ``bus_factory`` replaces ``can.Bus``,
    ``link_query`` replaces :func:`linkstate.query_link_state` and ``platform`` replaces
    ``sys.platform`` (for the macOS ``gs_usb`` workaround, CAN-05). The transport never reads
    the environment (CAN-02). Entering a ``with`` block does not open the bus; leaving it
    closes the bus.
    """

    def __init__(
        self,
        settings: Settings,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        *,
        bus_factory: BusFactory | None = None,
        link_query: LinkQuery | None = None,
        platform: str = sys.platform,
    ) -> None:
        """Store the collaborators; nothing is opened yet."""
        self.listen_only = settings.can_listen_only
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._bus_factory: BusFactory = bus_factory or _default_bus_factory
        self._link_query: LinkQuery = link_query or query_link_state
        self._platform = platform
        self._bus: BusLike | None = None

    def __enter__(self) -> Self:
        """Return the transport without opening it."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the transport on every exit path (CAN-24)."""
        self.close()

    def open(self) -> None:
        """Open the bus (CAN-04..07, CAN-10, CAN-15, CAN-16, CAN-18); no-op when already open.

        Raises:
            TransportFault: ``op="open"``; any partly opened bus has been shut down.

        """
        if self._bus is not None:
            return
        kwargs = bus_kwargs(self._settings)
        try:
            bus = self._construct_bus(kwargs)
        except Exception as exc:
            fault = map_exception("open", exc)
            if fault is None:
                raise
            raise fault from exc
        try:
            self._after_open(bus)
        except BaseException as exc:
            _shutdown_quietly(bus)
            if isinstance(exc, TransportFault):
                raise
            fault = map_exception("open", exc)
            if fault is None:
                raise
            raise fault from exc
        self._bus = bus
        self._log_open()

    def _construct_bus(self, kwargs: Mapping[str, Any]) -> BusLike:
        """Construct the bus, applying the ``gs_usb`` workarounds around the call.

        This is the only place a bus is created; the test suite's ``tx_guard`` fixture wraps
        its result to police every transmission (TEST-28).
        """
        interface = self._settings.can_interface
        if interface == "gs_usb" and self._platform == "darwin":
            core = importlib.import_module("usb.core")
            core.Device.is_kernel_driver_active = _no_kernel_driver
        if interface == "gs_usb" and self.listen_only:
            with _gs_usb_listen_only():
                return self._bus_factory(**kwargs)
        return self._bus_factory(**kwargs)

    def _after_open(self, bus: BusLike) -> None:
        """Enable error frames on SocketCAN and verify ``gs_usb`` listen-only (CAN-10, CAN-16)."""
        interface = self._settings.can_interface
        if interface == "socketcan":
            raw = getattr(bus, "socket", None)
            if raw is None:
                _log.warning("cannot enable CAN error frames: bus has no socket")
            else:
                raw.setsockopt(SOL_CAN_RAW, CAN_RAW_ERR_FILTER, CAN_ERR_MASK)
        if interface == "gs_usb" and self.listen_only:
            flags = getattr(getattr(bus, "gs_usb", None), "device_flags", None) or 0
            if not flags & GS_CAN_MODE_LISTEN_ONLY:
                raise TransportFault(
                    "open",
                    FaultKind.LISTEN_ONLY_REFUSED,
                    None,
                    "gs_usb adapter did not enter listen-only mode",
                )

    def _log_open(self) -> None:
        settings = self._settings
        bitrate: int | None = settings.can_bitrate
        if settings.can_interface == "socketcan":
            link = self.link_state()
            bitrate = link.bitrate
            self._check_link_listen_only(link.listen_only)
        _log.info(
            "can bus open: interface=%s channel=%s bitrate=%s listen_only=%s",
            settings.can_interface,
            settings.can_channel,
            "unknown" if bitrate is None else bitrate,
            str(self.listen_only).lower(),
        )
        if settings.can_interface == "slcan" and self.listen_only:
            _log.warning(
                "slcan has no hardware listen-only mode: the adapter is in normal mode and "
                "only software refusal to transmit applies"
            )

    def _check_link_listen_only(self, link_listen_only: bool | None) -> None:
        """Warn when the link's listen-only mode differs from ``CAN_LISTEN_ONLY`` (CAN-15)."""
        channel = self._settings.can_channel
        wanted = str(self.listen_only).lower()
        refused = "; transmission is still refused" if self.listen_only else ""
        if link_listen_only is None:
            _log.warning(
                "CAN_LISTEN_ONLY=%s but the listen-only mode of link %s cannot be determined%s",
                wanted,
                channel,
                refused,
            )
        elif link_listen_only is not self.listen_only:
            state = "listen-only" if link_listen_only else "not listen-only"
            _log.warning("CAN_LISTEN_ONLY=%s but link %s is %s%s", wanted, channel, state, refused)

    def recv(self, timeout: float) -> RxFrame | RxErrorFrame | None:
        """Return the next accepted frame or error frame, or ``None`` (CAN-09, CAN-11, CAN-12).

        Non-matching data frames are discarded without ending the call early; the deadline is
        ``timeout`` on the injected clock. A timeout is not logged.

        Raises:
            TransportFault: ``op="recv"``, including ``NOT_OPEN`` when closed.

        """
        bus = self._require_open("recv")
        deadline = self._clock() + timeout
        remaining = timeout
        while True:
            try:
                message = bus.recv(timeout=max(remaining, 0.0))
            except Exception as exc:
                fault = map_exception("recv", exc)
                if fault is None:
                    raise
                raise fault from exc
            now = self._clock()
            if message is None:
                return None
            if message.is_error_frame:
                return RxErrorFrame(message.arbitration_id & CAN_ERR_MASK, bytes(message.data), now)
            if accepts(message.arbitration_id, message.is_extended_id):
                return RxFrame(
                    message.arbitration_id, message.is_extended_id, bytes(message.data), now
                )
            remaining = deadline - now
            if remaining <= 0:
                return None

    def send_poll(self, module: int) -> None:
        """Transmit the poll for ``module``: the package's only transmission (CAN-14, CAN-19..22).

        Raises:
            ListenOnlyError: in listen-only mode, before anything else is touched.
            ValueError: ``build_poll`` refused ``module``, or its frame failed the re-check.
            TransportFault: ``op="send"``, including ``NOT_OPEN`` when closed.

        """
        if self.listen_only:
            raise ListenOnlyError("send_poll called with CAN_LISTEN_ONLY=true")
        bus = self._require_open("send")
        frame = protocol.build_poll(module, self._settings.module_count)
        _recheck_poll(frame, module, self._settings.module_count)
        message = can.Message(
            arbitration_id=frame.arbitration_id, is_extended_id=True, data=frame.data
        )
        try:
            bus.send(message, timeout=SEND_TIMEOUT_S)
        except Exception as exc:
            fault = map_exception("send", exc)
            if fault is None:
                raise
            raise fault from exc

    def reopen(self) -> None:
        """Close, sleep :data:`REOPEN_GAP_S` with the injected sleep, open (CAN-26).

        Raises:
            TransportFault: ``op="open"``; the transport is left closed.

        """
        self.close()
        self._sleep(REOPEN_GAP_S)
        self.open()

    def close(self) -> None:
        """Shut the bus down once; idempotent and never raises (CAN-23)."""
        bus, self._bus = self._bus, None
        if bus is None:
            return
        try:
            bus.shutdown()
        except Exception as exc:
            _log.warning("can bus shutdown failed: %s", exc)
            return
        _log.info("can bus shut down")

    def link_state(self) -> LinkState:
        """Return the link state; ``UNKNOWN`` without a query unless socketcan (CAN-27, -30)."""
        now = self._clock()
        if self._settings.can_interface != "socketcan":
            return unknown_link_state(now)
        return self._link_query(self._settings.can_channel, now)

    def _require_open(self, op: str) -> BusLike:
        if self._bus is None:
            raise TransportFault(op, FaultKind.NOT_OPEN, None, "can bus is not open")
        return self._bus


def _recheck_poll(frame: protocol.PollFrame, module: int, module_count: int) -> None:
    """Re-check the frame from ``build_poll`` against the poll pattern (CAN-20, PROTO-27).

    Raises:
        ValueError: the frame is not the extended poll for ``module`` in 1..``module_count``.

    """
    arbitration_id = frame.arbitration_id
    data = frame.data
    if type(arbitration_id) is not int or type(data) is not bytes:
        raise ValueError(f"refusing to send a malformed poll frame {frame!r}")
    if not protocol.is_poll(arbitration_id, True, data):
        raise ValueError(f"refusing to send {arbitration_id:#x}: not the poll pattern")
    if arbitration_id == protocol.PARAMETER_WRITE_ID or arbitration_id > _EXTENDED_MASK:
        raise ValueError(f"refusing to send {arbitration_id:#x}")
    polled = (arbitration_id >> 8) & 0xF
    if polled != module or not 1 <= polled <= module_count:
        raise ValueError(f"poll {arbitration_id:#x} does not address module {module}")


def _no_kernel_driver(self: object, interface: int) -> bool:
    """Stand in for ``usb.core.Device.is_kernel_driver_active`` on macOS (CAN-05)."""
    return False


@contextmanager
def _gs_usb_listen_only() -> Iterator[None]:
    """Wrap ``GsUsb.start`` for the duration of the block (CAN-16).

    Inside the block every ``start`` call has ``GS_CAN_MODE_LISTEN_ONLY |
    GS_CAN_MODE_HW_TIMESTAMP`` OR'ed into its flags; the original method is restored on exit,
    even when the block raises.
    """
    gs_usb_class = importlib.import_module("gs_usb.gs_usb").GsUsb
    original = gs_usb_class.start

    def start(device: object, flags: int = 0, *args: object, **kwargs: object) -> object:
        return original(
            device, flags | GS_CAN_MODE_LISTEN_ONLY | GS_CAN_MODE_HW_TIMESTAMP, *args, **kwargs
        )

    gs_usb_class.start = start
    try:
        yield
    finally:
        gs_usb_class.start = original
