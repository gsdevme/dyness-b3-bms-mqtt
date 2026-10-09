# Development, installation and operational targets for dyness-b3-bms-mqtt.
#
# Behaviour is specified in docs/specs/10-installation.md (INST) and, for `check`,
# docs/specs/11-testing.md (TEST-33). The installed paths and the unit they serve are in
# docs/specs/09-service.md. Every path is built from CHECKOUT, so
# `make -C /path/to/checkout <target>` behaves the same as running make inside it.
#
#   make dev                      uv sync in the checkout's own .venv (any host)
#   make check                    lint, format check, types and tests
#   sudo make install             install, or upgrade in place; never touches your settings
#   sudo make uninstall           remove everything but settings, state and uv
#   sudo make uninstall PURGE=1   also remove settings and state
#   make status                   systemctl status of the service
#   make logs                     follow the service's journal
#   sudo make restart             restart the service, e.g. after editing settings
#   sudo make check-config        validate /etc/default/dyness-b3-bms-mqtt as the service sees it

# INST-04: every recipe line runs in bash with -e, -u and pipefail, so any failing command,
# including one inside a pipeline, stops the target with a non-zero status.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

# INST-04: the checkout is wherever this Makefile lives, not the current directory.
CHECKOUT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

# The uv command. Overridable for a uv that is not on PATH; `install` fetches uv into
# /usr/local/bin (on root's PATH on Pi OS) only when this command is not found.
UV ?= uv

# ---------------------------------------------------------------------------------------
# Host facts. Each is an ordinary assignment, so a command-line value replaces it. That is
# how tests/packaging/test_makefile.py stands in for a Pi on any machine.
# ---------------------------------------------------------------------------------------

# CPU architecture, checked by preflight (INST-01).
UNAME_M := $(shell uname -m)
# The os-release file preflight reads (INST-01). Made absolute below because the shell's
# `.` searches PATH for a name without a slash.
OS_RELEASE := /etc/os-release
# Effective user id, checked by root-check (INST-02).
EUID := $(shell id -u)

# ---------------------------------------------------------------------------------------
# Install options.
# ---------------------------------------------------------------------------------------

# Staging root prefixed to every path that install and uninstall write or remove. Empty for
# a real install; the tests point it at a temporary directory. It does not stop the system
# commands (apt-get, systemctl, ...) from running, and paths that systemd itself reads at
# run time, such as those passed to systemd-run by check-config, never carry it.
DESTDIR ?=
# Owner flags for `install`. root:root on the Pi; the tests empty it to run unprivileged.
INSTALL_OWNER ?= -o root -g root
# PURGE=1 makes uninstall also remove the env file and the state directory (INST-08).
PURGE ?= 0

# ---------------------------------------------------------------------------------------
# Fixed values. Only the host facts and install options above may be set on the command
# line: root runs `rm -rf` on these paths, so `make uninstall SERVICE=` or `OPT_DIR=/opt`
# must never redirect it. Each is declared with `override`, so neither the command line
# nor `make -e` can replace it, and any attempt to set one on the command line stops make
# before it does anything. CHECKOUT, whose line stays a plain `:=`, is checked here too,
# which also catches it coming from the environment under `make -e`.
# ---------------------------------------------------------------------------------------

FIXED := CHECKOUT PACKAGING OS_RELEASE_FILE SERVICE OPT_DIR PYTHON_DIR VENV_DIR HELPER_DIR \
	ENVFILE UNIT_FILE UDEV_RULE UV_BIN_DIR STATE_DIRS UV_ENV CHECK_CONFIG
$(foreach name,$(FIXED),$(if $(or $(findstring command line,$(origin $(name))),$(findstring environment override,$(origin $(name)))),$(error $(name) is fixed by the Makefile and cannot be set on the command line or with make -e)))

override PACKAGING := $(CHECKOUT)/packaging
override OS_RELEASE_FILE = $(abspath $(OS_RELEASE))

