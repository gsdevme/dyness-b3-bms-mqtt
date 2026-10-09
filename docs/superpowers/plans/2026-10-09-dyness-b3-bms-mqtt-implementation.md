# dyness-b3-bms-mqtt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (chosen by the user) to implement this plan task-by-task. Each task follows the same five steps: write the failing tests, run them and see them fail, implement, run `make check` and see it pass, commit. Implementers read the spec sections named in the task; the specs carry the exact requirements, so this plan carries the contracts between tasks rather than repeating the code.

## Context

The specification phase is done and committed on `main` (`920539a`): twelve living specs in `docs/specs/` with 283 numbered requirements, a dated design record, README and CLAUDE.md. This plan implements them. The user chose subagent-driven execution, GitHub Actions for lint/types/tests plus a Mosquitto job, and release-please for versioning and tags.

**Goal:** A systemd service on a Pi Zero 2 W that reads a 3 × Dyness B3 stack over CAN, publishes module, stack and service state to MQTT with Home Assistant device discovery, keeps a local daily CSV history on the Pi for direct analysis, recovers on its own, and has every MUST traced to a test.

**Architecture:** One synchronous Python 3.14 process. Pure cores (`protocol`, `telemetry`, `alerts`, `health`, `discovery`, `payloads`) with injected clocks; thin I/O shells (`canbus`/`linkstate` on python-can, `mqtt` on paho 2.x, `sdnotify`, `state_store`, `app`). Tests drive a `SimulatedStack` on python-can `virtual` with a `FakeClock`; nothing sleeps.

**Tech Stack:** Python 3.14 via uv (`uv_build` backend), python-can 4.x, paho-mqtt 2.x, optional extra `gs-usb`. Dev: pytest, pytest-timeout, hypothesis, ruff, mypy --strict. POSIX sh helpers, GNU make, GitHub Actions, release-please.

**Spec:** `docs/specs/00`–`11` and `docs/superpowers/specs/2026-10-09-dyness-b3-bms-mqtt-design.md`. Protocol authority for anything unclear: `../dyness-debug/docs/protocol.md`.

## Global Constraints

- Python 3.14 pinned three ways: `requires-python = ">=3.14"`, `.python-version` = `3.14`, `uv.lock` committed from Task 1 (CI uses `--frozen`, INST-11).
- Runtime deps exactly `python-can>=4.5,<5` and `paho-mqtt>=2,<3`; extra `gs-usb` = `gs-usb` + `pyusb`; no sd_notify dependency (INST-13).
- Console script `dyness-b3-bms-mqtt = "dyness_b3_bms_mqtt.app:main"` (INST-14).
- One version source: `pyproject.toml` `version` (bumped by release-please); `__version__ = importlib.metadata.version("dyness-b3-bms-mqtt")`.
- `make check` = `ruff check`, `ruff format --check`, `mypy --strict src tests`, `pytest`, fail-fast in that order (TEST-33).
- pytest `--strict-markers`; markers `req`, `integration`, `mosquitto`, `hardware` (TEST-02). Every test carries `@pytest.mark.req("PREFIX-NN", ...)`.
- `time.sleep` raises outside `integration`; integration tests get a 10 s timeout; tests never read `../dyness-debug` at runtime (TEST-11, TEST-14).
- TX safety: the only transmittable frame is extended `0x18F21m77` with data `01 00 00 00 00 00 00 00`, m in 1..MODULE_COUNT. `build_poll` checks `type(x) is int`, ranges 1..4 and 1..module_count, builds with bitwise OR, validates with `if … raise`, never `assert`. `PollFrame.__post_init__` refuses any id outside the four valid polls. Listen-only never transmits.
- Sign: module `current_a = -current_raw_a`; `0x356` current unchanged; limits are magnitudes; rounding is `round(x, n) + 0.0` so −0.0 becomes 0.0.
- 15S: slot 16 and T4 ignored. `0x22` cell numbers are 0-based on the wire, 1-based everywhere else.
- Exit codes only 0, 1, 75, 76, 78; 75/76 come only from the health `Exit` action.
- `bus.shutdown()` on every exit path. Constants: recv timeout 0.2 s, reopen gap 2 s, link query 10 s.
- Env names and defaults live only in `config.DEFAULTS`, mirroring `08-configuration.md`. Logs to stdout, `LEVEL message`, no timestamp or colour.
- Doc blocks over inline comments (file/class/function docstrings; no narration, no commented-out code).
- Branch `feat/implementation` off `main`. Conventional Commits, no `chore`, one atomic commit per task, each ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Merge to `main` by rebase/fast-forward so release-please sees the individual commits.
- Traceability rule 1 (uncovered MUST fails) is opt-in via `--req-strict` until Task 15 adds it to `addopts`; rules 2–4 enforced from Task 2.

## Review Focus

1. Negative zero from the sign flip (raw 4000 → −0.0 A; also ±x stack totals). Tasks 4 and 5: `test_worked_example_details` (copysign), `test_negative_zero_is_published_as_zero`.
2. A MALFORMED module-range frame (DLC ≠ 8) keeps the bus alive but must not refresh module ages; app reports it to health as `HEARTBEAT_ID`. Task 12: `test_malformed_frames_keep_bus_alive_not_module`.
3. `ip -j` omits `ctrlmode` when no control-mode flag is set; with `info_data` present, missing `ctrlmode` means `listen_only=False`. Task 8: `test_missing_ctrlmode_means_not_listen_only`.
4. Lenient Python number parsing: `500_000`, `0x10`, `1e3`, `nan`, `inf`, ` 5`, non-ASCII digits must be rejected. Task 3: `test_integers_are_strict_ascii`, `test_durations_are_finite_plain_decimals`.
5. Hot loop after a recv fault: app closes the transport and idles 0.2 s via injected sleep; exactly one `on_bus_exception` per fault. Task 12: `test_recv_fault_does_not_spin`.
6. SD card full or read-only: local logging must degrade to `error` and the service keep publishing. Task 12b: `test_write_oserror_sets_error_status_and_never_raises`.

## File Structure

