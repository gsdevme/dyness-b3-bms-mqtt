# dyness-b3-bms-mqtt — Install from the GitHub release archive

* **Date:** 2026-10-10
* **Status:** accepted
* **Scope:** the documented install and upgrade path, and the release-please config that
  keeps it current

The quick start cloned the repository with git and installed whatever `main` held, and
upgrades ran `git pull`. A Pi then ran an unreleased, moving target, and needed git
installed for nothing else. The quick start now downloads the source archive GitHub
generates for a release tag and runs `sudo make install` from the unpacked directory.
That archive already holds the `Makefile`, `packaging/`, `pyproject.toml` and `uv.lock`, so
the Makefile is unchanged and CI uploads nothing. Earlier design records are unchanged.

A wheel or PyPI package was considered and deferred: the systemd unit, the `can-up` and
`recover` helpers and the udev rule are not Python, so a wheel alone cannot install the
service, and the Makefile would still be needed beside it.

## Changes

1. **INST-15** wording, §6 (`10-installation.md`, `README.md`,
   `tests/packaging/test_readme.py`): the promised quick start installs `curl` instead of
   `git`, downloads `archive/refs/tags/vX.Y.Z.tar.gz` and changes into
   `dyness-b3-bms-mqtt-X.Y.Z`. Upgrade downloads the newer release the same way and runs
   `sudo make install` from its directory. The README still matches §6 byte for byte. The
   spec's intro now says the Makefile works from an unpacked release archive or a git
   checkout.
2. **INST-18** (`10-installation.md`, `release-please-config.json`,
   `.github/workflows/release-please.yml`, `tests/harness/test_project.py`): the download and
   `cd` lines carry the `x-release-please-version` annotation, and the config lists
   `README.md` and `docs/specs/10-installation.md` as `generic` extra-files, so each release
   PR rewrites both lines to the new version and the quick start always names the latest
   release. The test checks the config and that each file has exactly two annotated lines
   whose version is the one in `pyproject.toml`.
3. **INST-18**: the package is marked `"prerelease": true`, so GitHub releases are flagged as
   prereleases until 1.0.
4. `docs/acceptance/README.md`: the run records the installed release instead of a git
   commit, since an archive install has no git metadata.

## Ordering

This change must merge before the first release PR. The v0.1.0 archive is a snapshot of the
tagged tree, so it only contains the self-referential quick start (and the generic updater
only has annotated lines to bump) if this is already on `main` when v0.1.0 is cut.

## Risks

* GitHub's `/releases/latest` ignores prereleases. Nothing uses it today, since the quick
  start names an exact tag. It matters only if a script that resolves "latest" is added
  later; that script would need the releases API with prereleases included, or the flag
  dropped at 1.0.
* A release PR that fails to bump the annotated lines would leave the quick start on the old
  release. The INST-18 test fails if the lines and `pyproject.toml` disagree, so CI on the
  release PR catches it.
