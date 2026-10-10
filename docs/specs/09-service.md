# 09 — Service (`SVC`)

This document covers how the service runs under systemd on the Pi: the unit file, the
`sd_notify` contract and watchdog, signals, the two root helpers (`can-up` and `recover`),
the USB reset sequence and the udev hotplug rule. The health machine that decides when to
ask for recovery is in `03-health-and-recovery.md`. Installation paths are in
`10-installation.md`.

The target is systemd 252 (Debian/Raspberry Pi OS Bookworm). Directives added after 252,
such as `RestartSteps=` and `RestartMaxDelaySec=`, MUST NOT be used.

## 1. Unit file

* **SVC-01** The repository MUST ship `packaging/dyness-b3-bms-mqtt.service`, installed as
  `/etc/systemd/system/dyness-b3-bms-mqtt.service`, with exactly this content:

```ini
[Unit]
Description=Dyness B3 BMS CAN to MQTT bridge
Documentation=https://github.com/gsdevme/dyness-b3-bms-mqtt
After=network.target
StartLimitIntervalSec=0

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=30
Restart=always
RestartSec=5
RestartPreventExitStatus=78
TimeoutStopSec=15

EnvironmentFile=-/etc/default/dyness-b3-bms-mqtt
Environment=PYTHONUNBUFFERED=1
ExecStartPre=+/usr/local/lib/dyness-b3-bms-mqtt/can-up
ExecStart=/opt/dyness-b3-bms-mqtt/.venv/bin/dyness-b3-bms-mqtt
ExecStopPost=+/usr/local/lib/dyness-b3-bms-mqtt/recover

DynamicUser=yes
StateDirectory=dyness-b3-bms-mqtt

NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
RestrictAddressFamilies=AF_CAN AF_INET AF_INET6 AF_UNIX AF_NETLINK

[Install]
WantedBy=multi-user.target
```

Why some of these are set or left out (all directives exist in systemd 252):

| Directive | Reason |
|---|---|
| `After=network.target` | Not `network-online.target`. The broker may be unreachable at boot, and MQTT reconnects on its own (`06-mqtt.md`). Waiting for network-online would delay CAN monitoring for no benefit |
| `StartLimitIntervalSec=0` | In `[Unit]`, where systemd 252 reads it. Turns off the start rate limit so `Restart=always` never gives up |
| `RestartPreventExitStatus=78` | A configuration error does not fix itself by restarting (HEALTH-27) |
| `TimeoutStopSec=15` | Also limits `ExecStopPost=`, so `recover` has a time budget (SVC-17) |
| `+` on `ExecStartPre`/`ExecStopPost` | The helpers run as full root without the sandbox. The service itself has no capabilities |
| `DynamicUser=yes` | Implies `PrivateTmp`, `RemoveIPC` and a read-only `/home`. A raw `AF_CAN` socket needs no capability |
| `AF_NETLINK` | For the unprivileged `ip -j -d -s link show` bus-state query (`02-can-transport.md`) |
| `AF_UNIX` | For `$NOTIFY_SOCKET` and journald |
| No `PrivateDevices=`, `DeviceAllow=` or `ProtectClock=` | These close the device policy, which would block `CAN_INTERFACE=slcan` serial devices. `ProtectClock=` adds an implicit `DeviceAllow=` |
| No `MemoryDenyWriteExecute=` | Left out on purpose. CPython 3.14's optional JIT and libffi closures can need writable+executable memory, and a crash loop caused by hardening is worse than the protection it adds |
| No `SystemCallFilter=` | Left out to avoid unexplained crash loops from interpreter or library syscalls. `SystemCallArchitectures=native` still applies |

* **SVC-02** Unit file and helper changes MUST keep every directive valid for systemd 252.
  `systemd-analyze verify` on the installed unit MUST report no unknown directives. **MANUAL**
  (run on the Pi).

## 2. State directory

