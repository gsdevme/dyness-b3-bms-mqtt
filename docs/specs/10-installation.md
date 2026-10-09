# 10 — Installation (`INST`)

How the service gets onto a Pi. A `Makefile` in the repository root installs, upgrades and
removes everything from a git checkout. The unit file, helpers and udev rule it installs are
specified in `09-service.md`.

## 1. Supported target

* **INST-01** The supported target is Raspberry Pi OS 64-bit, Bookworm (Debian 12) or later,
  on `aarch64`. `make install` MUST check that `uname -m` is `aarch64` and that
  `/etc/os-release` has `ID=debian` (or `ID_LIKE` containing `debian`) with a numeric
  `VERSION_ID` of at least 12. If either check fails, it MUST stop with a clear message before
  changing anything.
* **INST-02** `install`, `uninstall` and `restart` MUST run as root (`sudo make install`). If
  run as anyone else they MUST stop with `run with sudo` before changing anything. The
  checkout can be at any path, including under `/home`.

## 2. Paths

| Path | Contents | Created by |
|---|---|---|
| `/usr/local/bin/uv` | uv binary | `install`, only if `uv` is not already on `PATH` |
| `/opt/dyness-b3-bms-mqtt/python` | uv-managed CPython 3.14 (`UV_PYTHON_INSTALL_DIR`) | `install` |
| `/opt/dyness-b3-bms-mqtt/.venv` | Runtime virtualenv with the package installed non-editable | `install` |
| `/usr/local/lib/dyness-b3-bms-mqtt/can-up` | Helper, `0755 root:root` | `install` |
| `/usr/local/lib/dyness-b3-bms-mqtt/recover` | Helper, `0755 root:root` | `install` |
| `/etc/default/dyness-b3-bms-mqtt` | Environment file, `0600 root:root` | `install`, only if absent |
| `/etc/systemd/system/dyness-b3-bms-mqtt.service` | Unit, `0644` | `install` |
| `/etc/udev/rules.d/80-dyness-b3-can.rules` | udev rule, `0644` | `install` |
| `/var/lib/dyness-b3-bms-mqtt` | State directory | systemd (`StateDirectory=`) |

Repository sources: `packaging/dyness-b3-bms-mqtt.service`, `packaging/can-up`,
`packaging/recover`, `packaging/80-dyness-b3-can.rules` and
`packaging/dyness-b3-bms-mqtt.env` (CFG-08).

* **INST-03** The installer MUST install to exactly the paths in the table above, with the
  modes listed, and MUST NOT write anywhere else except the apt package set (INST-06) and
  root's uv cache.

## 3. Makefile rules

* **INST-04** The `Makefile` MUST work with GNU make as shipped on Pi OS. It MUST set
  `SHELL := /bin/bash` and `.SHELLFLAGS := -eu -o pipefail -c`, so any failing command,
  including one in a pipeline, stops the target with a non-zero status. It MUST derive the
  checkout directory as `$(abspath $(dir $(lastword $(MAKEFILE_LIST))))` and use absolute
  paths built from it, so `make -C /path/to/checkout install` behaves the same as running
  `make install` inside it.
* **INST-05** All uv calls made by `install` MUST set `UV_PYTHON_INSTALL_DIR=/opt/dyness-b3-bms-mqtt/python`,
  `UV_PYTHON_PREFERENCE=only-managed` and `UV_LINK_MODE=copy`, and MUST run with
  `umask 022`.

## 4. Targets

### `install`

* **INST-06** `install` MUST carry out these steps in order:
  1. Run the checks in INST-01 and INST-02.
  2. `apt-get update` and `apt-get install -y --no-install-recommends ca-certificates curl iproute2 usbutils`.
  3. If `uv` is not on `PATH`, install it with the official installer:
     `curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh`.
  4. `uv python install --no-bin 3.14` (into `UV_PYTHON_INSTALL_DIR`. `--no-bin` stops uv
     from placing interpreter shims outside `/opt`).
  5. Create the runtime venv from the checkout:
     `UV_PROJECT_ENVIRONMENT=/opt/dyness-b3-bms-mqtt/.venv uv sync --project <checkout> --frozen --no-dev --no-editable --compile-bytecode --python 3.14 --reinstall-package dyness-b3-bms-mqtt`.
     `--no-editable` is required because the service runs with `ProtectHome=yes` and cannot
     read a checkout under `/home`. `--reinstall-package` makes sure a re-run installs the
     current checkout even when the version string has not changed. `--compile-bytecode`
     is needed because the service cannot write `__pycache__` under `ProtectSystem=strict`.
  6. `chmod -R a+rX /opt/dyness-b3-bms-mqtt`, so the dynamic user can read and run the
     interpreter and the venv (INST-12).
  7. `install -D -m 0755 -o root -g root` the two helpers into `/usr/local/lib/dyness-b3-bms-mqtt/`.
  8. `install -m 0644` the unit file. `install -m 0644` the udev rule, then
     `udevadm control --reload`.
  9. If `/etc/default/dyness-b3-bms-mqtt` does not exist, `install -m 0600 -o root -g root`
     the example env file there. If it exists, leave it untouched and print that it was
     kept.
  10. `systemctl daemon-reload`.
  11. Run the `check-config` target. If the configuration is invalid, stop here with its output.
  12. `systemctl enable dyness-b3-bms-mqtt`, then `systemctl restart dyness-b3-bms-mqtt`.
      `restart` is used instead of `enable --now` so that a re-run picks up the new code.
  13. Print `systemctl status --no-pager dyness-b3-bms-mqtt` and a reminder to edit
      `/etc/default/dyness-b3-bms-mqtt`, then run `sudo make restart`.