# Installed paths (10-installation.md §2). The names avoid ENV_FILE and UV_INSTALL_DIR,
# which the root helpers and the uv installer read from the environment.
override SERVICE := dyness-b3-bms-mqtt
override OPT_DIR := /opt/$(SERVICE)
override PYTHON_DIR := $(OPT_DIR)/python
override VENV_DIR := $(OPT_DIR)/.venv
override HELPER_DIR := /usr/local/lib/$(SERVICE)
override ENVFILE := /etc/default/$(SERVICE)
override UNIT_FILE := /etc/systemd/system/$(SERVICE).service
override UDEV_RULE := /etc/udev/rules.d/80-dyness-b3-can.rules
override UV_BIN_DIR := /usr/local/bin
# StateDirectory= with DynamicUser=yes keeps the data under /var/lib/private and links
# /var/lib/<name> to it.
override STATE_DIRS := /var/lib/$(SERVICE) /var/lib/private/$(SERVICE)

# INST-05: the environment of every uv call made by install.
#   UV_PYTHON_INSTALL_DIR  put the managed CPython under /opt, not in root's home, where
#                          the service's dynamic user could not read it;
#   UV_PYTHON_PREFERENCE   use only uv's own CPython 3.14, never the system python3;
#   UV_LINK_MODE=copy      copy packages out of root's uv cache rather than hard-linking,
#                          so the venv stands alone and keeps its a+rX modes.
override UV_ENV := UV_PYTHON_INSTALL_DIR="$(DESTDIR)$(PYTHON_DIR)" UV_PYTHON_PREFERENCE=only-managed UV_LINK_MODE=copy

# INST-09, INST-10: validate the settings exactly as the service will see them. The binary
# runs in a transient unit with the service's DynamicUser= and EnvironmentFile=, so the env
# file is loaded by systemd's own parser (quoting included) and never sourced by the shell.
# The leading "-" matches the unit: a missing file means every default. --pipe connects the
# program's output to this terminal, --wait returns its exit status (0, or 78 for an invalid
# setting) and --collect removes the transient unit even when it fails.
override CHECK_CONFIG := systemd-run --quiet --pipe --wait --collect -p DynamicUser=yes -p EnvironmentFile=-$(ENVFILE) $(VENV_DIR)/bin/$(SERVICE) --check-config

.PHONY: dev check root-check preflight install uninstall status logs restart check-config

# ---------------------------------------------------------------------------------------
# Development (INST-09, TEST-33). Touches nothing outside the checkout.
# ---------------------------------------------------------------------------------------

dev:
	$(UV) --directory $(CHECKOUT) sync --all-extras

check:
	$(UV) --directory $(CHECKOUT) run --frozen ruff check
	$(UV) --directory $(CHECKOUT) run --frozen ruff format --check
	$(UV) --directory $(CHECKOUT) run --frozen mypy --strict src tests
	$(UV) --directory $(CHECKOUT) run --frozen pytest

# ---------------------------------------------------------------------------------------
# Checks run before anything is changed.
# ---------------------------------------------------------------------------------------

# INST-02: install, uninstall, restart and check-config stop here unless run as root.
root-check:
	@if [ "$(EUID)" != 0 ]; then echo "run with sudo" >&2; exit 1; fi

# INST-01: stop unless this is Raspberry Pi OS 64-bit (or Debian) 12 or later.
#  1. The architecture must be aarch64; 32-bit Pi OS reports armv7l.
#  2. The os-release file must be readable.
#  3. It is shell syntax by definition, so it is sourced; that also strips its quotes.
#     ID must be debian, or ID_LIKE must list debian (as derivatives such as raspbian do).
#  4. VERSION_ID must be all digits and at least 12 (Bookworm). A missing one (Debian
#     testing) or a dotted one (Ubuntu's 24.04, which is not a Debian release number) fails.
preflight:
	@if [ "$(UNAME_M)" != aarch64 ]; then \
		echo "unsupported architecture $(UNAME_M): needs aarch64 (Raspberry Pi OS 64-bit)" >&2; \
		exit 1; \
	fi
	@if [ ! -r "$(OS_RELEASE_FILE)" ]; then echo "cannot read $(OS_RELEASE_FILE)" >&2; exit 1; fi
	@. "$(OS_RELEASE_FILE)"; \
	case " $${ID:-} $${ID_LIKE:-} " in *" debian "*) debian=yes ;; *) debian=no ;; esac; \
	case "$${VERSION_ID:-}" in "" | *[!0-9]*) version=0 ;; *) version=$$VERSION_ID ;; esac; \
	if [ "$$debian" != yes ] || [ "$$version" -lt 12 ]; then \
		echo "unsupported system ID=$${ID:-} VERSION_ID=$${VERSION_ID:-}:" \
			"needs Debian 12 or later (Raspberry Pi OS Bookworm 64-bit)" >&2; \
		exit 1; \
	fi

