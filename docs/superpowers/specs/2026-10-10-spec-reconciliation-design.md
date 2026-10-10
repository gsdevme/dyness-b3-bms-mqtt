# dyness-b3-bms-mqtt — spec reconciliation

* **Date:** 2026-10-10
* **Status:** accepted
* **Scope:** a full sweep comparing the living specs with the code, tests, packaging, CI and
  docs after the 0.1.x release work, and the edits that bring them back into agreement

After the 0.1.0 and 0.1.1 releases (install from the release archive, single-sourced
version, `vX.Y.Z` tags) the living specs had drifted from the code in many small places:
wording written before implementation, behaviour the implementation refined, and release
machinery the specs did not describe. An audit compared every spec with the code, tests,
`Makefile`, packaging, CI workflows, README and acceptance checklist. Almost every finding
was resolved by making the spec describe what the code already does. One finding changed
code, because the spec's intent was right and the code was not. Earlier design records are
unchanged.

## Behaviour change

1. **TEL-04, TEL-06, TEL-07** (`04-telemetry.md`, `telemetry.py`, `test_telemetry.py`):
   `cells_fresh` now requires all five cell frames `0x11`–`0x15` to have been received, in
   addition to `online` and the oldest of them being within `CELL_STALE_S`. Before, it only
   checked the oldest age among the frames received so far, so a module with four of the
   five frames read as fresh. `cell_age_s` is unchanged: it stays the age of the oldest
   cell frame actually received, and `None` until one has arrived.

   Rationale: HEALTH-04 ages a cell frame never received from the bus open, so the health
   machine already kept such a module `CELLS_STALE`. Published `cells_fresh` and module
   status could disagree, and a partial cell set could feed the stack extremes (TEL-12) and
   the `cell_low` input (ALERT-05) with a minimum that ignores the missing cells. With the
   change both fall back to the `0x22` extremes until the stream is complete. Here the code
   follows the spec's intent; everywhere else below the spec follows the code.

## Requirement and test changes

2. **HEALTH-34** (`03-health-and-recovery.md`, `tests/unit/test_action_blocking.py`):
   promoted from "may" to MUST. A new test runs the real `App` on the virtual bus through a
   silence-to-exit sequence, times each action on the fake clock, and asserts that no action
   other than `ReopenBus` blocks for more than 1 s.
3. **INST-17, INST-18** (`tests/harness/test_project.py`, `release-please.yml`): the
   manifest assertion is now exact (`.release-please-manifest.json` equals `__version__`).
   The `0.0.0` bootstrap allowance only mattered before the first release; the workflow
   comment is rewritten to match.

## Spec wording aligned with the code

* **`00-overview.md`**: scope names the release-archive install, single-sourced version,
  release-please tags and `make check`/CI. The cell-stream glossary entry no longer claims
  the stream continues indefinitely; it points at the **UNVERIFIED** row in
  `01-protocol.md` §5. Plans live under `docs/superpowers/plans/`. The defaults rule exempts
  worked examples labelled as using the defaults (`05-alerts.md` §3,
  `07-home-assistant.md` §2). The index lists INST-17/18 and TEST-33..35. New §7 maps each
  source module to its responsibility and spec.
* **`01-protocol.md`**: an informative note on PROTO-04 says the transport's acceptance set
  excludes the `IGNORED` stack ids, so `IGNORED` never occurs at runtime and
  `unknown_frames` only counts module-range ids. The §5 overnight cell-stream row cites
  the authority's `docs/findings.md`, which records the all-night stream.
* **`02-can-transport.md`**: `open()` is idempotent and the context manager does not open
  (CAN-01). `LinkState` gains `rx_dropped` from `stats64.rx.dropped`, published as
  `dropped_frames`; a missing `ctrlmode` inside `info_data` gives `listen_only=False`
  (CAN-28). The fault table and CAN-32 state the code's order: any errno that matches no
  earlier row is `OS_ERROR` before `OPEN_FAILED`/`CAN_ERROR`, and the send-timeout forms of
  `TX_BLOCKED` apply only when no errno was found.