* **INST-07** `install` MUST be idempotent. Running it again on an installed system upgrades
  the venv, helpers, unit and udev rule in place, never overwrites the env file or the state
  directory, and restarts the service.

### `uninstall`

* **INST-08** `uninstall` MUST: `systemctl disable --now dyness-b3-bms-mqtt` (ignoring "not
  loaded"); remove the unit file and run `systemctl daemon-reload`; remove
  `/usr/local/lib/dyness-b3-bms-mqtt`; remove the udev rule and run
  `udevadm control --reload`; and remove `/opt/dyness-b3-bms-mqtt` (venv and interpreter).
  It MUST keep `/usr/local/bin/uv`, because other software may use it. It MUST keep
  `/etc/default/dyness-b3-bms-mqtt` and the state directory unless `PURGE=1` is given. With
  `PURGE=1` it MUST also run `systemctl clean --what=state dyness-b3-bms-mqtt` before the
  unit is removed, remove the env file, and remove any remaining
  `/var/lib/dyness-b3-bms-mqtt` and `/var/lib/private/dyness-b3-bms-mqtt`. Running it on a
  system where nothing is installed MUST succeed.

### Operational targets

| Target | Behaviour | Root |
|---|---|---|
| `status` | `systemctl status --no-pager dyness-b3-bms-mqtt`. An inactive or failed unit is shown, not treated as a make error | No |
| `logs` | `journalctl -u dyness-b3-bms-mqtt -f -n 100` | No (journal group) or sudo |
| `restart` | `systemctl restart dyness-b3-bms-mqtt` | Yes |
| `check-config` | `systemd-run --quiet --pipe --wait --collect -p DynamicUser=yes -p EnvironmentFile=-/etc/default/dyness-b3-bms-mqtt /opt/dyness-b3-bms-mqtt/.venv/bin/dyness-b3-bms-mqtt --check-config`. Exits with the program's status (0 or 78) | Yes |
| `dev` | `uv sync --all-extras` in the checkout's own `.venv`, with the dev dependency group, on any host (Linux or macOS). Touches no system path | No |

* **INST-09** The `status`, `logs`, `restart`, `check-config` and `dev` targets MUST behave as in
  the table above. The `check` target (lint, types and tests) is defined by TEST-33 in
  `11-testing.md`.
* **INST-10** `check-config` MUST load the env file with systemd's own parser, through
  `systemd-run -p EnvironmentFile=`, and MUST NOT source it with the shell. This way the
  check sees exactly what the service will see, including quoting.

## 5. Python and dependencies

* **INST-11** The repository MUST pin Python 3.14: `.python-version` contains `3.14`,
  `pyproject.toml` has `requires-python = ">=3.14"`, and `uv.lock` is committed and kept
  current (installs use `--frozen`).
* **INST-12** The uv-managed interpreter under `/opt/dyness-b3-bms-mqtt/python` and the
  venv under `/opt/dyness-b3-bms-mqtt/.venv` MUST be readable by any user: directories
  `0755`, files at least `0644`, executables `0755`. The service's dynamic user has no other
  way to read them.
* **INST-13** Runtime dependencies MUST be exactly `python-can` and `paho-mqtt` (2.x,
  `>=2,<3`). `gs_usb` and `pyusb` MUST only be in the optional extra `gs-usb`, which is for
  macOS development. It is never installed on the Pi. There MUST NOT be a dependency for
  `sd_notify` (SVC-05). Development tools are in the `dev` dependency group, owned by
  `11-testing.md`.
* **INST-14** `pyproject.toml` MUST declare the console script `dyness-b3-bms-mqtt`, pointing
  at the application entry point, and use the `uv_build` build backend.
* **INST-17** The version MUST be single-sourced from `version` in `pyproject.toml`: the
  package reads its `__version__` from the installed distribution metadata and contains no
  version literal. release-please MUST bump it (`release-please-config.json` and
  `.release-please-manifest.json`), and update the root package's version in `uv.lock`
  through its `extra-files` TOML updater so the lock file stays current (INST-11).

## 6. README promises

The README is written separately. It MUST give these quick-start commands, which the
installer MUST support exactly as written:

```sh
sudo apt-get install -y git make
git clone https://github.com/gsdevme/dyness-b3-bms-mqtt.git
cd dyness-b3-bms-mqtt
sudo make install
sudo nano /etc/default/dyness-b3-bms-mqtt   # set MQTT_HOST etc.
sudo make check-config
sudo make restart
make logs
```

Upgrade: `git pull && sudo make install`. Removal: `sudo make uninstall`, or
`sudo make uninstall PURGE=1` to also remove configuration and state.

* **INST-15** The README quick-start commands above MUST work on a fresh Pi OS Lite 64-bit
  Bookworm image, with the service ending up `active (running)`. **MANUAL**.
* **INST-16** A second `sudo make install` on the same Pi MUST finish without errors, keep a
  modified `/etc/default/dyness-b3-bms-mqtt` byte-for-byte, and restart the service.
  **MANUAL**.
