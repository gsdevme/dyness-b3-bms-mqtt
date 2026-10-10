---
name: verify-mqtt
description: Use when verifying a live dyness-b3-bms-mqtt deployment, or a hardware acceptance checklist step, by observing what it publishes on the MQTT broker.
---

# Verify a deployment over MQTT

Takes a timed snapshot of everything the service publishes under its base topic and checks it
against `docs/specs/06-mqtt.md` and `docs/specs/03-health-and-recovery.md`. Read those two
specs before judging anything; the key names below come from MQTT-11, MQTT-16, MQTT-17 and
MQTT-18 and must not be paraphrased or invented.

## 1. Ask for the broker settings

Ask the user for each of these. Do not guess, and do not reuse a hostname from the repo or the
Pi's env file: the host may resolve differently, or not at all, off the LAN.

| Setting | Env var | Default (`08-configuration.md`) |
|---|---|---|
| Broker host or IP | `MQTT_HOST` | none; always ask |
| Port | `MQTT_PORT` | `1883` |
| Username, if any | `MQTT_USERNAME` | unset |
| Password, if any | `MQTT_PASSWORD` | unset |
| TLS yes/no | `MQTT_TLS` | `false` (accepts `1/0`, `true/false`, `yes/no`, `on/off`) |
| Base topic | `MQTT_BASE_TOPIC` | `dyness_b3` |

Never write the credentials to any file (no scratch scripts, no `.env`, no notes). Pass them
as environment variables on the single command that needs them.

## 2. Run the snapshot

From the repo root:

```sh
MQTT_HOST=... MQTT_PORT=... MQTT_USERNAME=... MQTT_PASSWORD=... MQTT_TLS=... MQTT_BASE_TOPIC=... \
  uv run --frozen python -I .claude/skills/verify-mqtt/mqtt_snapshot.py --seconds 30
```

Leave a variable empty or out to use its default. The script prints, per topic, `count=` (messages
in the window), `retained=`, `ts_age=` (now minus the payload's `ts`) and the last payload as
compact sorted JSON. Exit 1 means the broker was unreachable or refused the login (report the
message as-is); exit 3 means nothing was published under the base topic (wrong base topic, or the
service is not connected).

When judging a soak or counters (checklist step 12, or anything about frame counts), run it
twice about 60 s apart and compare.

## 3. Checks

`<B>` is the base topic, `<m>` each module number 1..`MODULE_COUNT`.

| Check | Topic and key | Pass when |
|---|---|---|
| Service online | `<B>/status` | `online`, `retained=True` (MQTT-08, MQTT-09) |
| Health | `<B>/service/state` `health` | `healthy`; anything else (`starting`, `degraded`, `stale`, `recovering`, `dead`) is a fail unless the step expects it. Quote `health_reason` |
| Bus | `<B>/service/state` `can_state`, `bus_age_s` | `error-active`; `bus_age_s` small (seconds) |
| Polling | `<B>/service/state` `polling_suspended` | `false` |
| Module availability | `<B>/module/<m>/availability` | `online`, `retained=True`, for every module (MQTT-12) |
| Module freshness | `<B>/module/<m>/state` `online`, `cells_fresh` | both `true` for every module; note `bcast_age_s`, `cell_age_s` |
| Stack | `<B>/stack/state` `modules_online` | equals `MODULE_COUNT` (3 for this stack) |
| Timestamps | `ts` in each state payload | `ts_age` within a few seconds of `PUBLISH_INTERVAL_S` (default 10 s) |
| Cadence | `count=` of each `/state` topic | about window / `PUBLISH_INTERVAL_S` (3 for 30 s at the default; MQTT-19) |
| Counters | `<B>/service/state` `dropped_frames`, `unknown_frames`, `malformed_frames`, `error_frames` | not increasing between two runs (`dropped_frames` may be `null` when unavailable) |
| Reconnects | `<B>/service/state` `mqtt_reconnects` | unchanged between runs unless the broker was restarted (step 6) |
| Restarts | `<B>/service/state` `service_restarts`, `escalation`, `last_exit_code`, `last_exit_result` | as expected for the step; `escalation` 0 in steady state |
| Local log | `<B>/service/state` `local_log` | `ok` (or `disabled` if configured so) |
| Module alerts | `<B>/module/<m>/state` `bms_alarm`, `bms_protection`, `alert_cell_spread`, `alert_cell_low` | `false` unless expected |
| Stack alerts | `<B>/stack/state` `any_alarm`, `any_protection`, `alert_current_imbalance`, `alert_any_cell_spread`, `alert_any_cell_low`, `alert_any_bms_alarm`, `alert_any_bms_protection` | `false` unless expected |

Also flag: a `null` in a key MQTT-16/17/18 says is never null, any key missing or extra versus
those tables (MQTT-15), any topic under `<B>/` not listed in MQTT-11, and a retained state
topic (state is never retained). Current is positive when charging.

## 4. Report

1. A short table: check, pass/fail, observed value.
2. Anything unexpected, with the topic and key.
3. When the user names an acceptance step (`docs/acceptance/README.md` §3), say which rows
   are that step's MQTT-side evidence and what the TEST-39 table in
   `docs/specs/11-testing.md` expects for it.

## 5. Limits

The snapshot sees only what is published under `<B>/`. It does not see Home Assistant
discovery under `homeassistant/`, the journal (`can-up`, `recover`, exit reasons), the
systemd state or `ip -d -s link show can0`; those need the Pi (`make logs`,
`systemctl status`). A state topic missing from the output was not published during the
window; a longer `--seconds` tells a slow cadence from a stopped one.