* **SVC-03** The service MUST keep its persistent state only in `$STATE_DIRECTORY`, which
  systemd provides as `/var/lib/dyness-b3-bms-mqtt` (a symlink into `/var/lib/private/`
  under `DynamicUser=yes`). It holds `recovery.json` (written by the service,
  HEALTH-24), `last_exit.json` (written by `recover`, SVC-15) and the local history in
  `logs/` (written by the service, `12-local-logging.md`).
* **SVC-04** The service MUST read `last_exit.json` at start-up for diagnostics and the
  `restarts` count (HEALTH-24). A missing or invalid file MUST be treated as "no previous
  exit" and MUST NOT stop start-up.

## 3. sd_notify and watchdog

* **SVC-05** The service MUST implement `sd_notify` without any third-party dependency. It
  sends one `AF_UNIX` `SOCK_DGRAM` datagram per message to `$NOTIFY_SOCKET`. A path that
  starts with `@` is an abstract-namespace address: replace the `@` with a NUL byte. When
  `NOTIFY_SOCKET` is unset or empty, every notify call MUST do nothing, so development runs
  work. A failed send MUST be logged at `DEBUG` and MUST NOT raise.
* **SVC-06** The service MUST send `READY=1` exactly once. It is sent after the first bus
  open has been attempted (whether or not it succeeded) and the MQTT connection has been
  started without blocking. `READY=1` MUST NOT wait for the broker to accept the connection
  or for CAN frames.
* **SVC-07** When `WATCHDOG_USEC` is set (and `WATCHDOG_PID`, if set, equals the process's
  own PID), the main loop MUST send `WATCHDOG=1` at most every `WATCHDOG_USEC / 3`, and only
  from a main-loop iteration that actually completed (HEALTH-32). No other thread may send
  `WATCHDOG=1`. When `WATCHDOG_USEC` is unset, no watchdog messages are sent.
* **SVC-08** The service MUST send `STATUS=<STATE>: <summary>` on every health transition,
  for example `STATUS=DEGRADED: m2 cells_stale` or `STATUS=HEALTHY: 3/3 modules ok`.
  `<STATE>` is the uppercase health state and `<summary>` is the transition reason
  (HEALTH-18). This text appears in `systemctl status`.
* **SVC-09** The service MUST send `STOPPING=1` when it starts any shutdown (signal, `Exit`
  action or crash handler) before it does the shutdown work.
* **SVC-10** If the main loop stops iterating, systemd's watchdog MUST kill and restart the
  service within `WatchdogSec`. **MANUAL** (hardware acceptance: freeze the loop with a
  debug hook and observe the restart).

## 4. Signals and clean stop

* **SVC-11** `SIGTERM`, `SIGINT` and `SIGHUP` MUST all start the same clean stop. `SIGHUP`
  does not reload configuration. The handler only sets a flag. The main loop notices it within
  one receive timeout and runs the clean-stop sequence:
  1. `STOPPING=1` (SVC-09);
  2. publish `<MQTT_BASE_TOPIC>/status` = `offline` (retained) and the final service state
     (`06-mqtt.md`) without waiting for them, and close the local history (LOG-10);
  3. `bus.shutdown()` on the CAN bus, so the adapter is released cleanly (see the adapter
     stall quirk in `03-health-and-recovery.md`);
  4. stop the MQTT publisher (MQTT-10), which publishes the retained `offline` again, waits
     at most 2 s for it and disconnects, then exit with status 0.

  The duplicate retained `offline` is accepted: it lets the bus be released in step 3
  without first waiting on the broker.
* **SVC-12** The clean-stop sequence MUST finish within 10 s so that it stays inside
  `TimeoutStopSec`. Further signals received during the sequence MUST be ignored.
* **SVC-13** The `Exit` action (HEALTH-27) MUST run steps 1–4 of SVC-11 but exit with its own
  code instead of 0.

