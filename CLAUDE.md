# Context for AI assistants working in this repo

This repo is a systemd service for a Raspberry Pi Zero 2 W that reads a 3 × Dyness B3 stack
over CAN and publishes it to MQTT with Home Assistant discovery.
Read `README.md`, then `docs/specs/00-overview.md`, then the spec for the area you are changing.

Spec-driven rules:
* `docs/specs/` is the living spec. Change it, and add a dated design doc in
  `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`, before or with the code. Never edit
  an existing dated design doc.
* Tests reference requirement IDs with `@pytest.mark.req("PREFIX-NN")`. Every `MUST` needs a
  test or a **MANUAL** tag; the suite fails otherwise (`docs/specs/11-testing.md`).
* Env var names and defaults live only in `docs/specs/08-configuration.md`.

Release rules:
* Commits are Conventional Commits; release-please derives the CHANGELOG and versions from
  them. Don't use `chore`.
* The version lives only in `src/dyness_b3_bms_mqtt/__init__.py`; release-please bumps it.
* Lines annotated `x-release-please-version` (README quick start, `10-installation.md` §6)
  are rewritten by release-please; never hand-edit them.

Key facts that are easy to get wrong:
* B3 is **15S**, not 16S. Cell slot 16 in `0x14` reads 0; ignore it.
* The BMS talks **CAN** (RJ45 pins 4/5) with the Solis setting, not RS485.
* Module number = CAN address (`0x18F21m??`), not shelf position.
* `0x22` cell numbers are 0-based on the wire; cells are 1-based everywhere in this project.
* Raw `0x21` current is negative when charging; published current is **positive when charging**.
* Never transmit anything except the poll `0x18F21m77` (`01 00 00 00 00 00 00 00`). Never
  `0x18F26677` (parameter write); it must stay unconstructible.
* Always `bus.shutdown()` on every exit path. The SH-C31A stopped receiving after transmitting
  sessions on macOS until replugged; keep transmissions minimal.

Other notes:
* `docs/specs/01-protocol.md` is the protocol authority for this repo. Hardware results win
  over it; update the spec to match.
* Python 3.14 managed by `uv`: `uv run pytest`, `make check`. Tests must not need hardware.
* Don't add Dyness binaries, decompiled source, manuals or parameter files to the repo.
