# dyness-b3-bms-mqtt — dynamic version

* **Date:** 2026-10-10
* **Status:** accepted
* **Scope:** where the package version is written, how release-please bumps it, and what
  `uv.lock` records about it

The version lived in `pyproject.toml`, and `uv.lock` repeated it in the root package's
entry. release-please bumped `pyproject.toml` through the `python` release type and was
meant to bump the lock through a `toml` extra-file with the JSONPath filter
`$.package[?(@.name=='dyness-b3-bms-mqtt')].version`. That updater never applied: the
0.1.1 release PR (#5) changed `pyproject.toml` and left `uv.lock` at 0.1.0. Installs and CI
use `--frozen`, which does not compare the lock's root version with `pyproject.toml`, so
nothing broke, but the committed lock was stale after every release while INST-11 and
INST-17 promised it was current. Earlier design records are unchanged.

## Changes

1. **INST-14** (`10-installation.md`, `pyproject.toml`, `tests/harness/test_project.py`):
   the build backend is `hatchling` instead of `uv_build`, because `uv_build` refuses a
   dynamic version. The wheel target packages `src/dyness_b3_bms_mqtt`, `py.typed`
   included, and the console script is unchanged. The sdist is limited to `src/` plus the
   files hatchling always adds; installs never use it (they build the wheel from the
   release's source archive), so this only keeps `uv build` from sweeping up `.hypothesis/`.
2. **INST-17** (`10-installation.md`, `pyproject.toml`, `src/dyness_b3_bms_mqtt/__init__.py`,
   `release-please-config.json`, `.github/workflows/release-please.yml`, `uv.lock`,
   `tests/harness/test_project.py`): the version is the one line `__version__ = "0.1.0"` in
   the package's `__init__.py`. `pyproject.toml` says `dynamic = ["version"]` and
   `[tool.hatch.version] path` names that file. With a dynamic version `uv lock` writes the
   root package with no `version` line at all, so there is nothing in the lock for a release
   to bump. The dead `uv.lock` extra-file is removed. The test asserts the lock's root entry
   has no version.
3. **INST-18** (`tests/harness/test_project.py`): the quick-start lines are compared with
   `__version__` instead of `pyproject.toml`'s version. The rule itself is unchanged.

## release-please configuration

`release-type` stays `python`, with no extra-file for the version. From release-please's
source (`main` at `fa67225`):

* `src/strategies/python.ts` pushes a `PythonFileWithVersion` update for
  `<name>/__init__.py` and `src/<name>/__init__.py`, for the `pyproject.toml` project name
  both as written and with `-` replaced by `_`. `src/dyness_b3_bms_mqtt/__init__.py` is one
  of those paths. Each is `createIfMissing: false`, so the paths that do not exist are
  skipped. The strategy does not read a `version-file` option.
* `src/updaters/python/python-file-with-version.ts` rewrites the first match of
  `(__version__ ?= ?["'])X.Y.Z(["'])`. A type annotation (`__version__: str = ...`) would not
  match, so the line is a plain assignment; hatchling's default pattern accepts it too.
* `src/updaters/python/pyproject-toml.ts` returns the file unchanged with a warning when
  `project.version` is absent and `project.dynamic` contains `version`; it throws only when
  the version is absent and not declared dynamic. The strategy still reads the project name
  from `pyproject.toml`, which is what locates `__init__.py`.

The `simple` release type was not needed. Its strategy (`src/strategies/simple.ts`) only
updates `version-file`, default `version.txt`, with `createIfMissing: false`, so it would
not create a stray file, but the version would then have to come from a `generic`
extra-file annotation on `__init__.py`. That works too; the `python` type does the same
with no extra config and keeps its changelog sections.

## Alternatives considered

* **Accept the lag.** `--frozen` makes the stale root version harmless, and the next
  `uv lock` fixes it. Rejected: the spec would have to promise a stale file, and any
  contributor running `uv lock` gets an unrelated diff after each release.
* **Index the lock array by position** (`$.package[N].version`). The `toml` updater might
  accept a plain index where it ignored the filter, but the root entry's position moves
  whenever a dependency sorting before it is added or removed, and a wrong index silently
  bumps another package's version.

## Risks

* Release PR #5 is open and was generated from the old config. After this merges
  release-please regenerates it from `main`. A reviewer should confirm the regenerated PR
  bumps `src/dyness_b3_bms_mqtt/__init__.py`, the manifest, `README.md` and
  `10-installation.md`, and does not touch `pyproject.toml` or `uv.lock`.
* Installs now fetch `hatchling` instead of `uv_build` as the build dependency when
  `uv sync` builds the wheel. Both come from PyPI the same way, so the install's network
  needs are unchanged.