# ---------------------------------------------------------------------------------------
# install (INST-06, INST-07): the numbered steps of INST-06, in order. Step 1 is the two
# prerequisites. Re-running upgrades the venv, helpers, unit and udev rule in place, keeps
# the env file and the state directory, and restarts the service.
# ---------------------------------------------------------------------------------------

install: root-check preflight
# 2. The packages the installer and the helpers need: curl fetches uv, iproute2 provides
#    `ip` for can-up and recover, usbutils provides `usbreset` for recover.
	apt-get update
	apt-get install -y --no-install-recommends ca-certificates curl iproute2 usbutils
# 3. uv, with the official installer, only when it is not already on PATH. The installer
#    must not edit shell profiles (UV_NO_MODIFY_PATH).
	command -v $(UV) >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$(DESTDIR)$(UV_BIN_DIR)" UV_NO_MODIFY_PATH=1 sh
# 4. CPython 3.14 into PYTHON_DIR (INST-05). --no-bin keeps uv from placing interpreter
#    shims outside /opt. umask 022 makes everything uv creates readable by any user.
	umask 022 && $(UV_ENV) $(UV) python install --no-bin 3.14
# 5. The runtime venv from this checkout's lock file (--frozen), without dev tools.
#    --no-editable: the service runs with ProtectHome=yes and cannot read a checkout
#    under /home. --compile-bytecode: it cannot write __pycache__ under
#    ProtectSystem=strict. --reinstall-package: a re-run installs the current checkout
#    even when the version string has not changed.
	umask 022 && $(UV_ENV) UV_PROJECT_ENVIRONMENT="$(DESTDIR)$(VENV_DIR)" $(UV) sync --project "$(CHECKOUT)" --frozen --no-dev --no-editable --compile-bytecode --python 3.14 --reinstall-package $(SERVICE)
# 6. INST-12: the dynamic user needs to read the interpreter and venv and run their
#    executables. X adds execute only to directories and to files already executable.
	chmod -R a+rX "$(DESTDIR)$(OPT_DIR)"
# 7. The root helpers the unit runs as ExecStartPre=+ and ExecStopPost=+ (and udev runs).
	install -D -m 0755 $(INSTALL_OWNER) "$(PACKAGING)/can-up" "$(DESTDIR)$(HELPER_DIR)/can-up"
	install -D -m 0755 $(INSTALL_OWNER) "$(PACKAGING)/recover" "$(DESTDIR)$(HELPER_DIR)/recover"
# 8. The unit and the udev rule, then make udev re-read its rules.
	install -D -m 0644 $(INSTALL_OWNER) "$(PACKAGING)/$(SERVICE).service" "$(DESTDIR)$(UNIT_FILE)"
	install -D -m 0644 $(INSTALL_OWNER) "$(PACKAGING)/80-dyness-b3-can.rules" "$(DESTDIR)$(UDEV_RULE)"
	udevadm control --reload
# 9. CFG-08: the example env file, only when there is none. An existing file, or a
#    symlink in its place, is never overwritten.
	if [ -e "$(DESTDIR)$(ENVFILE)" ] || [ -L "$(DESTDIR)$(ENVFILE)" ]; then echo "kept existing $(ENVFILE)"; else install -D -m 0600 $(INSTALL_OWNER) "$(PACKAGING)/$(SERVICE).env" "$(DESTDIR)$(ENVFILE)"; fi
