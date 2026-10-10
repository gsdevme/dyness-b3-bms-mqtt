# Hardware acceptance 2026-10-10

**Partial run.** The automated hardware checks and steps 1, 2, 3, 4, 5, 11, 15 and 16 were
run. Steps 6, 7, 8, 9, 10, 12, 13, 14 and 17 remain, and no **UNVERIFIED** fact was
checked. TEST-39 is not yet satisfied; the remaining steps go in a later dated record.

Release: v0.1.2 (initial install, hour run), v0.2.0, v0.3.0 (current) · Kernel:
6.18.50+rpt-rpi-v8 · Debian: 13.7 (Pi OS Lite 64-bit Trixie) · Settings changed: `MQTT_HOST`;
`CAN_LISTEN_ONLY=false` for most of the run (`true` for step 1 and the listen-only tests)

Hardware: Pi Zero 2 W, SH-C31A (USB `1d50:606f`) on the kernel `gs_usb` driver (SocketCAN),
3 modules, MQTT broker without authentication, Home Assistant discovery on.

| Check | Result | Notes |
|---|---|---|
| Hardware tests, listen-only | pass | v0.1.2. 1 passed, 1 skipped |
| Hardware tests, with poll | pass | v0.1.2. 2 passed |
| Step 1 | pass | v0.1.2. Image was Trixie, not the Bookworm INST-15 named (spec updated). Service `active (running)`, HEALTHY about 1 s after start; 5 discovery messages; 3 modules and the stack in HA with 15 cells each, no cell 16 |
| Step 2 | pass | v0.1.2. 1 h 44 min with polling. RX dropped stayed at 229 across 434k frames (all 229 predate the service); no STALE, no reopen. One MQTT keepalive-timeout disconnect at 12:00:09, reconnected in 1 s with discovery republished; it coincided with a router firewall change. The disconnect line was logged twice |
| Step 3 | pass | v0.3.0, unplugged 14:19 BST. `link_down` fault → STALE → reopen attempts at 14:19:16 and 14:19:31, both `No such device` → 14:19:46 exit 75 `reopen_exhausted`. `recover`: `exit status 75: bouncing can0`, link down fails `Cannot find device`, runs `can-up` (`can0 absent`). systemd restart counter 1 at 14:19:52; health `none -> dead (adapter_absent)`, reopen retried every 60 s. Run straight on into step 4, so the replug is recorded there |
| Step 4 | pass | 14:34:54 `exiting with status 76: dead_usb_reset`. `recover`: `resetting the USB adapter of can0`, `ignoring the failed link down`, `usb device not found`, `can0 did not appear after 16 polls of 0.5 s`, `can-up`, exit 0. systemd restart counter 2 at 14:35:08, DEAD (`adapter_absent`). Replug 14:38:51: `journalctl -b -t can-up` shows the configure, the `restart-ms` refusal, the retry and `can0 up: bitrate 500000 restart-ms 0 listen-only off`; service 14:38:53 `link absent -> error-active`, 14:39:10 reopen succeeded, 14:39:13 HEALTHY (SVC-21). MQTT (`verify-mqtt`) agreed throughout: escalation 1, `service_restarts` 7, `last_exit_code` 76, then healthy |
| Step 5 | pass | v0.3.0, link down 14:41:48 BST, about 2 min after the step 4 recovery, so `RECOVERY_RESET_S` had not elapsed and escalation was still 1. The spec's expected exit 75 and link bounce therefore became exit 76 and a USB reset. Journal: `link_down` fault → STALE → reopen at 14:41:50 opened and faulted at once (link `error-active -> down` 14:41:53) → second reopen 14:42:03, same → 14:42:18 `stale -> recovering (reopen_exhausted: 2/2 reopen attempts)`, `exiting with status 76: reopen_exhausted`. `recover`: `exit status 76: resetting the USB adapter of can0`, `ip link set dev can0 down: ok`, `usb device /sys/devices/platform/soc/3f980000.usb/usb1/1-1/1-1.3 (from can0)`, `usbreset /dev/bus/usb/001/005: failed with status 1` / `No such device found`, `wrote 0 to …/1-1.3/authorized`, `waiting 2 s`, `wrote 1 to …/authorized`, `can0 present`, `can-up` (`restart-ms` refusal, retry, `can0 up: bitrate 500000 restart-ms 0 listen-only off`), `can-up finished with status 0`. systemd restart counter 3 at 14:42:27; 14:42:30 `starting -> healthy (all_ok: 3/3 modules ok)`. 42 s from link down to HEALTHY. MQTT (`verify-mqtt`) agreed: `stale` with `bus_error: link_down: Error receiving: Network is down`, then `healthy`, escalation 2, `service_restarts` 8. First real run of the SVC-18 USB reset: `usbreset` failed (see "Fixes made during the run") and the sysfs `authorized` fallback brought the adapter back. The escalation reset after `RECOVERY_RESET_S` healthy was not observed |
| Step 6 | not run | |
| Step 7 | not run | |
| Step 8 | not run | |
| Step 9 | not run | |
| Step 10 | not run | |
| Step 11 | pass | v0.2.0, then v0.3.0 by `sudo reboot` and later hard power-offs. Service and `can0` up from cold. On v0.2.0 the udev-run `can-up` left nothing in the journal (fixed in PR #15, v0.3.0); on v0.3.0 the boot lines show at 14:03:01, including the `restart-ms` retry |
| Step 12 | not run | |
| Step 13 | not run | |
| Step 14 | not run | |
| Step 15 | pass | v0.3.0. `systemd-analyze verify /etc/systemd/system/dyness-b3-bms-mqtt.service` printed nothing, exit 0 (SVC-02) |
| Step 16 | pass | `sudo make install` of v0.2.0 over v0.1.2: `sha256sum /etc/default/dyness-b3-bms-mqtt` identical before and after (`f04b1aa2…56f48`); `kept existing` logged; package 0.1.2 → 0.2.0; service restarted, HEALTHY. Repeated for v0.3.0 |
| Step 17 | not run | |

## Incidental observations

Not checklist steps.

| Action | Observation |
|---|---|
| `sudo ip link set can0 down`, then `can-up` by hand | fault → STALE → reopen after 2 s → HEALTHY in 3 s; no escalation. Step 5 (link left down for the service to recover) is recorded separately |
| Unplug for about 13 s | First reopen failed; the second, 15 s later and after the replug, succeeded. No restart |

## Fixes made during the run

| Finding | Fix |
|---|---|
| The `gs_usb` adapter refuses `restart-ms`, so `can-up`'s whole `ip link set` failed (`Device doesn't support restart from Bus Off`) | PR #11, v0.2.0 |
| `can-up` run by udev (boot, replug, USB reset) left nothing in the journal | PR #15, v0.3.0 |
| Upgrading by hand meant typing the new version twice | `make upgrade`, PR #14, v0.3.0 |
| MQTT-side observations needed the Pi | `verify-mqtt` skill, PR #13, v0.2.0 |
| `recover`'s `usbreset` step can never succeed: usbutils' `usbreset` takes `BBB/DDD`, `VVVV:PPPP`, a serial or a product name, not the `/dev/bus/usb/BBB/DDD` path that `recover` and SVC-18 pass, so it fails with `No such device found` and the sysfs `authorized` fallback does the reset (step 5) | PR #18 |

## Open observations

No fix yet; candidates are in `docs/superpowers/specs/2026-10-10-acceptance-run-1-design.md`.

* The installer's closing hint says to edit the env file even on a reinstall.
* `recovery.json does not exist` is logged at WARNING on every fresh start.
* python-can logs `Created a socket` (INFO) and `Could not access SocketCAN device` (ERROR)
  on every reopen retry.
* After a replug the service waits for the next 60 s DEAD retry rather than reopening when
  the link reappears (step 4: link up 14:38:53, reopen 14:39:10).
* A 2 s link blip produced 38 alert `off → unknown → off` lines.
* The MQTT disconnect line is logged twice.

## Operational notes

* Two hard power-offs caused ext4 orphan cleanup and lost the Pi's wifi profile (its PSK).
  Run `sudo poweroff` and wait before pulling power.
* The journal was not persistent, so `journalctl -b -1` had nothing.
  `sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald` fixed that.
* The Pi was moved to wired Ethernet for the rest of the run.

## UNVERIFIED facts

Not checked in this run.

| Spec | Fact | Confirmed or contradicted | Spec updated |
|---|---|---|---|
