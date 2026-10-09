# 02 — CAN transport (`CAN`)

The transport is the only code that talks to python-can. It opens the bus for the
configured interface, delivers received frames and error frames to the main loop, sends the
poll frame and nothing else, shuts the adapter down cleanly on every exit path, and reports
the kernel's view of the CAN link. It lives in `canbus.py`. It does not decode payloads
(`01-protocol.md`) and does not decide how to react to faults (`03-health-and-recovery.md`).

Variables referenced here (`CAN_INTERFACE`, `CAN_CHANNEL`, `CAN_BITRATE`, `CAN_RESTART_MS`,
`CAN_LISTEN_ONLY`, `MODULE_COUNT`) are defined, with their defaults and validation, in
`08-configuration.md`.

## 1. Interface

* **CAN-01** The transport MUST implement the following `typing.Protocol`, and the rest of
  the application MUST depend only on this protocol, so tests can inject a fake:

  ```python
  class CanTransport(Protocol):
      listen_only: bool                      # from CAN_LISTEN_ONLY, fixed for the lifetime
      def open(self) -> None: ...            # raises TransportFault(op="open")
      def recv(self, timeout: float) -> RxFrame | RxErrorFrame | None: ...
      def send_poll(self, module: int) -> None: ...
      def reopen(self) -> None: ...          # close, wait, open
      def close(self) -> None: ...           # idempotent, never raises
      def link_state(self) -> LinkState: ...
      def __enter__(self) -> Self: ...
      def __exit__(self, *exc) -> None: ...  # calls close()
  ```

  `RxFrame(arbitration_id: int, extended: bool, data: bytes, at: float)` and
  `RxErrorFrame(error_class: int, data: bytes, at: float)` are immutable. `at` is the
  injected monotonic clock at the moment `recv` obtained the message, never python-can's
  `msg.timestamp` (whose epoch differs between interfaces). `LinkState` is defined in §7.
* **CAN-02** The python-can implementation (`PythonCanTransport`) MUST take the settings
  object, the monotonic clock and a sleep function as constructor arguments (the sleep is
  used by `reopen`) and MUST NOT read the environment itself.
* **CAN-03** The repository MUST provide a test double implementing `CanTransport` that
  records every `send_poll` call and lets a test script received frames, error frames,
  timeouts and `TransportFault`s. The real implementation over python-can's `virtual`
  interface MUST also pass the transport's contract tests, so the simulated stack in
  `11-testing.md` exercises real python-can code.

## 2. Opening the bus

| `CAN_INTERFACE` | `can.Bus(...)` keyword arguments | Bitrate / link mode set by |
|---|---|---|
| `socketcan` | `interface="socketcan"`, `channel=CAN_CHANNEL`, `can_filters=<§3>`, `receive_own_messages=False` | The `can-up` helper (`09-service.md`) from `CAN_BITRATE`, `CAN_RESTART_MS`, `CAN_LISTEN_ONLY`; the transport does not pass `bitrate` |
| `gs_usb` | `interface="gs_usb"`, `channel=int(CAN_CHANNEL)`, `index=int(CAN_CHANNEL)`, `bitrate=CAN_BITRATE` | The transport, including listen-only (CAN-16) |
| `slcan` | `interface="slcan"`, `channel=CAN_CHANNEL`, `bitrate=CAN_BITRATE` | The transport (bitrate only; no hardware listen-only, CAN-18) |
| `virtual` | `interface="virtual"`, `channel=CAN_CHANNEL`, `receive_own_messages=False` | Not applicable (tests and simulation) |

* **CAN-04** `open()` MUST construct the bus with exactly the keyword arguments in the table
  for the configured `CAN_INTERFACE`. A unit test MUST assert the arguments for each
  interface with `can.Bus` replaced by a recorder.

  Besides the arguments in the table, `open()` passes `ignore_config=True` for every
  interface. Without it `can.Bus` merges any python-can configuration file (`~/can.conf`,
  `/etc/can.conf`, `~/.can`, `~/.canrc`) and the `CAN_*` and `CAN_CONFIG` environment
  variables into its keyword arguments: a malformed file or `CAN_CONFIG` would make every
  open raise, and a key the service does not pass (such as `bitrate` from `CAN_BITRATE` with
  socketcan) could be added. With it, the bus gets exactly the table's arguments.

* **CAN-05** For `gs_usb` on macOS only (`sys.platform == "darwin"`), `open()` MUST replace
  `usb.core.Device.is_kernel_driver_active` with a function returning `False` before
  constructing the bus. On macOS libusb reports a kernel driver as active on the adapter's
  vendor-class interface although none is bound, and the `gs_usb` package's attempt to
  detach it fails with "Access denied" unless run as root. The stub MUST NOT be applied on
  any other platform; on Linux a real kernel `gs_usb` driver may be bound and the check
  matters. (Reference: `open_gs_usb` in `../dyness-debug/dyness_cells.py`.)