## 5. `can-up` helper

`/usr/local/lib/dyness-b3-bms-mqtt/can-up` is a POSIX `sh` script that runs as root. It is
called by `ExecStartPre=+`, by `recover`, and by udev on hotplug (SVC-20). It brings the
SocketCAN link up with the configured settings.

* **SVC-14** `can-up` MUST behave as follows:
  1. **Inputs.** Read `CAN_INTERFACE`, `CAN_CHANNEL`, `CAN_BITRATE`, `CAN_RESTART_MS` and
     `CAN_LISTEN_ONLY` from the environment. Any that are unset or empty MUST be read from
     `/etc/default/dyness-b3-bms-mqtt` if it exists. That path MUST be overridable with
     `ENV_FILE`, used only as a test seam; like `SYSFS_ROOT` and `DEV_ROOT` (SVC-18) it is
     a helper test input, not service configuration, and is not part of
     `08-configuration.md`. The script MUST parse only these `KEY=value` lines (stripping
     one layer of matching single or double quotes, as systemd does), and MUST NOT source or
     execute the file, because it runs as root and the file can hold an `MQTT_PASSWORD`
     containing shell metacharacters. Values still unset take
     the defaults from `08-configuration.md`. A test MUST assert that the script's defaults
     equal `08`'s.
  2. **Scope.** If `CAN_INTERFACE` is not `socketcan`, exit 0. If an argument is given
     (udev passes the interface name) and it is not `$CAN_CHANNEL`, exit 0.
  3. **Absent link.** If `/sys/class/net/$CAN_CHANNEL` does not exist, log
     `can-up: $CAN_CHANNEL absent` and exit 0.
  4. **Already correct.** Read `ip -details link show dev "$CAN_CHANNEL"`. If the link is
     `UP`, `bitrate` equals `$CAN_BITRATE`, `restart-ms` equals `$CAN_RESTART_MS` or `0`,
     and `LISTEN-ONLY` appears in the control-mode list exactly when `CAN_LISTEN_ONLY` is
     true, exit 0 without changes. `restart-ms 0` is accepted because a device that refuses
     automatic bus-off restart (step 5) stays at `0`, and comparing against
     `$CAN_RESTART_MS` alone would bounce its link on every run. The script keeps no state,
     so a supporting device left at `0` by hand is also accepted.
  5. **Configure.** Otherwise run, in order:

     ```sh
     ip link set dev "$CAN_CHANNEL" down
     ip link set dev "$CAN_CHANNEL" type can bitrate "$CAN_BITRATE" restart-ms "$CAN_RESTART_MS" listen-only "$LISTEN"   # LISTEN is on or off
     ip link set dev "$CAN_CHANNEL" type can bitrate "$CAN_BITRATE" listen-only "$LISTEN"   # only if the previous command failed and CAN_RESTART_MS is not 0
     ip link set dev "$CAN_CHANNEL" up
     ```

     If the combined command fails and `$CAN_RESTART_MS` is not `0`, the script MUST retry
     it once without `restart-ms`. Some devices reject `restart-ms` outright; the `gs_usb`
     driver on kernel 6.18 answers `Error: Device doesn't support restart from Bus Off.`,
     and the whole command then sets nothing, not even the bitrate. When the retry
     succeeds, the script MUST log one line saying the device does not support automatic
     bus-off restart and `CAN_RESTART_MS` is ignored, and its success line reports
     `restart-ms 0`. When `$CAN_RESTART_MS` is `0` there is no retry. The first failure is
     logged as in step 6 either way, and `up` runs whatever the outcome.
  6. **Always exit 0.** Every failure MUST be logged to stderr (which journald captures)
     with the failing command and its output, and the script still exits 0. A link that is
     not up is reported by the service's bus open, not by a failed `ExecStartPre`.
  7. `can-up` MUST NOT wait or sleep, so it stays within udev's `RUN+=` time budget.
  8. **Journal outside systemd.** When `$JOURNAL_STREAM` is unset or empty (systemd sets it
     when a unit's stderr is the journal, so it is absent under udev) and `logger` is on
     `PATH`, every line the script logs to stderr MUST also be passed to
     `logger -t can-up -p daemon.info`, without the `can-up: ` prefix. udev discards the
     output of `RUN+=` programs, so without this a boot-time, replug or post-reset run
     (SVC-20) leaves no trace; with it those runs show in `journalctl -t can-up`. Under the
     unit `logger` MUST NOT be called, so each line reaches the journal once. stderr output
     is the same in both cases.

## 6. `recover` helper

`/usr/local/lib/dyness-b3-bms-mqtt/recover` is a POSIX `sh` script that runs as root from
`ExecStopPost=+`. systemd sets `$EXIT_STATUS` and `$SERVICE_RESULT`, and the unit's
`EnvironmentFile=` variables, for it.

* **SVC-15** `recover` MUST write `last_exit.json` to `${STATE_DIRECTORY:-/var/lib/dyness-b3-bms-mqtt}`
  on every run, before doing anything else:

  ```json
  {"code": 76, "result": "exit-code", "at": "2026-10-09T12:00:00Z"}
  ```

  `code` is `$EXIT_STATUS` as a JSON integer when it is numeric, as a JSON string when it is
  a signal name (for example `"ABRT"` after a watchdog kill), or `null` if it is unset.
  `result` is `$SERVICE_RESULT` (or `null`). `at` is `date -u +%Y-%m-%dT%H:%M:%SZ`. The file
  MUST be written to a temporary file and renamed into place, with mode `0644`, so the
  dynamic user can read it even though root owns it.
* **SVC-16** `recover` MUST then dispatch on `$EXIT_STATUS`:

  | `EXIT_STATUS` | Action |
  |---|---|
  | `75` | Link bounce: `ip link set dev "$CAN_CHANNEL" down`, then run `can-up` |
  | `76` | `ip link set dev "$CAN_CHANNEL" down` (errors ignored), the USB reset sequence (SVC-18), then `can-up` |
  | anything else | Nothing |

  For `75` and `76` it MUST do nothing unless `CAN_INTERFACE` is `socketcan`. CAN settings
  are resolved exactly as in SVC-14 step 1.
* **SVC-17** `recover` MUST always exit 0, log every step and its result to stderr, and
  finish within 12 s in total, which is inside `TimeoutStopSec`. Waits in SVC-18 MUST be
  shortened to stay within this budget. If it is killed anyway, the restart still runs
  `can-up` through `ExecStartPre`.

## 7. USB reset sequence

The Zero 2's OTG port cannot switch VBUS off, so a true power cycle is not possible. The
sequence below resets the device logically. A powered hub controlled with `uhubctl` is a
possible future hook. It is not a requirement and has no configuration variable.

* **SVC-18** The USB reset MUST run these steps in order. Steps 3–5 are alternatives: each
  runs only if the one before it failed (non-zero exit status or a write error).
  1. **Resolve from the netdev.** If `/sys/class/net/$CAN_CHANNEL/device` exists, take
     `readlink -f` of it (the USB interface directory, for example `.../1-1:1.0`) and use
     its parent directory as the USB device (for example `.../1-1`).
  2. **Fallback scan.** Otherwise, scan `/sys/bus/usb/devices/*/idVendor` and `idProduct`
     for `CAN_USB_ID` (`vvvv:pppp`) and use the first match in sorted order. Log any other
     matches. If nothing matches, log `usb device not found` and go straight to step 6.
  3. **USBDEVFS_RESET ioctl.** Read `busnum` and `devnum` from the device directory and
     form the address `BBB/DDD` (each zero-padded to 3 digits). If `usbreset` (from
     usbutils) is on `PATH`, run `usbreset BBB/DDD`; usbutils' `usbreset` accepts a
     bus/device number, vendor:product ID, serial or product name, but not a
     `/dev/bus/usb/...` device-node path. Otherwise run the system interpreter (not the
     service venv) on the device node:

     ```sh
     /usr/bin/python3 -c 'import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_WRONLY); fcntl.ioctl(fd, 0x5514, 0); os.close(fd)' "/dev/bus/usb/$BBB/$DDD"
     ```

     `0x5514` is `USBDEVFS_RESET` (`_IO('U', 20)`).
  4. **De-authorise.** Write `0` to `<device>/authorized`, wait 2 s, write `1`.
  5. **Driver rebind.** For each interface directory `<device>:*` bound to `gs_usb`, write
     its basename to `/sys/bus/usb/drivers/gs_usb/unbind` and then to
     `/sys/bus/usb/drivers/gs_usb/bind`.
  6. **Wait and configure.** Poll every 0.5 s, for up to 10 s (shortened per SVC-17), for
     `/sys/class/net/$CAN_CHANNEL` to exist. Then run `can-up` whether or not it appeared.

  The paths `/sys` and `/dev` MUST be overridable with `SYSFS_ROOT` and `DEV_ROOT`, used only
  as test seams. These are helper test inputs, not service configuration, and are not part of
  `08-configuration.md`. `ENV_FILE` (SVC-14 step 1) is the third such seam.
* **SVC-19** A real exit 76 on the Pi MUST bring a stalled SH-C31A back to receiving
  without a physical replug, or the log MUST show which step failed. **MANUAL**.

## 8. udev hotplug rule

* **SVC-20** The repository MUST ship `packaging/80-dyness-b3-can.rules`, installed as
  `/etc/udev/rules.d/80-dyness-b3-can.rules`, with exactly this content:

```udev
# Configure the Dyness B3 CAN link whenever a CAN netdev appears (boot, replug, USB reset).
# can-up ignores interfaces other than CAN_CHANNEL and only runs ip(8), so it fits in the
# short time budget udev allows RUN+= programs.
ACTION=="add", SUBSYSTEM=="net", KERNEL=="can*", RUN+="/usr/local/lib/dyness-b3-bms-mqtt/can-up $name"
```

  udev discards what `RUN+=` programs print, so `can-up` logs these runs through `logger`
  (SVC-14 step 8): `journalctl -b -t can-up` shows how the link was configured at boot and
  after a replug or USB reset.

* **SVC-21** Unplugging and replugging the adapter while the service runs MUST bring the
  link back up configured (through the udev rule) and the service back to `HEALTHY`
  (through `STALE` → `STARTING`, or a restart), with no manual action. **MANUAL**.

## 9. Logging

* **SVC-22** The service MUST log only to stdout/stderr in the format of CFG-10. journald
  collects it under the unit, so `journalctl -u dyness-b3-bms-mqtt -f` follows the service
  and its helpers. The helpers MUST prefix every line with their own name (`can-up:`,
  `recover:`). `can-up` runs started by udev are not under the unit; they reach the journal
  through `logger` with the tag `can-up` (SVC-14 step 8).

## 10. Hardware acceptance (MANUAL summary)

These items are covered by the hardware acceptance checklist in `11-testing.md`:

| ID | Check |
|---|---|
| SVC-02 | `systemd-analyze verify` on the installed unit reports no unknown directives |
| SVC-10 | Watchdog restarts a frozen main loop |
| SVC-19 | Exit 76 revives a stalled adapter without a replug |
| SVC-21 | Hotplug brings the link and the service back |

Everything else in this document is covered by automated tests: unit-file and udev-rule
content checks, a fake `NOTIFY_SOCKET`, signal tests on the real process, and helper scripts
run against a fake `SYSFS_ROOT`/`DEV_ROOT` and a temporary `ENV_FILE` with a stub `ip` on
`PATH`.
