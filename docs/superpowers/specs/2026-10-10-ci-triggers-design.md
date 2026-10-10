# dyness-b3-bms-mqtt — CI triggers

* **Date:** 2026-10-10
* **Status:** accepted
* **Scope:** which events run `.github/workflows/ci.yml`

The workflow ran on every `push` to any branch and on every `pull_request` targeting
`main`. A push to a branch with an open PR fires both events, so every commit on a PR ran
the `check` and `mqtt` jobs twice. Earlier design records are unchanged.

## Changes

1. **TEST-34** (`11-testing.md`, `.github/workflows/ci.yml`, `tests/harness/test_project.py`):
   the `push` trigger is limited to `main`; `pull_request` on `main` is unchanged. A PR
   branch is covered once, by the `pull_request` event, and `main` once, by the `push`
   after a merge, which is also the event release-please acts on.

## Risks

* A branch with no open PR gets no CI until a PR is opened. Accepted: the work on this
  project goes through PRs, and `make check` runs the same suite locally.