* **CAN-06** Any exception raised while opening MUST be mapped per §8 and re-raised as
  `TransportFault(op="open", …)`. If a bus object was partly constructed, its `shutdown()`
  MUST be called before raising.
* **CAN-07** After a successful open the transport MUST log at `INFO` the interface, channel,
  bitrate (for socketcan, as reported by `link_state()`), and whether it is listen-only.

## 3. Receive

The acceptance set is:

| Frames | python-can filter (socketcan) |
|---|---|
| Module frames and poll echoes | `{"can_id": 0x18F21000, "can_mask": 0x1FFFF000, "extended": True}` (the 29-bit form of mask `0xFFFFF000`) |
| Heartbeat | `{"can_id": 0x18F20103, "can_mask": 0x1FFFFFFF, "extended": True}` |
| Stack frames `0x351`, `0x355`, `0x356`, `0x359`, `0x35C`, `0x35F`, `0x70D` | one entry each, `{"can_id": <id>, "can_mask": 0x7FF, "extended": False}` |
| Error frames | Not a data filter; enabled by CAN-10 |

* **CAN-08** With `CAN_INTERFACE=socketcan` the acceptance set MUST be installed as kernel
  filters via `can_filters`, so unwanted frames never reach user space. For every other
  interface `can_filters` MUST NOT be passed (python-can's software filtering would also
  drop error frames).
* **CAN-09** On every interface, `recv` MUST additionally apply the acceptance set itself and
  discard non-matching data frames, so behaviour is identical with and without kernel
  filtering. Error frames MUST always pass. Discarding a frame MUST NOT end the call early:
  `recv` keeps waiting for the remainder of `timeout`, and the total time spent in one call
  MUST NOT exceed `timeout` by more than 50 ms.
* **CAN-10** Error frames MUST be delivered. With socketcan the transport MUST ensure the raw
  socket's `CAN_RAW_ERR_FILTER` option is set to `CAN_ERR_MASK` (all error classes),
  setting it explicitly on the underlying socket if the installed python-can does not.
* **CAN-11** A message with `is_error_frame` set MUST be returned as `RxErrorFrame`, with
  `error_class` = `arbitration_id & 0x1FFFFFFF` (Linux `can/error.h` classes, e.g.
  `0x004` controller, `0x008` protocol, `0x020` no ACK, `0x040` bus-off, `0x080` bus error,
  `0x100` restarted). It MUST NOT be returned as `RxFrame` and MUST NOT reach the decoder.
  The application reports it to the health machine as an error-frame event (see
  `03-health-and-recovery.md`); the transport itself takes no action on it.
* **CAN-12** `recv(timeout)` MUST return `None` when no accepted message arrived within
  `timeout`. A timeout is not an error and MUST NOT produce a fault or a log line above
  `DEBUG`. The main loop MUST call `recv` with a fixed timeout of 0.2 s (a constant, not
  configuration); this is the bounded timeout HEALTH-33 relies on.
* **CAN-13** `receive_own_messages` MUST be `False` wherever the interface accepts it. Echoes
  that arrive anyway (the candleLight firmware echoes transmitted frames through userspace
  `gs_usb`) are passed up as ordinary `RxFrame`s and classified `ECHO` by the decoder
  (`01-protocol.md`, PROTO-32); the transport does not try to recognise them.

## 4. Listen-only

* **CAN-14** When `listen_only` is true, `send_poll` MUST raise `ListenOnlyError` without
  touching the bus, on every interface and regardless of the link or adapter mode. This is
  a programming-error guard: the health machine never asks for a poll in listen-only mode
  (PROTO-31). `ListenOnlyError` MUST NOT be a `TransportFault` and MUST NOT be caught by
  the main loop's fault handling.
* **CAN-15** With `CAN_INTERFACE=socketcan` the link's listen-only mode is set by `can-up`
  (`09-service.md`, CFG-13). After opening, the transport MUST compare `link_state()`'s
  `listen_only` with `CAN_LISTEN_ONLY` and log a `WARNING` if they differ or cannot be
  determined. A mismatch MUST NOT stop the service; CAN-14 still prevents transmission.
* **CAN-16** With `CAN_INTERFACE=gs_usb` and listen-only, `open()` MUST, for the duration of
  the `can.Bus(...)` call only, wrap `gs_usb.gs_usb.GsUsb.start` so that the flags it
  receives are OR'ed with `GS_CAN_MODE_LISTEN_ONLY | GS_CAN_MODE_HW_TIMESTAMP` (the approach
  proven in `../dyness-debug/overnight_logger.py`), restoring the original method afterwards
  even if the call raises. After opening it MUST check
  `bus.gs_usb.device_flags & GS_CAN_MODE_LISTEN_ONLY`; if the flag is not set it MUST shut
  the bus down and raise `TransportFault(op="open", kind=LISTEN_ONLY_REFUSED)`.
