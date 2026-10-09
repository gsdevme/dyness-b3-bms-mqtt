# Development and installation targets for dyness-b3-bms-mqtt.
# Behaviour is specified in docs/specs/10-installation.md (INST) and, for `check`,
# docs/specs/11-testing.md (TEST-33). Every path is built from CHECKOUT so that
# `make -C /path/to/checkout <target>` behaves the same as running make inside it.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

CHECKOUT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
UV ?= uv

.PHONY: dev check

dev:
	$(UV) --directory $(CHECKOUT) sync --all-extras

check:
	$(UV) --directory $(CHECKOUT) run --frozen ruff check
	$(UV) --directory $(CHECKOUT) run --frozen ruff format --check
	$(UV) --directory $(CHECKOUT) run --frozen mypy --strict src tests
	$(UV) --directory $(CHECKOUT) run --frozen pytest
