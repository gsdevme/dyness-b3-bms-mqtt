# dyness-b3-bms-mqtt — release tag name

* **Date:** 2026-10-10
* **Status:** accepted
* **Scope:** the tag name release-please gives each release

The first release came out tagged `dyness-b3-bms-mqtt-v0.1.0`, not `v0.1.0`: release-please
puts the component (the package name) in the tag unless `include-component-in-tag` is
false. The quick start downloads `archive/refs/tags/v0.1.0.tar.gz`, so it returned 404.
A `v0.1.0` tag added by hand at the release commit covers the first release, and the
config covers every release after it. Earlier design records are unchanged.

## Changes

1. **INST-18** (`10-installation.md`, `release-please-config.json`,
   `.github/workflows/release-please.yml`, `tests/harness/test_project.py`): release tags
   must be `vX.Y.Z` with no package-name prefix (`"include-v-in-tag": true`,
   `"include-component-in-tag": false`). The test pins both keys and checks that the
   annotated quick-start lines download `archive/refs/tags/vX.Y.Z.tar.gz` and `cd` into
   `dyness-b3-bms-mqtt-X.Y.Z`.

## Risks

* release-please finds the previous release by the manifest version: it first looks for a
  GitHub release whose tag matches the configured format, then for a git tag of that name.
  With the component dropped, the existing `dyness-b3-bms-mqtt-v0.1.0` release no longer
  matches, so the next run relies on the hand-made `v0.1.0` tag. Without it, release-please
  would not find the 0.1.0 release commit and would build the next changelog from the whole
  history. The tag must exist before the next release PR is built.
* The release PR title no longer carries the package name (`build(main): release 0.2.0`).
  release-please fills `${component}` from the same setting as the tag. The title pattern
  is unchanged and still gives a valid Conventional Commits subject.