* **CAN-17** **MANUAL** On the real SH-C31A, opening with `gs_usb` in listen-only mode MUST
  produce an adapter that receives the stack's traffic and does not ACK (verified by the
  hardware acceptance checklist).

  On the macOS development path only, python-can's `gs_usb` `shutdown()` (with an `index`)
  re-runs the adapter's `start()` and `stop()` to reset it, and that happens in normal mode,
  outside the window in which CAN-16 forces listen-only. On `gs_usb`, a USB write timeout
  from `send` maps to `OS_ERROR` (a bus exception) and an absent adapter at `open` to
  `OPEN_FAILED`, per the §8 table.

* **CAN-18** With `CAN_INTERFACE=slcan` hardware listen-only is not used. When
  `CAN_LISTEN_ONLY=true` the transport MUST log a `WARNING` at open that the adapter is in
  normal mode and only software refusal (CAN-14) applies.

## 5. Send

* **CAN-19** `send_poll(module)` MUST be the transport's only sending method and the only
  place in the package that constructs a `can.Message` or calls python-can's `send`
  (PROTO-30). It MUST obtain the frame from `protocol.build_poll(module, MODULE_COUNT)`,
  letting its `ValueError` propagate.
* **CAN-20** Immediately before `bus.send`, `send_poll` MUST re-check that the `PollFrame`
  matches the poll pattern (PROTO-27) and raise `ValueError` otherwise, then build
  `can.Message(arbitration_id=frame.arbitration_id, is_extended_id=True, data=frame.data)`
  from it and send that message with a finite send timeout of at most 0.5 s, so a full
  transmit queue cannot stall the main loop.
* **CAN-21** Exceptions from `bus.send` MUST be mapped per §8 and raised as
  `TransportFault(op="send", …)`. In particular an `OSError` (or a python-can exception
  caused by one) MUST become a bus-exception event for the health machine, never an
  unhandled crash.
* **CAN-22** `send_poll` MUST raise `TransportFault(op="send", kind=NOT_OPEN)` if the bus is
  not open. Logging every poll at `INFO` is the application's job (CFG-11).

## 6. Shutdown and reopen

* **CAN-23** `close()` MUST call `bus.shutdown()` exactly once for each successfully opened
  bus, MUST be idempotent, and MUST NOT raise: an exception from `shutdown()` is logged at
  `WARNING` and swallowed. After `close()`, `recv` and `send_poll` raise
  `TransportFault(kind=NOT_OPEN)`.
* **CAN-24** The bus MUST be shut down on every exit path: normal return, any exception,
  `SIGTERM`, `SIGINT` and `SIGHUP`. The application MUST hold the transport in a `with`
  block (or `try/finally`) around the main loop, and the signal handlers (installed by the
  application, see `09-service.md`) MUST make the loop return rather than call
  `sys.exit` or `os._exit` from the handler. Rationale: on the SH-C31A an unclean exit has
  been linked to the adapter receiving nothing until it was replugged, so a clean
  `shutdown()` is part of recovery, not hygiene.
* **CAN-25** A test MUST start the service as a subprocess on the `virtual` interface, send
  each of `SIGTERM`, `SIGINT` and `SIGHUP`, and assert that `bus.shutdown()` ran (observable
  through a test hook or log line) and the process exited with the code `03-health-and-recovery.md`
  assigns to a clean stop.
* **CAN-26** `reopen()` MUST call `close()`, then sleep 2 s using the injected sleep, then
  `open()`. The 2 s gap is a fixed constant, not configuration. A failure in `open()` MUST
  propagate as `TransportFault(op="open", …)` with the transport left closed. `reopen` is
  invoked by the application for the health machine's `ReopenBus` action (STALE and DEAD
  states, `03-health-and-recovery.md`).

## 7. Bus-state query

`LinkState` fields: `state` (enum below), `operstate` (str or `None`), `listen_only`
(bool or `None`), `bitrate` (int or `None`), `berr_tx`, `berr_rx`, `restarts`,
`bus_off` (each int or `None`), `at` (monotonic time of the query).

| `state` | Condition |
|---|---|
| `ERROR_ACTIVE` | Link up, `linkinfo.info_data.state == "ERROR-ACTIVE"` |
| `ERROR_WARNING` | Link up, `"ERROR-WARNING"` |
| `ERROR_PASSIVE` | Link up, `"ERROR-PASSIVE"` |
| `BUS_OFF` | Link up, `"BUS-OFF"` |
| `STOPPED` | Link up, `"STOPPED"` |
| `DOWN` | Link exists but `"UP"` is not in `flags` |
| `ABSENT` | `ip` reports the device does not exist |
| `UNKNOWN` | Not socketcan, `ip` missing or failing for another reason, timeout, or output not parseable |