**src/dyness_b3_bms_mqtt/** — `__init__.py` (`__version__`), `__main__.py`, `py.typed`, `config.py`, `logsetup.py`, `protocol.py`, `status.py`, `telemetry.py`, `alerts.py`, `timefmt.py`, `state_store.py`, `health.py`, `linkstate.py`, `canbus.py`, `sdnotify.py`, `discovery.py`, `payloads.py`, `mqtt.py`, `localog.py`, `app.py`.

**tests/** — `conftest.py`; `plugins/{markers,reqtrace,golden}.py`; `support/{settings,makefile}.py`; `fakes/{clock,transport,flaky,publisher,notify}.py`; `sim/{encode,stack,paced}.py`; `vectors/` (loader, `t1-examples`, `t2-examples`, `synthetic`, `edge`, `captures/`, each `.txt` + hand-written `.expected.json`); `golden/{discovery,state}/`; `harness/`, `unit/`, `integration/`, `hardware/`, `packaging/` test modules.

**packaging/** — `dyness-b3-bms-mqtt.service`, `can-up`, `recover`, `80-dyness-b3-can.rules`, `dyness-b3-bms-mqtt.env`.

**root** — `pyproject.toml`, `.python-version`, `uv.lock`, `Makefile`, `release-please-config.json`, `.release-please-manifest.json`, `docs/acceptance/README.md`, `docs/superpowers/plans/2026-10-09-dyness-b3-bms-mqtt-implementation.md` (this plan, committed).

**.github/workflows/** — `ci.yml` (jobs `check`, `mqtt`), `release-please.yml`.

---

### Task 0: Spec amendments and committed plan

Planning surfaced small spec gaps. Fix them first so implementers never contradict a spec.

**Files.** Modify `docs/specs/01-protocol.md` (PROTO-30), `02-can-transport.md` (CAN-20), `05-alerts.md` (ALERT-06, ALERT-11), `04-telemetry.md` (TEL-04), `09-service.md` + `11-testing.md` (helper `ENV_FILE` test seam), `11-testing.md` TEST-34 (CI on every push and on pull_request; Mosquitto job uses `eclipse-mosquitto:1.6` because 2.x needs a config file a service container cannot be given), `11-testing.md` TEST-05 note. Create `docs/superpowers/specs/2026-10-09-implementation-amendments-design.md` recording each change and why, and `docs/superpowers/plans/2026-10-09-dyness-b3-bms-mqtt-implementation.md` (copy of this plan body).

**New spec: local history logging.** Create `docs/specs/12-local-logging.md` (prefix `LOG`), add it to `00-overview.md` §4.1 and §5, and add to `08-configuration.md` §9 "Local logging": `LOCAL_LOG` bool default `true`; `LOCAL_LOG_INTERVAL_S` duration default `10` (1..300); `LOCAL_LOG_RETENTION_DAYS` int default `90` (1..3650); `LOCAL_LOG_COMPRESS_AFTER_DAYS` int default `1` (0..retention). Requirements to write (modelled on `../dyness-debug/overnight_logger.py` CSV + events):
- Location: `$STATE_DIRECTORY/logs/` (so `/var/lib/dyness-b3-bms-mqtt/logs` on the Pi; kept on uninstall unless `PURGE=1`). With no `STATE_DIRECTORY`, logging is disabled with one warning.
- Files per UTC day: `modules-YYYY-MM-DD.csv` (one row per module per interval: `ts, module, online, cells_fresh, cell_01_mv..cell_15_mv, cell_min_mv, cell_min_no, cell_max_mv, cell_max_no, cell_avg_mv, spread_mv, pack_v, current_a, soc, soh, cycles, energy_kwh, chg_limit_a, dis_limit_a, temp_1_c..temp_3_c, temp_mos_c, temp_board_c, flags_raw, bms_alarm, bms_protection`), `stack-YYYY-MM-DD.csv` (one row per interval with the stack payload keys minus alerts), `events-YYYY-MM-DD.log` (one line per health transition, alert change, poll sent, exit, with ISO UTC timestamp). Column order fixed and identical to the MQTT JSON key names; `null` written as empty field.
- Writes every `LOCAL_LOG_INTERVAL_S`, independent of `PUBLISH_INTERVAL_S`, from the same snapshot path; files opened in append mode, header written when the file is created; flushed per write, never fsynced (SD wear); a new day rolls files.
- Housekeeping once per hour and at start-up: gzip files older than `LOCAL_LOG_COMPRESS_AFTER_DAYS` (not today's), delete files older than `LOCAL_LOG_RETENTION_DAYS`.
- Faults: `OSError` on write (disk full, read-only FS) MUST never stop the service. Log once at ERROR, set the service payload field `local_log` to `error`, retry opening at the next housekeeping run. Values: `ok` / `disabled` / `error`.
- Add `local_log` to the MQTT-18 service schema and to the 07 entity table as a diagnostic enum sensor (enabled by default); add the housekeeping and disk-full cases to the TEST-39 acceptance checklist.

**Changes.**
- PROTO-30 vs CAN-20 disagree on where `can.Message` is built; resolve as: the only `can.Message(` construction and `.send(` call is inside `PythonCanTransport.send_poll`, built from a `PollFrame`.
- ALERT-06, ALERT-11, TEL-04: move the word MUST into the lead sentence so the TEST-05 parser (text ends at a blank line) classifies them as MUST.
- SVC/TEST: add `ENV_FILE` beside `SYSFS_ROOT`/`DEV_ROOT` as test-only helper overrides.
- Add a short section to `02-can-transport.md` or `09-service.md` only if the implementer of Task 8 finds a conflict; otherwise none.

**Commit:** `docs(specs): align protocol, alert and CI wording for implementation`

### Task 1: Project scaffold, CI and release automation

**Files.** Create `pyproject.toml`, `.python-version`, `uv.lock`, `src/dyness_b3_bms_mqtt/{__init__.py,py.typed}`, `tests/{__init__,conftest}.py`, `tests/fakes/{__init__,clock}.py`, `tests/plugins/{__init__,markers}.py`, `tests/support/{__init__,makefile}.py`, `tests/harness/__init__.py`, `Makefile` (`dev`, `check`), `.github/workflows/{ci,release-please}.yml`, `release-please-config.json`, `.release-please-manifest.json`. Tests: `tests/harness/test_project.py`, `tests/harness/test_markers.py`.

**Requirements:** TEST-01, 02, 03, 11, 33, 34; INST-04, 11, 13, 14.

**Produces.**
- `dyness_b3_bms_mqtt.__version__: str` via `importlib.metadata.version("dyness-b3-bms-mqtt")`.
- `FakeClock(start: float = 0.0)`: `__call__() -> float`, `now()`, `advance(seconds)` (ValueError if negative), `sleep(seconds)` advances.
- Fixtures `clock` = `FakeClock(1000.0)`, `wall` = `FakeClock(1791504000.0)`.
- `markers.broker_address(environ) -> (host, port)`, `broker_reachable(host, port) -> bool`; options `--hardware`, `--hardware-poll`; `INTEGRATION_TIMEOUT_S = 10.0`.
- `support.makefile.recipe(text, target) -> list[str]`.

**Config contents.**
- pyproject: pytest `testpaths=["tests"]`, `pythonpath=["."]`, `addopts=["--strict-markers","--strict-config","-ra"]`, four markers. Ruff line-length 100, target py314, rules `E W F I B UP SIM RUF D` pep257, D ignored under tests. mypy strict; `ignore_missing_imports` for `gs_usb.*`, `usb.*`. Dev group: pytest, pytest-timeout, hypothesis, ruff, mypy.
- `ci.yml`: `on: push` (all branches) and `pull_request` to `main`. Job `check` (ubuntu-latest): checkout, `astral-sh/setup-uv` with Python 3.14, `uv sync --frozen`, `make check`; virtual bus only, no vcan/modprobe/sudo. Job `mqtt`: service container `eclipse-mosquitto:1.6` on 1883, env `MQTT_HOST=localhost MQTT_PORT=1883`, `uv sync --frozen`, `uv run pytest -m mosquitto`.
- `release-please.yml`: on push to `main`; permissions `contents: write`, `pull-requests: write`; `googleapis/release-please-action@v4` with `config-file`/`manifest-file`.
- `release-please-config.json`: `{"packages": {".": {"release-type": "python", "package-name": "dyness-b3-bms-mqtt", "changelog-path": "CHANGELOG.md", "include-v-in-tag": true, "bump-minor-pre-major": true, "pull-request-title-pattern": "build${scope}: release${component} ${version}"}}}`; manifest `{".": "0.1.0"}`.
- Makefile: `SHELL := /bin/bash`, `.SHELLFLAGS := -eu -o pipefail -c`, `CHECKOUT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))`, `UV ?= uv`; `dev` runs `uv sync --all-extras`; `check` runs the four gates.

**Tests.** `test_python_is_pinned_to_314`, `test_runtime_dependencies_are_exact`, `test_console_script_and_build_backend`, `test_version_is_single_sourced` (no literal version in `__init__`), `test_release_please_config`, `test_markers_registered_and_strict`, `test_make_check_runs_the_four_gates_in_order`, `test_ci_workflow` (both jobs, triggers, no vcan/sudo), `test_fake_clock`; pytester: `test_sleep_raises_outside_integration`, `test_integration_may_sleep_and_gets_timeout` (`(10.0,)`), `test_hardware_skipped_without_flag` (reason "--hardware not given"), `test_mosquitto_skipped_when_broker_unreachable` (reason "broker unreachable at host:port").

**Commit:** `build: scaffold uv project, test markers, CI and release-please`

### Task 2: Requirement traceability plugin

**Files.** Create `tests/plugins/reqtrace.py`; modify `tests/conftest.py` (`pytest_plugins = ["pytester", "tests.plugins.markers", "tests.plugins.reqtrace"]`). Test `tests/harness/test_reqtrace.py`.

**Requirements:** TEST-05..10.

**Produces.** `ID_PATTERN`; `Requirement(ident, spec, status, covered_by)`; `known_prefixes(spec_dir)` from the 00 §4.1 table; `classify(text) -> "withdrawn"|"manual"|"must"|"should"`; `parse_specs(spec_dir) -> (dict[str, Requirement], duplicates)`; `ReqTrace(config)` registered as `reqtrace-session`; options `--req-strict`, `--req-report`, `--req-specs PATH`; summary line `reqtrace: N must / N covered / N manual / N should`.

**Interpretations.** A requirement's text ends at the next identifier, heading, table row, blank line or fence; code spans and fenced blocks never define. A run is "narrowed" (rule 1 downgrades to warning) when `args_source == ARGS`, `-k`, `-m`, `--deselect`, `--lf`, stepwise, or any deselection. `iter_markers` so module/class marks count. Hardware and mosquitto-marked tests do not count as coverage.

**Tests** (pytester, throwaway spec tree with prefix DEMO): `test_parse_classifies_definitions`, `test_real_specs_parse_without_duplicates` (PROTO-01 must, CAN-17 manual, MQTT-22 should), `test_covered_run_passes_with_summary`, `test_uncovered_must_fails_when_strict`, `test_uncovered_must_only_warns_without_strict`, `test_narrowed_run_downgrades_rule_1` (path, -k, -m, --deselect), `test_bad_references_fail_even_when_narrowed` (unknown, withdrawn, malformed), `test_duplicate_definition_fails`, `test_hardware_tests_do_not_cover`, `test_failing_test_still_covers_and_module_marks_count`, `test_report_prints_matrix_and_manual_list`.

**Commit:** `test: add requirement traceability pytest plugin`

### Task 3: Configuration

**Files.** Create `src/dyness_b3_bms_mqtt/{config,logsetup}.py`, `tests/support/settings.py`, `tests/unit/__init__.py`. Tests `tests/unit/test_config.py`, `tests/unit/test_logsetup.py`.

**Requirements:** CFG-01..07, 09, 10, 12; SVC-22 (format).

**Produces.**
- `EX_CONFIG = 78`, `REDACTED`, `SECRETS = {"MQTT_PASSWORD"}`, `INTERFACES`, `LOG_LEVELS`, `DEFAULTS: Mapping[str, str | None]` (46 variables in spec order; `None` = unset; `MQTT_CLIENT_ID` computed at load), `VARIABLES`.
- `ConfigError(errors: list[tuple[name, shown_value, reason]])`.
- `Settings` (frozen, slots), one lowercase field per variable: `can_interface, can_channel, can_bitrate, can_restart_ms, can_listen_only, can_usb_id, module_count, module_names: Mapping[int, str]`; `poll_stagger_s, poll_retry_s, poll_max_retries, poll_backoff_s, start_grace_s, cell_stale_s, module_stale_s, bus_silence_s`; `bus_reopen_attempts, recovery_reset_s, dead_after, dead_retry_s, dead_usb_reset_s, error_suspend_count, error_window_s`; `mqtt_host, mqtt_port, mqtt_username, mqtt_password, mqtt_client_id, mqtt_tls, mqtt_ca_file, mqtt_keepalive_s, mqtt_base_topic, publish_interval_s`; `ha_discovery, ha_discovery_prefix, ha_node_id, ha_device_name, expire_after_s` (resolved: `ceil(3*PUBLISH_INTERVAL_S+5)` when 0); `alert_cell_spread_mv, alert_cell_low_mv, alert_current_imbalance_pct, alert_imbalance_min_current_a, alert_debounce_s, alert_hysteresis_pct, alert_cell_low_hysteresis_mv`; `log_level`. Method `module_name(m) -> str`.
- `load_settings(environ, *, hostname=socket.gethostname, readable=_readable) -> Settings`; `describe(settings) -> list[str]`; `check_config(environ) -> int`.
- `configure_logging(level, stream=None)`: `"%(levelname)s %(message)s"`, newlines → `" | "`, replaces only its own tagged handler (caplog keeps working).
- `tests.support.settings.make_settings(**env) -> Settings` = `load_settings({"CAN_INTERFACE": "virtual", "CAN_CHANNEL": "test", **env}, hostname=lambda: "testhost")`.

**Interpretations.** Integers match `-?[0-9]+`; decimals `-?(\d+(\.\d*)?|\.\d+)`; errors collected, never first-fail; default client id sanitises short hostname; gs_usb channel must be a decimal integer; durations spec'd only as "> 0" use `lo=1e-9`.

**Tests.** `test_defaults_apply_when_unset_or_empty`, `test_variable_names_and_defaults_match_spec_table` (parses 08 tables), `test_range_limits` (both limits and just outside, parametrized), `test_integers_are_strict_ascii`, `test_durations_are_finite_plain_decimals`, `test_boolean_spellings`, `test_bad_booleans`, `test_invalid_values`, `test_valid_specials`, `test_cross_variable_rules`, `test_all_failures_are_reported_together`, `test_describe_redacts_password`, `test_check_config_logs_and_returns_code`, `test_reads_no_files` (monkeypatch `builtins.open`), `test_one_line_level_first_no_timestamp`.

**Commit:** `feat(config): parse and validate environment settings`

### Task 4: Protocol decoder, poll guard, vectors

**Files.** Create `src/dyness_b3_bms_mqtt/protocol.py`, `tests/vectors/{__init__,loader}.py`, vector files. Tests `tests/unit/test_protocol.py`, `tests/unit/test_vectors.py`.

**Requirements:** PROTO-01..28, 29 (grid), 32, 33; TEST-14..17, TEST-28 (property).

**Produces.**
- Constants `MODULE_ID_BASE=0x18F21000`, `MODULE_ID_MASK=0xFFFFF000`, `POLL_ID_MASK=0xFFFFF0FF`, `POLL_ID_PATTERN=0x18F21077`, `POLL_DATA`, `PARAMETER_WRITE_ID=0x18F26677`, `HEARTBEAT_ID=0x18F20103`, `VALID_POLL_IDS`, `CELL_CODES=(0x11..0x15)`, `ENERGY_CODE=0x17`, `BROADCAST_CODES=(0x16,0x21,0x22,0x23,0x24)`, `MODULE_CODES`, `STACK_IDS`, `IGNORED_IDS`, `CELLS_PER_MODULE=15`, `FRAME_LENGTH=8`.
- `FrameClass` {ECHO, MODULE, STACK, HEARTBEAT, IGNORED, MALFORMED, UNKNOWN}; `BUS_ACTIVITY` = all but ECHO.
- `FlagCategory` (StrEnum) {PROTECTION, ALARM, STATUS, FAULT}; `FLAG_TABLE` 27 rows `(name, byte, bit, category)` in canonical order; `FLAG_NAMES`; `FLAG_CATEGORY`.
- Module frames (frozen, slots), base `ModuleFrame(module, code)`: `CellVoltages(cells_mv: Mapping[int,int])`, `CellTemperatures(temps_c: tuple[float,float,float])`, `MosBoardCycles(mos_c, board_c, cycles)`, `EnergyThroughput(energy_kwh)`, `PackStatus(pack_v, current_raw_a, avg_cell_mv, max_discharge_a)`, `CellExtremes(cell_max_mv, cell_max_no: int|None, cell_min_mv, cell_min_no: int|None, max_charge_a)`, `TempExtremes(temp_max_c, temp_max_sensor_raw, temp_min_c, temp_min_sensor_raw, soh, soc)`, `StatusFlags(flags: Mapping[str,bool], flags_raw: str)`.
- Stack frames, base `StackFrame(can_id)`: `ChargeLimits(chg_v, chg_a, dis_a, dis_v)`, `StackSocSoh(soc, soh)`, `StackMeasurements(voltage_v, current_a, temp_c)`, `StackModuleCount(module_count, raw_hex)`, `ChargeRequest(request_raw, charge_enable, discharge_enable)`, `StackCapacity(capacity_ah)`, `StackWords(words)`.
- `classify(can_id, extended, data) -> FrameClass`; `decode(...) -> ModuleFrame | StackFrame | None`; `FrameCounters(echo_frames, malformed_frames, unknown_frames).count(cls)`; `is_poll(can_id, extended, data)`; `PollFrame(arbitration_id, data)` validating in `__post_init__`; `build_poll(module, module_count) -> PollFrame`.
- `loader.VECTORS`, `VectorLine(lineno, time, can_id, extended, data, key)`, `TxLine(lineno, time, can_id, data)`, `load(path) -> (header, lines)`.

**Vectors** (vendored; header `# source: dyness-debug/data/2026-10-08/<file>` and `# lines:`): `t1-examples` (28 frames from t1 lines 1–47), `t2-examples` (21 frames + 3 TX from t2 lines 1–86), `synthetic` (33 frames all dropped: 6 MALFORMED, 2 ECHO, 1 HEARTBEAT, 24 UNKNOWN), `edge` (10 decode edge cases: 0x22 raw 15/255 → None, −0.1/−40.0 °C, all flags, unnamed bits, raw 4000 → 0.0, T4 value, non-zero slot 16, negative 0x356 current/temp, negative 0x351 current, SOC/SOH >100 raw), `captures/` full t1/t2 with class counts (t1: MODULE 1501, STACK 420, IGNORED 360, HEARTBEAT 600; t2: MODULE 2579, ECHO 194, TX 194, rest as t1). `.expected.json` hand-written from `01-protocol.md` §7, keyed `"<ID> <X|S> <bytes>"`. Worked example: 0x21 `13 6e 0f 6f 0c f4 01 77` → 49.74 V, −4.9 A raw, 3316 mV, 37.5 A.

**Tests.** `test_every_vector_matches_expected` (t1, t2, edge, synthetic), `test_synthetic_frames_are_dropped_and_counted` (6/2/24), `test_each_echo_follows_its_tx`, `test_vectors_cover_every_frame_type_for_modules_1_to_3`, `test_capture_replay_has_no_malformed_or_unknown`, `test_tests_never_reference_dyness_debug_at_runtime` (AST), `test_classify_and_decode_never_raise` (hypothesis), `test_module_range_never_raises`, `test_identifier_and_extended_flag_both_matter`, `test_bus_activity_excludes_only_echo`, `test_module_4_decodes_without_module_count`, `test_worked_example_details`, `test_flag_table_and_categories`, `test_stack_frames_need_no_reinterpretation`, `test_build_poll_exhaustive_grid` (−1..20 × −1..6 plus bool/float/str/None/0x56), `test_parameter_write_frame_is_unconstructible`, `test_guard_is_not_an_assert` (AST), `test_build_poll_property` (hypothesis).

**Commit:** `feat(protocol): classify and decode B3 frames with guarded poll builder`

### Task 5: Telemetry

**Files.** Create `src/dyness_b3_bms_mqtt/{status,telemetry}.py`. Test `tests/unit/test_telemetry.py`.

**Requirements:** TEL-01..26; TEST-42 (should).

**Produces.**
- `status.ModuleStatus` {OK="ok", CELLS_STALE="cells_stale", SILENT="silent"}; `status.HealthState` {STARTING, HEALTHY, DEGRADED, STALE, RECOVERING, DEAD} (lower-case values); `RUNNING_STATES = {STARTING, HEALTHY, DEGRADED}`.
- `ModuleSnapshot` (frozen) in order: `module, online, cells_fresh, bcast_age_s, cell_age_s, cells_mv (15-tuple), cell_max_mv, cell_max_no, cell_min_mv, cell_min_no, cell_avg_mv, spread_mv, pack_v, current_a, soc, soh, cycles, energy_kwh, chg_limit_a, dis_limit_a, temps_c (3-tuple), temp_mos_c, temp_board_c, temp_max_c, temp_min_c, flags, flags_raw, chg_mos_on, dis_mos_on, balancing`.
- `StackSnapshot`: `lowest_mv, lowest_module, lowest_cell, highest_mv, highest_module, highest_cell, spread_mv, total_current_a, bms_current_a, avg_soc, bms_soc, bms_soh, voltage_v, temp_c, chg_limit_v, chg_limit_a, dis_limit_a, dis_limit_v, module_count_bms, modules_online, any_alarm, any_protection`.
- `Snapshot(modules, stack)`; `TelemetryStore(settings)` with `.ingest(frame, at)` and `.snapshot(now, status: Mapping[int, ModuleStatus]) -> Snapshot`.

**Interpretations.** `cell_age_s` = oldest received cell code, `bcast_age_s` = newest broadcast, both None until first frame; a 0 mV cell warns once per occurrence.

**Tests.** `test_snapshot_shape`, `test_module_fields_from_vectors` (module 1: spread 24, current +4.9), `test_stale_values_become_none_at_the_boundary` (10.0 fresh / 10.1 stale), `test_offline_module_nulls_everything_but_identity_and_ages`, `test_cells_stale_status_keeps_module_online`, `test_stack_derived_values` (lowest 3286 m2 c8, highest 3326 m3 c11, spread 40, total 13.1 = bms 13.1), `test_ties_resolve_to_lowest_module_then_cell`, `test_no_candidates_gives_none`, `test_avg_soc_uses_builtin_round`, `test_raw_rollups_count_cut_only_for_protection`, `test_negative_zero_is_published_as_zero`, `test_zero_cells_and_minus_40_temperatures`, `test_out_of_range_numbers_and_percentages`, `test_ignores_modules_above_count_and_keeps_no_history`, `test_deterministic`, `test_ten_thousand_frames_under_one_second`.

**Commit:** `feat(telemetry): latest-value store with freshness and derived stack values`

### Task 6: Alerts

**Files.** Create `src/dyness_b3_bms_mqtt/alerts.py`. Test `tests/unit/test_alerts.py`.

**Requirements:** ALERT-01, 02, 04–15, 17 (format), 18 (ALERT-03 and 16 complete in Task 12).

**Produces.** `MODULE_ALERTS = ("cell_spread","cell_low","bms_alarm","bms_protection")`; `STACK_ALERTS = ("current_imbalance","any_cell_spread","any_cell_low","any_bms_alarm","any_bms_protection")`; `ROLLUPS`, `ALARM_FLAGS`, `PROTECTION_FLAGS` (cut + fault); `Scope = int | Literal["stack"]`; `AlertThresholds(cell_spread_mv, cell_low_mv, current_imbalance_pct, imbalance_min_current_a, debounce_s, hysteresis_pct, cell_low_hysteresis_mv).from_settings(s)`; `Alert(state, pending_since, value, flags)`; `AlertState(modules, stack).initial(n)`, `.module(m)`; `AlertChange(scope, key, old, new, value, flags, state_changed, flags_changed)`; `evaluate(snapshot, previous, now, thresholds) -> AlertState`; `changes(previous, current) -> list[AlertChange]`; `imbalance_pct(currents, min_current) -> (known, pct|None)`; `format_change(change, thresholds) -> str`.

**Tests.** `test_initial_state_is_unknown`, `test_module_is_pure` (AST), `test_unknown_takes_instant_result`, `test_short_condition_never_fires_held_condition_fires` (30.9 vs 31 s, dip cancels), `test_recovery_inside_band_does_not_clear` (45 < x ≤ 50), `test_stale_input_gives_unknown_and_cancels_pending`, `test_zero_debounce_is_immediate`, `test_cell_low_sources_and_absolute_margin` (3049/3050 boundary), `test_imbalance_formula_and_gating` (66.7 %, gating, N=1 → 0), `test_imbalance_alert_timeline`, `test_flag_alerts_follow_flags_directly`, `test_rollups`, `test_changes_report_state_and_flag_changes_not_values`, `test_change_log_format` (exact ALERT-17 strings), `test_thresholds_come_from_settings`.

**Commit:** `feat(alerts): debounced hysteresis alerts with flag roll-ups`

### Task 7: Health machine and state store

**Files.** Create `src/dyness_b3_bms_mqtt/{timefmt,state_store,health}.py`. Tests `tests/unit/test_state_store.py`, `tests/unit/test_health.py`.

**Requirements:** HEALTH-01, 02, 04–20, 22–26; PROTO-31 (machine half); SVC-03, SVC-04.

**Produces.**
- `iso_utc(wall) -> str` (seconds truncated, `Z`); `RECOVERY_FILE="recovery.json"`, `LAST_EXIT_FILE="last_exit.json"`; `RecoveryRecord(escalation=0, restarts=0)`; `LastExit(code: int|str|None, result: str|None)`; `StateStore(directory, wall)` with `.from_environ(environ, wall)`, `.load_recovery()`, `.save_recovery(r)` (atomic, swallows OSError with a warning), `.load_last_exit()`.
- Actions `Transition(old, new, reason)`, `Poll(module)`, `SetAvailability(module, online)`, `ReopenBus()`, `SuspendPolling(reason)`, `SetEscalation(value)`, `Exit(code, reason)`; `Action` union; `ACTION_ORDER = (Transition, SetAvailability, SetEscalation, SuspendPolling, Poll, ReopenBus, Exit)`; `REASON_CODES`; `EXIT_LINK_BOUNCE=75`, `EXIT_USB_RESET=76`.
- `HealthMachine(settings, escalation)`: inputs `start(now, open_ok, detail)`, `on_frame(arbitration_id, now)`, `on_error_frame(now)`, `on_bus_exception(now, detail)`, `on_bus_open(now, ok, detail)`, `step(now) -> list[Action]`; accessors `state`, `reason`, `escalation`, `polling_suspended`, `module_status(m)`, `published_statuses() -> dict[int, ModuleStatus]`, `bus_age(now)`.

**Interpretations.** Ages before the first frame measure from the last successful open. STARTING's "all OK" also needs a received broadcast and, unless listen-only, all five cell codes. A module is online only after a broadcast and while not SILENT; `published_statuses` reports SILENT for offline modules. Offline availability is suppressed during grace. Timers fire at `>=`; staleness compares `>`. A failed reopen counts when emitted. Reasons: `all_ok: N/N modules ok`, `modules_not_ok: m2 cells_stale, m3 silent`, `escalation_limit: 3 >= DEAD_AFTER 3`. After `Exit`, `step` returns `[]`. The machine imports no time/os/socket/threading/logging/subprocess.

**Tests.** State store: `test_iso_utc_floors_to_seconds`, `test_round_trip_and_atomic_write`, `test_invalid_recovery_counts_as_zero` (parametrized), `test_memory_only_without_state_directory`, `test_last_exit`. Health (driven by a `Rig` with 0.5 s grid): `test_machine_is_pure`, `test_start_and_staggered_polls` (0.29 vs 0.3), `test_listen_only_never_polls_and_ignores_cells`, `test_starting_to_healthy_needs_real_frames`, `test_grace_then_degraded` (19.5 vs 20.0), `test_cells_stale_ladder_and_reset` (t0, +5, +10, +70), `test_cell_age_boundary`, `test_silent_module_backoff_never_escalates`, `test_bus_silence_boundary_then_stale` (15.0/15.5, action order), `test_reopen_ladder_then_recovering` (escalation 0 → 75, 1 → 76), `test_zero_reopen_attempts_goes_straight_to_recovering`, `test_frame_after_reopen_restarts`, `test_bus_exception`, `test_dead_mode` (ReopenBus at 60 not 59.5, exit 76 at 900, no SetEscalation), `test_dead_to_starting_on_frame`, `test_escalation_reset_after_unbroken_healthy`, `test_error_frames_suspend_polling_only`, `test_error_window_slides` (asserts `not polling_suspended`), `test_every_reason_uses_a_known_code`.

**Commit:** `feat(health): pure health state machine and recovery state store`

### Task 8: CAN transport, link state, fakes, simulator

**Files.** Create `src/dyness_b3_bms_mqtt/{linkstate,canbus}.py`, `tests/fakes/{transport,flaky}.py`, `tests/sim/{__init__,encode,stack,paced}.py`; modify `tests/conftest.py` (fixtures `can_channel`, `state_dir`, autouse `tx_guard`). Tests `tests/unit/{test_linkstate,test_canbus,test_static_tx}.py`, `tests/harness/{test_sim,test_fakes}.py`.

**Requirements:** CAN-01..16, 18..23, 26..30, 32..34; CFG-13; PROTO-30; TEST-13, 21..26, TEST-28 (guard fixture).

**Produces.**
- linkstate: `CanState` {ERROR_ACTIVE="error-active", ERROR_WARNING, ERROR_PASSIVE, BUS_OFF, STOPPED, DOWN, ABSENT, UNKNOWN} with `.published -> str|None`; `LinkState(state, operstate, listen_only, bitrate, berr_tx, berr_rx, restarts, bus_off, rx_dropped, at)`; `unknown_link_state(at)`; `parse_ip_link(stdout, at)`; `query_link_state(channel, at, *, runner=subprocess.run, which=shutil.which, environ=os.environ)`.
- canbus: `RECV_TIMEOUT_S=0.2`, `REOPEN_GAP_S=2.0`, `SEND_TIMEOUT_S=0.5`, `ACCEPT_FILTERS`; `FaultKind` (StrEnum) {DEVICE_GONE, LINK_DOWN, TX_BLOCKED, OS_ERROR, OPEN_FAILED, CAN_ERROR, LISTEN_ONLY_REFUSED, NOT_OPEN}; `TransportFault(op, kind, errno, message)`; `ListenOnlyError(RuntimeError)`; `RxFrame(arbitration_id, extended, data, at)`, `RxErrorFrame(error_class, data, at)`; `CanTransport` Protocol per CAN-01; `BusLike` Protocol (`recv`, `send`, `shutdown`); `accepts(arbitration_id, extended)`; `bus_kwargs(settings)`; `map_exception(op, exc) -> TransportFault|None`; `PythonCanTransport(settings, clock, sleep, *, bus_factory=None, link_query=None, platform=sys.platform)`.
- Fakes: `FakeTransport(listen_only=False, module_count=3)` with `.script(items)`, `.polls`, `.fail_open(n, fault)`, `.link`; `RecordingTransport(inner)`; `FlakyBus(inner, timeline=None)` with `.fail_recv(errno, once=True)`, `.fail_send(...)`, `.clear()`, `.shutdown_called`; `FlakyFactory(make_inner)` with `.fail_next_opens(n, errno=ENODEV)`, `.opened`, `.kwargs`, `.all_shut_down()`.
- Simulator: `SimulatedStack(channel, clock, module_count=3, *, echo_polls=False)` with `.bus`, `.received`, `.advance(dt)`, `.advance_until_frame(limit)`, `.schedule(at, fn)`, knobs `silence_module/resume_module/silence_bus/resume_bus/stop_cell_stream/inject_error_frames/set_cells/set_flags/set_current/set_soc`, `.close()`; defaults M2 #8 and M3 #9 about 25 mV low; encodes with `tests/sim/encode.py`, never importing `protocol`. `SimPacedBus(inner, sim)`, `paced_factory(sim, wrap=identity)`.

**Interpretations.** Software filtering on every interface; kernel filters only on socketcan; `CAN_RAW_ERR_FILTER=CAN_ERR_MASK` always set on `bus.socket`. Missing `ctrlmode` with `info_data` present → `listen_only=False`. `rx_dropped` from `stats64.rx.dropped`. The only `can.Message(` construction and `.send(` call is inside `send_poll`, from a `PollFrame`, via `protocol.build_poll` looked up as a module attribute. `CanOperationError` "buffer full"/"timed out" → TX_BLOCKED. `close` logs `INFO can bus shut down`. Simulator drops missed ticks during gaps; error frames use `is_error_frame` class 0x004. `SimPacedBus.recv` never really waits.

**Tests.** Linkstate: `test_each_state_from_fixture`, `test_missing_ctrlmode_means_not_listen_only`, `test_mistyped_fields_become_none_individually`, `test_absent_and_unknown_command_outcomes`, `test_command_is_argument_list_with_lc_all_and_timeout`, `test_non_socketcan_returns_unknown_without_command`. Transport: `test_bus_kwargs_per_interface`, `test_gs_usb_darwin_stub_only_on_darwin`, `test_gs_usb_listen_only_wraps_start_and_restores`, `test_gs_usb_listen_only_refused`, `test_open_failure_maps_and_shuts_partial_bus`, `test_open_logs_info`, `test_socketcan_listen_only_mismatch_warns`, `test_slcan_listen_only_warns`, `test_recv_filters_without_ending_early_and_within_50ms`, `test_error_frames_returned_as_rx_error`, `test_socketcan_error_filter_set`, `test_recv_timeout_returns_none_quietly`, `test_send_poll_listen_only_raises_without_touching_bus`, `test_send_poll_builds_via_build_poll_and_rechecks`, `test_send_timeout_bounded`, `test_exception_mapping_matrix`, `test_not_open_and_close_idempotent`, `test_shutdown_exception_swallowed`, `test_usable_after_fault`, `test_reopen_closes_sleeps_2s_opens`, `test_contract_virtual_and_fake`. Static: `test_only_send_poll_constructs_or_sends` (AST over src). Harness: `test_sim_broadcast_rates_and_cell_stream_after_poll`, `test_sim_records_and_echoes`, `test_sim_knobs`, `test_sim_defaults_weak_cells`, `test_sim_does_not_import_protocol`, `test_flaky_bus`, `test_unique_channels_and_state_dirs`, `test_tx_guard_rejects_bad_frames`.

**Commit:** `feat(canbus): python-can transport, link-state query and simulated stack`

### Task 9: sd_notify

**Files.** Create `src/dyness_b3_bms_mqtt/sdnotify.py`, `tests/fakes/notify.py`; modify conftest (fixture `notify_socket`). Test `tests/unit/test_sdnotify.py`.

**Requirements:** SVC-05, 07 (cadence), 09 (send); TEST-20.

**Produces.** `Notifier(environ, pid)` with `.notify(message)`, `.ready()` (once), `.status(text)`, `.stopping()`, `.watchdog(now)` (at most every WATCHDOG_USEC/3, only when WATCHDOG_PID unset or equal to pid); no-ops without `NOTIFY_SOCKET`; abstract namespace supported. `NotifySocket()` with `.path`, `.messages()`, `.close()`.

**Tests.** `test_noop_without_socket`, `test_datagrams_received`, `test_abstract_namespace`, `test_send_failure_logged_debug_not_raised`, `test_ready_once`, `test_watchdog_cadence_and_pid`, `test_no_watchdog_without_usec`.

**Commit:** `feat(sdnotify): dependency-free sd_notify with watchdog cadence`

### Task 10: Home Assistant discovery

**Files.** Create `src/dyness_b3_bms_mqtt/discovery.py`, `tests/plugins/golden.py`, `tests/golden/discovery/{default,named}/*.json`; modify conftest plugins. Tests `tests/unit/test_discovery.py`, `tests/harness/test_golden.py`.

**Requirements:** HA-02..17, 21, 22; TEST-31, 32.

**Produces.** `SUPPORT_URL`; `Component(key, name, platform, device_class, unit, state_class, category, precision, template: "S"|"B"|"B+", enabled=True, options=None, service=False)`; `MODULE_COMPONENTS`, `STACK_COMPONENTS`, `SERVICE_COMPONENTS`, `HEALTH_OPTIONS`, `CAN_STATE_OPTIONS`; `REMOVED_COMPONENTS` (empty); `build_discovery(settings, version) -> list[tuple[topic, payload]]` (stack first, modules ascending, then `""` for modules MODULE_COUNT+1..4; `[]` when discovery off). `golden(name, actual)` fixture and `--update-golden`.

**Interpretations.** Full HA key names, never abbreviations. `availability` at the device-config root (HA-06 UNVERIFIED). Service components override `state_topic`.

**Tests.** `test_discovery_matches_golden` (default, named), `test_example_components_match_spec`, `test_devices_and_order`, `test_origin_and_shared_options`, `test_unique_ids_and_no_object_id`, `test_names_sentence_case_unpadded`, `test_binary_templates_render_on_off_none`, `test_expire_after_everywhere`, `test_enum_options`, `test_bms_flag_attributes`, `test_every_payload_key_exposed_except_7_5_1`, `test_removals_for_surplus_modules`, `test_disabled_publishes_nothing`, `test_removed_components_list_rendered`; golden harness `test_missing_golden_fails`, `test_differing_golden_fails`, `test_update_golden_writes`.

**Commit:** `feat(discovery): device-based Home Assistant discovery builder`

### Task 11: MQTT publisher and payloads

**Files.** Create `src/dyness_b3_bms_mqtt/{payloads,mqtt}.py`, `tests/fakes/publisher.py`, `tests/golden/state/*`, `tests/integration/test_mosquitto.py`. Tests `tests/unit/{test_payloads,test_mqtt}.py`, `tests/harness/test_suite_shape.py` (TEST-37 part).

**Requirements:** MQTT-01..08, 10, 11, 13..18, 21 (publisher), 23; TEST-18, 31 (state), 37; TEST-35 (should).

**Produces.** payloads: `MODULE_KEYS`, `STACK_KEYS`, `SERVICE_KEYS` (exact MQTT-16/17/18 sets); `encode(obj) -> str` (sorted, compact, `ensure_ascii=False`, `allow_nan=False`); `module_payload(ms, alerts, name, ts)`, `stack_payload(ss, alerts, ts)` (imbalance_pct 1 dp), `ServiceInfo(version, health, health_reason, can_state, can_berr_tx, can_berr_rx, can_restarts, bus_age_s, error_frames, dropped_frames, echo_frames, malformed_frames, unknown_frames, polls_sent, polling_suspended, escalation, service_restarts, last_exit_code, last_exit_result, uptime_s, mqtt_reconnects)`, `service_payload(info, ts)`. mqtt: `MqttEvent` {CONNECTED, HA_ONLINE}; `Publisher` Protocol per MQTT-23 (`start`, `stop`, `connected`, `take_events`, `subscribe`, `publish(topic, payload, *, qos, retain)`); `Topics(settings)` with `.status`, `.module_state(m)`, `.module_availability(m)`, `.stack_state`, `.service_state`, `.ha_status`, `.discovery_stack`, `.discovery_module(m)`; `is_ha_online(topic, payload, ha_status_topic)`; `PahoPublisher(settings, *, client_factory=...)`, `MAX_QUEUED=16`; `FakePublisher(...)` with `.published`, `.dropped`, `.subscriptions`, `.started`, `.stopped`, `.set_connected(v, fire=True)`, `.deliver(topic, payload)`, `.explode_on_take_events`, `.last_json(topic)`; `Published(topic, payload, qos, retain)`.

**Interpretations.** paho callbacks only set a `threading.Event`, record events under a lock; `take_events()` logs on the main thread. `publish()` swallows paho errors and is a no-op while disconnected. `stop()` publishes offline, `wait_for_publish(2.0)`, disconnects, `loop_stop`. Keepalive `ceil(MQTT_KEEPALIVE_S)`. MQTT 3.1.1, clean session.

**Tests.** Payloads: `test_module_keys_exact`, `test_stack_keys_exact`, `test_service_keys_exact`, `test_encoding_rules`, `test_ts_format`, `test_state_golden`. MQTT: `test_client_construction_v2_mqtt311_clean_session`, `test_credentials_and_no_password_logged`, `test_tls_with_and_without_ca`, `test_connect_async_reconnect_delay_loop_start`, `test_callbacks_only_set_events`, `test_failures_never_raise`, `test_will_set_before_connect`, `test_stop_offline_bounded_wait`, `test_publish_noop_when_disconnected`, `test_ha_online_exact_payload`, `test_topics`, `test_fake_publisher_records`. Mosquitto (marked `mosquitto` + `integration`): `test_retained_discovery_and_availability`, `test_lwt_on_abrupt_close`, `test_explicit_offline_on_stop`, `test_reconnect_after_drop`. `test_mosquitto_suite_shape` (AST, default run).

**Commit:** `feat(mqtt): paho publisher, topics and state payload builders`

### Task 12: Application wiring and main loop

**Files.** Create `src/dyness_b3_bms_mqtt/{app,__main__}.py`, `tests/integration/{test_signals,test_realtime}.py`, `tests/hardware/test_stack.py`. Tests `tests/unit/test_app.py`, `tests/harness/test_suite_shape.py` (TEST-38 part).

**Requirements:** CAN-12 (loop), 24, 25, 31; HEALTH-03, 21, 27–33, 34 (should); PROTO-05 (app), 29 (app), 31; ALERT-03, 16, 17; MQTT-06, 07, 09, 12, 19, 20, 21; HA-01, 18, 19, 20; CFG-03 (no bus/broker), 11; SVC-06, 07, 08, 09, 11, 12, 13; TEL-06; TEST-12, 29, 30 (should), 38.

**Produces.** `LINK_STATE_INTERVAL_S=10.0`, `ALERT_INTERVAL_S=0.5`, `HA_JITTER_MAX_S=5.0`; `Runtime(clock, wall, sleep, random_uniform, environ, pid, transport_factory, publisher_factory, install_signals=True)` with `.production()`; `App(settings, runtime)` with `.run() -> int`, `.request_stop(signum=None, frame=None)`; `run(environ, runtime) -> int`; `main(argv=None) -> int` handling `--version`, `--check-config`, unknown argument → 78.

**Interpretations.** MALFORMED frames reported as `on_frame(HEARTBEAT_ID)`. After a bus fault the app closes the transport and idles 0.2 s via injected sleep. Never `publish` while disconnected. Clean stop: `STOPPING=1` → final service state → `transport.close()` → `publisher.stop()`. Signal handlers set a flag then SIG_IGN during shutdown. `STATUS=` summary is the reason detail, e.g. `HEALTHY: 3/3 modules ok`. `service_restarts` increments at start when `last_exit.json` exists with code ≠ 0 (null included). Telemetry status from `health.published_statuses()`. Alerts evaluated every ≥ 0.5 s. Immediate publishes never reset the periodic schedule. HA republish scheduled at `now + random_uniform(0, 5)`, at most one pending.

**Tests** (SimulatedStack, FlakyFactory(paced_factory), FakePublisher, FakeClock, tmp STATE_DIRECTORY): `test_sigterm_clean_stop_exit_0`, `test_bus_silence_exit_75_then_76`, `test_dead_mode_waits_for_periodic_usb_reset`, `test_invalid_config_exit_78_without_bus_or_broker`, `test_unexpected_exception_exit_1_with_traceback`, `test_adapter_absent_starts_dead`, `test_every_transmit_is_a_valid_poll`, `test_listen_only_never_calls_build_poll`, `test_malformed_frames_keep_bus_alive_not_module`, `test_recv_fault_does_not_spin`, `test_broker_down_does_not_affect_health_or_watchdog`, `test_connect_sequence_order`, `test_reconnect_republishes_and_counts`, `test_ha_online_jittered_single_republish`, `test_discovery_disabled`, `test_surplus_module_removals`, `test_nothing_published_while_disconnected`, `test_periodic_and_immediate_publishes`, `test_alerts_evaluated_independently_of_publish_interval`, `test_info_logs`, `test_sd_notify_sequence`, `test_recv_timeout_constant`, `test_link_state_every_10s_change_logged`, `test_availability_offline_during_stale`, `test_final_state_before_exit`, `test_main_flags`. Integration: `test_signal_clean_stop` (SIGTERM/SIGINT/SIGHUP, subprocess on virtual), `test_realtime_cells_published`. Hardware: `tests/hardware/test_stack.py` listen-only unless `--hardware-poll`; `test_hardware_suite_shape` (AST).

**Commit:** `feat(app): main loop wiring, signals, exit codes and publish cadence`

### Task 12b: Local history logging

**Files.** Create `src/dyness_b3_bms_mqtt/localog.py`; modify `app.py` (wire writer and housekeeping into the loop; `local_log` in `ServiceInfo`), `payloads.py` (`SERVICE_KEYS` + `local_log`), `discovery.py` (diagnostic enum sensor). Tests `tests/unit/test_localog.py`, additions to `tests/unit/test_app.py`, golden updates.

**Requirements:** LOG-* (all, from Task 0), the amended MQTT-18 and HA entity row.

**Produces.** `MODULE_COLUMNS`, `STACK_COLUMNS` (tuples, identical names to the JSON keys); `LocalLogStatus` {OK="ok", DISABLED="disabled", ERROR="error"}; `LocalLog(settings, directory: Path | None, wall, *, gzip_open=gzip.open)` with `.write(snapshot, alerts, ts)`, `.event(ts, line)`, `.housekeep(now_wall)`, `.status`; `day_name(prefix, wall) -> str`; `housekeeping_plan(directory, now_wall, compress_after_days, retention_days) -> (to_gzip, to_delete)` as a pure function.

**Interpretations.** CSV via `csv.writer` with `\n` line endings; header written only when creating a file; `-0.0` written as `0.0`; files rolled on UTC date change; gzip in place with the original removed only after a successful write; housekeeping never touches today's files; every `OSError` caught at the call site, status set to ERROR, one ERROR log per failure episode, writer re-opened at next housekeeping.

**Tests.** `test_columns_match_json_keys`, `test_header_once_and_append`, `test_rows_per_module_and_stack_with_nulls_empty`, `test_day_roll_at_utc_midnight`, `test_events_file_lines`, `test_interval_independent_of_publish_interval` (app), `test_housekeeping_plan_gzip_and_delete_boundaries`, `test_housekeeping_skips_today_and_is_idempotent`, `test_write_oserror_sets_error_status_and_never_raises` (disk full via monkeypatched `open`), `test_recovers_after_housekeeping_reopen`, `test_disabled_without_state_directory_warns_once`, `test_service_payload_local_log_field`, `test_no_fsync` (AST).

**Commit:** `feat(localog): daily CSV history with gzip and retention housekeeping`

### Task 13: Packaging and root helpers

**Files.** Create `packaging/{dyness-b3-bms-mqtt.service,can-up,recover,80-dyness-b3-can.rules,dyness-b3-bms-mqtt.env}`, `tests/packaging/{__init__,helpers}.py`. Tests `tests/packaging/{test_unit_file,test_env_example,test_can_up,test_recover}.py`.

**Requirements:** SVC-01, 14–18, 20, 22; CFG-08; TEST-19 (SVC-02 MANUAL).

**Produces.** Helper inputs: `CAN_*`, `STATE_DIRECTORY`, `EXIT_STATUS`, `SERVICE_RESULT`; test seams `SYSFS_ROOT`, `DEV_ROOT`, `ENV_FILE`. `can-up` settings lines `NAME=$(setting NAME default)` parsed by tests. `recover` writes `last_exit.json` mode 0644 and dispatches 75/76. `tests.packaging.helpers.Sandbox(root, bin, log, env)` and `make_sandbox(tmp_path, fakes=("ip","usbreset","sleep"))`; each fake records argv to `FAKE_LOG` and replays `<name>.stdout/.stderr/.status`.

**Interpretations.** Env file parsed with sed, never sourced; one layer of matching quotes stripped. Interface dirs are `<device>/<basename>:*`. USB reset step 6 waits 16 × 0.5 s for the netdev then runs can-up once. EXIT_STATUS/SERVICE_RESULT sanitised before JSON. Env example has every variable as `#NAME=<default>` under a `# description` line.

**Tests.** `test_unit_file_verbatim`, `test_udev_rule_verbatim`, `test_env_example_lists_every_variable`, `test_can_up_defaults_equal_spec`, `test_can_up_scope_and_absent`, `test_can_up_idempotent_no_ip_set_when_correct`, `test_can_up_configures_in_order`, `test_can_up_listen_only_on_off`, `test_can_up_env_file_not_executed` (metacharacters in MQTT_PASSWORD), `test_can_up_always_exit_0_and_prefix`, `test_recover_writes_last_exit`, `test_recover_75_bounces_link`, `test_recover_76_usb_reset_fallback_order`, `test_recover_fallback_scan_by_usb_id`, `test_recover_device_not_found`, `test_recover_other_codes_and_non_socketcan_do_nothing`, `test_recover_time_budget` (fake sleeps ≤ 10 s), `test_helper_lines_prefixed`.

**Commit:** `feat(packaging): systemd unit, udev rule, env example and root helpers`

### Task 14: Makefile install targets and README

**Files.** Modify `Makefile`, `README.md` (quick start exactly as `10-installation.md` §6). Tests `tests/packaging/{test_makefile,test_readme}.py`.

**Requirements:** INST-01..10, 12 (INST-15, 16 MANUAL).

**Produces.** Variables `DESTDIR ?=`, `UNAME_M`, `OS_RELEASE`, `EUID`, `INSTALL_OWNER ?= -o root -g root`, `PURGE ?= 0`, `UV ?= uv`. Targets `root-check`, `preflight` (aarch64; Debian ID/ID_LIKE; VERSION_ID ≥ 12), `install` (the INST-06 steps with `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_PREFERENCE=only-managed`, `UV_LINK_MODE=copy`, umask 022, `chmod -R a+rX`, env file never overwritten, `check-config` before `restart`), `uninstall` (PURGE rules via `systemctl clean --what=state`), `status`, `logs`, `restart`, `check-config` (`systemd-run -p DynamicUser=yes -p EnvironmentFile=...`), `dev`, `check`.

**Tests.** `test_requires_root`, `test_preflight_rejects_arch_and_old_debian`, `test_install_dry_run_order_and_flags` (`make -n`), `test_make_dash_C_equivalent`, `test_install_into_destdir_with_fakes` (paths, modes, a+rX), `test_reinstall_keeps_env_file_byte_for_byte`, `test_uninstall_on_empty_system_succeeds`, `test_uninstall_purge`, `test_operational_targets`, `test_check_config_uses_systemd_parser`, `test_readme_quick_start_exact`; all skip when GNU make < 3.82.

**Commit:** `feat(install): Makefile install, uninstall and operational targets`

### Task 15: Strict traceability and release readiness

**Files.** Modify `pyproject.toml` (`addopts += ["--req-strict"]`), `README.md` status line ("implemented, awaiting hardware acceptance"); create `docs/acceptance/README.md`. Test `tests/harness/test_suite_shape.py` (TEST-27 matrix: `test_component_matrix_has_req_tests`, `test_req_strict_on_by_default`).

**Verification.** `uv run pytest --req-report` → zero uncovered MUSTs; manual list equals the TEST-39 checklist. `uv lock --check`. `make check` clean.

**Commit:** `test: enforce requirement traceability by default`

---

## Execution

- Subagent-driven development (user's choice): one fresh opus implementer per task with this plan's task text, the spec sections it names, and the Global Constraints; a fresh opus reviewer after each task checks the diff against the listed requirement IDs and the Interfaces block; sonnet runs `make check` where a plain test run is all that is needed. Whole-branch review (opus) before merge.
- Work on branch `feat/implementation` from `main`. Tasks 0–15 (including 12b) land as 17 atomic commits. Merge to `main` with a rebase/fast-forward merge so release-please reads the conventional commits and opens the `0.1.0` release PR.
- CI must be green on the branch before the merge; the `mqtt` job validates LWT/retain/reconnect against a real broker.

## Verification

- Each task: its listed tests pass, `make check` passes, commit made.
- After Task 12: `uv run dyness-b3-bms-mqtt --check-config` with the example env exits 0; running against the simulated stack (`CAN_INTERFACE=virtual`) publishes to a local Mosquitto and HA-style retained discovery can be inspected with `mosquitto_sub -t 'homeassistant/#' -v`.
- After Task 15: `uv run pytest --req-report` shows zero uncovered MUSTs; `gh run list` shows `check` and `mqtt` green; after merge, release-please opens a release PR for `0.1.0`.
- Hardware acceptance (MANUAL, TEST-39): run the 12-step checklist on the Pi and record results in `docs/acceptance/YYYY-MM-DD-hardware-acceptance.md`. This is the first time the Linux kernel `gs_usb` path and the SH-C31A RX-stall question get answered.
