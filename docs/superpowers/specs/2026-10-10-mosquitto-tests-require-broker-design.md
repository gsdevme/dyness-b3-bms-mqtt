# dyness-b3-bms-mqtt — mosquitto tests require a broker

* **Date:** 2026-10-10
* **Status:** accepted
* **Scope:** how the `mosquitto` tests are selected and how their broker is provided, in CI
  and locally; the suite-wide test timeout

The `mosquitto` tests used to be skipped whenever no broker answered at
`MQTT_HOST`:`MQTT_PORT`, and the CI `mqtt` job set an environment variable,
`MQTT_REQUIRE_BROKER=1`, to turn that skip into a failure. A skip is the wrong default for a
test that a job exists to run: the job could pass green with nothing run, and the switch that
prevented it lived in the job's environment rather than in the command. This record replaces
the switch with an explicit pytest option, records where the CI broker image now comes from,
adds a local broker, and writes down the suite-wide timeout the project already enforces.
Earlier design records are unchanged.

## Changes

1. **TEST-01, TEST-02, TEST-03** (`11-testing.md`, `tests/plugins/markers.py`,
   `pyproject.toml`): `mosquitto` tests are deselected unless pytest is given
   `--mosquitto`, so the default run (`uv run pytest`, `make check`) neither runs nor reports
   them and needs no broker. With `--mosquitto` the broker is a hard requirement: it is
   probed once per session, retrying for up to 10 s because a CI service container has no
   healthcheck and may still be starting, and if it never accepts a TCP connection every
   `mosquitto` test fails with the host, port and wait as its reason. A `mosquitto` test is
   never skipped. `MQTT_REQUIRE_BROKER` is removed; it was never part of
   `08-configuration.md`, and no pytest option belongs there. The `hardware` marker keeps its
   skip with a reason: those tests are run by hand on the real stack, never by a CI job, and
   TEST-38's `--hardware-poll` fixtures build on the skip.
2. **TEST-08** (`11-testing.md`, `tests/plugins/reqtrace.py`): the traceability plugin
   treated any deselection as a narrowed run and downgraded rule 1 to a warning, which would
   have done so for every default run once the `mosquitto` tests are deselected. A
   deselection now narrows the run only when it removes a test that could cover a
   requirement; `mosquitto` and `hardware` tests never cover (TEST-07).
3. **TEST-35** (`11-testing.md`, `.github/workflows/ci.yml`): the `mqtt` job runs
   `uv run --frozen pytest --mosquitto -m mosquitto` with no extra environment beyond
   `MQTT_HOST` and `MQTT_PORT`.
4. **TEST-35** (`11-testing.md`, `.github/workflows/ci.yml`): the `mqtt` job's service
   container pulled `eclipse-mosquitto:1.6` from Docker Hub, whose unauthenticated pull
   rate limit failed the job on shared runners. The same official image is now pulled from
   the AWS Public ECR mirror, `public.ecr.aws/docker/library/eclipse-mosquitto:1.6`, which
   has no pull limit. This was first recorded as an extra item in
   `2026-10-09-implementation-deviations-design.md`; that edit to an existing dated record
   is reverted and the change is recorded here instead.
5. **TEST-35** (`11-testing.md`, `docker-compose.yml`, `README.md`): a `docker-compose.yml`
   at the repository root runs the same image on port 1883, so the CI command works locally
   after `docker compose up -d`. The README's development section gives the two commands.
6. **TEST-11** (`11-testing.md`): every test that is not marked `integration` is stopped by
   the suite-wide pytest-timeout ceiling of 120 s set by `timeout` in `pyproject.toml`, so a
   hung test fails instead of stalling the run. The ceiling was already configured and
   checked by `tests/harness/test_project.py`; the spec now states it.

## Risks

* A broker slower than 10 s to accept connections fails the `mqtt` job. The wait is the
  plugin constant `BROKER_WAIT_S` and can be raised if a runner ever needs longer.
* Running `-m mosquitto` without `--mosquitto` deselects everything and runs nothing. That
  run says so (every test reported as deselected) and exits with pytest's "no tests
  collected" status 5, so it cannot pass green either.