These member names are code identifiers. The published `can_state` value is lower-case hyphenated per `06-mqtt.md` (MQTT-18), for example `ERROR_ACTIVE` → `error-active`, and `UNKNOWN` is published as `null`.

* **CAN-27** With `CAN_INTERFACE=socketcan`, `link_state()` MUST run
  `ip -j -d -s link show <CAN_CHANNEL>` as an argument list (no shell) with `LC_ALL=C`, a
  2 s timeout, and `ip` resolved from `PATH` plus `/usr/sbin:/sbin`. It MUST NOT require
  privileges (no `sudo`, no capabilities); only `AF_NETLINK` is needed, which the unit
  allows (`09-service.md`). The command runner MUST be injectable for tests.
* **CAN-28** The JSON MUST be parsed as: first array element; `flags` for `UP`; `operstate`;
  `linkinfo.info_data.state`, `.ctrlmode` (contains `"LISTEN-ONLY"` → `listen_only`),
  `.bittiming.bitrate`, `.berr_counter.tx` / `.rx`; `linkinfo.info_xstats.restarts` and
  `.bus_off`. Each missing or mistyped field MUST become `None` individually; `state` is
  `UNKNOWN` only if the state itself cannot be determined. Unrecognised `info_data.state`
  strings MUST map to `UNKNOWN`.
* **CAN-29** A non-zero exit whose stderr contains `does not exist` MUST yield `ABSENT`. A
  missing `ip` binary, any other non-zero exit, a timeout, or invalid JSON MUST yield
  `UNKNOWN`. `link_state()` MUST NOT raise in any of these cases. Unit tests MUST cover each
  `state` value with fixture outputs.
* **CAN-30** For any `CAN_INTERFACE` other than `socketcan`, `link_state()` MUST return
  `UNKNOWN` without running a command.
* **CAN-31** The application MUST call `link_state()` every 10 s (fixed, not configuration)
  from the main loop and publish the result as service diagnostics (`06-mqtt.md`). A state
  change MUST be logged at `INFO`. `restarts` increasing is how kernel bus-off restarts
  (governed by `CAN_RESTART_MS`) become visible.

## 8. Exception mapping

`TransportFault(op, kind, errno, message)` is the transport's only fault exception. `op` is
`open`, `recv` or `send`. The application hands every `TransportFault` to the health
machine; which input it becomes per `op` and `kind` (open failure, bus exception, or nothing
for `TX_BLOCKED`) is defined in `03-health-and-recovery.md` §5.1.

| Source (on `open`, `recv` or `send`) | `kind` | Reported as |
|---|---|---|
| `recv` returns no message within `timeout` | — | Not an error; `recv` returns `None` (CAN-12) |
| `OSError` with `ENODEV` or `ENXIO`; `usb.core.USBError` with errno `ENODEV`/`EIO` or "No such device" | `DEVICE_GONE` | Bus exception |
| `OSError` with `ENETDOWN` | `LINK_DOWN` | Bus exception |
| `OSError` with `ENOBUFS` or `EAGAIN` on `send`; `can.CanOperationError` for a send timeout | `TX_BLOCKED` | Not a bus fault (logged; HEALTH §5.1) |
| Any other `OSError` | `OS_ERROR` | Bus exception |
| `can.CanInitializationError` not attributable to the rows above | `OPEN_FAILED` | Bus exception |
| Any other `can.CanError` | `CAN_ERROR` | Bus exception |
| gs_usb adapter refuses listen-only (CAN-16) | `LISTEN_ONLY_REFUSED` | Bus exception |
| `recv`/`send_poll` called while closed | `NOT_OPEN` | Bus exception |
| `ListenOnlyError` (CAN-14), `ValueError` from `build_poll` | — | Programming error; propagates, process exits 1 |
| Any other exception | — | Not caught by the transport; propagates |

* **CAN-32** The transport MUST map exceptions per the table. python-can 4 wraps OS errors
  in `CanOperationError` / `CanInitializationError`, so the errno MUST be taken from the
  exception itself, its `error_code` attribute, or the first `OSError` in its
  `__cause__` / `__context__` chain, in that order.
* **CAN-33** After any `TransportFault` from `recv` or `send`, the transport MUST remain
  usable for `close()` and `reopen()`; it MUST NOT attempt recovery on its own.
* **CAN-34** A test MUST drive a fake python-can bus that raises `OSError(ENODEV)`,
  `OSError(ENETDOWN)` and `OSError(ENOBUFS)` from `recv` and `send`, each bare and wrapped in
  `CanOperationError`, and assert the resulting `TransportFault.kind`.