* **`03-health-and-recovery.md`**: any bus-activity frame moves `STALE`/`DEAD` to
  `STARTING` whether or not a reopen ran (HEALTH-17). A failed `recovery.json` write is
  logged and the exit code is unchanged (HEALTH-15). `MALFORMED` frames are reported as the
  heartbeat id. Every transport fault except `TX_BLOCKED` goes to `on_bus_exception`, after
  which the application closes the bus until a `ReopenBus`. `SetAvailability` is
  de-duplicated per module and sorted (HEALTH-19).
* **`04-telemetry.md`**: flag classes are named `protection` and `status`, as
  `FlagCategory` spells them, instead of `cut` and `state` (also in `05-alerts.md`,
  `07-home-assistant.md` and an `alerts.py` docstring). TEL-04/06/07 describe the cell
  freshness of change 1.
* **`05-alerts.md`**: `imbalance_pct` is computed from the snapshot's 1 dp `current_a`
  values, not unrounded currents (ALERT-06). ALERT-17 states what each kind of alert adds
  to its log line: value and threshold, flag list, or nothing for roll-ups.
* **`06-mqtt.md`**: keepalive is `math.ceil(MQTT_KEEPALIVE_S)` (MQTT-04). Callbacks
  (including `on_connect_fail`) only record events and notices under a lock; the main loop
  drains them with `take_events()` (MQTT-05). Refused and failed connects log at `WARNING`
  (MQTT-06).
* **`07-home-assistant.md`**: the component option list names the options HA-12, HA-13 and
  HA-17 add.
* **`08-configuration.md`**: unknown command-line arguments exit 78 with the usage line.
* **`09-service.md`**: SVC-02 verifies the installed unit, as acceptance step 15 does.
* **`10-installation.md`**: the release archive's contents include `src/`.
* **`11-testing.md`**: requirement text also ends at a code fence, code spans are ignored
  when classifying (TEST-05), rule 1 is strict only through `--req-strict` in `addopts`
  (TEST-06), and `--stepwise` and collection errors narrow the run (TEST-08).
* **README and `CLAUDE.md`**: README status, CAN wiring facts, local logging and the MQTT
  spec link; `CLAUDE.md` gains release rules (Conventional Commits, single version line,
  annotated lines owned by release-please).

## Acceptance step 17 for CAN-17

CAN-17 (**MANUAL**) is about `gs_usb` in listen-only mode on the real SH-C31A, which is the
macOS development path. Step 1 of TEST-39 claimed it, but step 1 runs on the Pi with
SocketCAN and never exercises `gs_usb`, so no step covered it. Step 17 moves the adapter to
the macOS machine, runs the listen-only hardware tests with `CAN_INTERFACE=gs_usb`, then
moves it back and checks the Pi recovers. Step 1 no longer lists CAN-17, and the
acceptance README's checklist and record template run to step 17.

## Decisions without code changes

* **INST-03, uv receipt**: when the installer fetches uv, Astral's `install.sh` writes
  `~root/.config/uv/uv-receipt.json`, and `UV_NO_MODIFY_PATH` does not suppress it. The
  spec now lists it as a permitted write. The `Makefile` is unchanged; removing the receipt
  would only break `uv self update`.
* **CFG-06, LOG_LEVEL**: the code applies `LOG_LEVEL` before logging the start-up
  configuration, so at `WARNING` or above those lines are not written. That is kept; CFG-06
  and CFG-11 now say so, and that `--check-config` always logs at `INFO`.
* **TEST-36**: the 90 % coverage target stays a SHOULD, and the spec states that neither the
  suite nor CI measures coverage at present.

## Follow-ups outside this repo

* `../dyness-debug/docs/protocol.md` still says the cell-stream duration without a poll
  "isn't known yet", although `docs/findings.md` in the same repo records the all-night
  stream. The SH-C31A echo observation (userspace `gs_usb` on macOS returns our own
  transmitted frames) is not recorded there at all.
* dyness-debug is a private repository while this one is public, so the `../dyness-debug/`
  references in `00-overview.md`, `01-protocol.md` and `CLAUDE.md` should not be relied on
  long term. `01-protocol.md` already carries every fact it needs; a later change should
  make it the self-contained protocol reference for this project.

## Risks

* Change 1 can hold a module at `cells_fresh=false` longer than before when one cell frame
  is missing, and cell sensors stay `null` until it arrives. That matches the module status
  already published, so Home Assistant sees one consistent picture.