# 10. Load the new or changed unit.
	systemctl daemon-reload
# 11. The check-config target's command: an invalid setting stops the install here, with
#     the program's message, before the service is restarted on it.
	$(CHECK_CONFIG)
# 12. enable, then restart rather than `enable --now`, so a re-run picks up the new code.
	systemctl enable $(SERVICE)
	systemctl restart $(SERVICE)
# 13. Show the result. Status 3 (inactive or failed) is shown, not treated as an error.
	systemctl status --no-pager $(SERVICE) || [ $$? -eq 3 ]
	@echo "Edit $(ENVFILE) (at least MQTT_HOST), then run: sudo make restart"

# ---------------------------------------------------------------------------------------
# uninstall (INST-08). Safe on a system where nothing is installed. Keeps /usr/local/bin/uv,
# which other software may use, and keeps the env file and the state directory unless
# PURGE=1.
# ---------------------------------------------------------------------------------------

uninstall: root-check
# Any PURGE other than 0 or 1 is a typo; stop before removing anything.
	@case "$(PURGE)" in 0 | 1) ;; *) echo "PURGE must be 0 or 1, not '$(PURGE)'" >&2; exit 1 ;; esac
# Defence in depth behind `override`: before removing anything, every directory to be
# removed must be a dyness-b3-bms-mqtt directory below / and each file must be the unit or
# the udev rule, with no ".." anywhere. The name is spelled out rather than taken from
# SERVICE so that the guard does not depend on the variables it guards.
	@for path in "$(OPT_DIR)" "$(HELPER_DIR)" "$(ENVFILE)" $(foreach dir,$(STATE_DIRS),"$(dir)") "$(UNIT_FILE)" "$(UDEV_RULE)"; do \
		case "$$path" in \
			*/../* | */..) ok=no ;; \
			/?*/dyness-b3-bms-mqtt | */dyness-b3-bms-mqtt.service | */80-dyness-b3-can.rules) ok=yes ;; \
			*) ok=no ;; \
		esac; \
		if [ "$$ok" != yes ]; then echo "refusing to uninstall: unexpected path '$$path'" >&2; exit 1; fi; \
	done
# Stop and disable. On a system without the unit this fails ("not loaded"); carry on.
	systemctl disable --now $(SERVICE) || echo "$(SERVICE) was not loaded; continuing"
# PURGE=1: let systemd remove the state directory while it still knows the unit. Without
# the unit this fails too; the state paths are removed directly at the end either way.
	if [ "$(PURGE)" = 1 ]; then systemctl clean --what=state $(SERVICE) || echo "no state for systemd to clean; continuing"; fi
	rm -f "$(DESTDIR)$(UNIT_FILE)"
	systemctl daemon-reload
	rm -rf "$(DESTDIR)$(HELPER_DIR)"
	rm -f "$(DESTDIR)$(UDEV_RULE)"
	udevadm control --reload
# The venv and the managed interpreter.
	rm -rf "$(DESTDIR)$(OPT_DIR)"
# PURGE=1: settings and anything left of the state directory.
	if [ "$(PURGE)" = 1 ]; then rm -f "$(DESTDIR)$(ENVFILE)"; rm -rf $(foreach dir,$(STATE_DIRS),"$(DESTDIR)$(dir)"); fi

# ---------------------------------------------------------------------------------------
# Operational targets (INST-09).
# ---------------------------------------------------------------------------------------

# No root needed. Status 3 (inactive or failed) is shown, not treated as a make error.
status:
	systemctl status --no-pager $(SERVICE) || [ $$? -eq 3 ]

# Needs root or membership of the systemd-journal group.
logs:
	journalctl -u $(SERVICE) -f -n 100

restart: root-check
	systemctl restart $(SERVICE)

# Fails with the program's status (78 for an invalid setting) and its message.
check-config: root-check
	$(CHECK_CONFIG)
