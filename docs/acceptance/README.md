# Hardware acceptance

The manual acceptance run of `docs/specs/11-testing.md` §10 (TEST-39), on the Pi with the
real stack. Run it before the first unattended deployment and again after any change to
`01-protocol.md`, `02-can-transport.md`, `03-health-and-recovery.md` or `09-service.md`.

The spec is the authority for each step's expected observation; this page is the order of
work and the record format.

## 1. Before you start

1. Flash Pi OS Lite 64-bit Bookworm and follow the README quick start, with
   `CAN_LISTEN_ONLY=true` in `/etc/default/dyness-b3-bms-mqtt`.
2. Note the release installed (the archive's version, or `git rev-parse --short HEAD` when
   testing a git checkout), `uname -r` and `cat /etc/debian_version`.
3. Keep a terminal on `make logs`; use `systemctl status dyness-b3-bms-mqtt` and
   `ip -d -s link show can0` for the other observations.
4. The MQTT-side observations (`<base>/status`, health, module freshness, counters) can be
   taken from any machine that reaches the broker with the `verify-mqtt` Claude Code skill
   (`.claude/skills/verify-mqtt/`); journal lines still need the Pi.

## 2. Automated hardware checks

From the checkout, with the service stopped so the tests have the bus to themselves:

```sh
make dev
sudo systemctl stop dyness-b3-bms-mqtt
CAN_INTERFACE=socketcan CAN_CHANNEL=can0 uv run pytest tests/hardware --hardware
CAN_INTERFACE=socketcan CAN_CHANNEL=can0 uv run pytest tests/hardware --hardware --hardware-poll
sudo systemctl start dyness-b3-bms-mqtt
```

The first run is listen-only and transmits nothing. The second may send the module poll
`0x18F21m77` and nothing else (TEST-38). Record both results.

## 3. Checklist

Do the steps in order. The expected observations are in the TEST-39 table.

| # | Action | Covers |
|---|---|---|
| 1 | Fresh install, `CAN_LISTEN_ONLY=true`, start the service | INST-15 |
| 2 | `CAN_LISTEN_ONLY=false`, `sudo make restart`; check again an hour later | SVC-19 |
| 3 | Unplug the adapter for 2 min, then replug | SVC-21 |
| 4 | As step 3, but stay unplugged past `DEAD_USB_RESET_S` | |
| 5 | `sudo ip link set can0 down` | |
| 6 | Stop the broker for 2 min, then start it | |
| 7 | Restart Home Assistant | |
| 8 | `sudo kill -STOP <pid>` | SVC-10 |
| 9 | `MQTT_PORT=0`, restart; then restore it and restart | |
| 10 | `sudo systemctl stop dyness-b3-bms-mqtt` | |
| 11 | Power-cycle the Pi | |
| 12 | 24 h soak in normal operation | TEST-41 |
| 13 | Local log housekeeping (old `modules-`/`stack-` files, restart) | |
| 14 | Disk full, then free the space | LOG-22 |
| 15 | `systemd-analyze verify` on the installed unit | SVC-02 |
| 16 | Edit the env file, `sudo make install` again, compare `sha256sum` | INST-16 |
| 17 | Userspace `gs_usb` listen-only hardware tests on the macOS development machine, then back to the Pi | CAN-17 |

Step 2's hour-long run with polling is the SVC-19 evidence: it is where an adapter stall
after transmitting would show. If the adapter stalls and the escalation reaches exit 76,
the USB reset has to bring it back without a replug, or the log has to name the step that
failed. Step 4 exits 76 with the adapter unplugged, so it cannot show a stalled adapter
recovering.

Cross-check (TEST-40): `uv run pytest --req-report` ends with the list of **MANUAL**
requirements. Every identifier in it must appear in a "Covers" cell above, or be TEST-39 or
TEST-40 themselves.

## 4. Recording results

Add `docs/acceptance/YYYY-MM-DD-hardware-acceptance.md` for the date of the run:

```markdown
# Hardware acceptance YYYY-MM-DD

Release: <version or sha> · Kernel: <uname -r> · Debian: <version> · Settings changed: <list>

| Check | Result | Notes |
|---|---|---|
| Hardware tests, listen-only | pass/fail | |
| Hardware tests, with poll | pass/fail | |
| Step 1 | pass/fail | |
| … | | |
| Step 17 | pass/fail | |

## UNVERIFIED facts

| Spec | Fact | Confirmed or contradicted | Spec updated |
|---|---|---|---|
```

Any **UNVERIFIED** fact the run confirms or contradicts must be updated in `docs/specs/`
(with a dated design doc) in the same change as the record.
