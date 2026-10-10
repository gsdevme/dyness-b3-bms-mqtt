# dyness-b3-bms-mqtt — `make upgrade`

* **Date:** 2026-10-10
* **Status:** accepted
* **Scope:** a new `upgrade` target in the root `Makefile` (INST-19), INST-02 and INST-03
  in `10-installation.md`, the upgrade line of §6 and the README quick start

## Motivation

Hardware acceptance of 0.1.2 on the Pi went through the documented upgrade by hand: `curl`
the new tag's archive, `tar xz`, `cd` into the new directory, `sudo make install`. It works,
but every step has to be typed with the new version in it, twice (the URL and the directory),
and the user has to look up which release is newest first. The release archive's directory
name carries the version (`dyness-b3-bms-mqtt-X.Y.Z`, INST-18), so there is no stable
directory to re-run anything in: the old checkout can only reinstall itself.

## Decision

`sudo make upgrade [VERSION=vX.Y.Z]`, run in any checkout, does the dance itself:

1. **Root and preflight first.** `upgrade` depends on `root-check` and `preflight`, like
   `install`. The sub-make's `install` needs root anyway, and checking up front means a
   plain `make upgrade` fails before downloading anything instead of after.
2. **Tag.** `VERSION` from the command line, else the first `tag_name` from
   `https://api.github.com/repos/gsdevme/dyness-b3-bms-mqtt/releases?per_page=1`, which lists
   the newest release first, prereleases included. `VERSION` is a plain assignment in the
   Makefile, so a `VERSION` in the environment cannot select a release. The tag must match
   `^v[0-9]+\.[0-9]+\.[0-9]+$`; it names the new directory, so this also keeps `..` and `/`
   out of the path.
3. **Parsing with grep and sed.** The Makefile otherwise needs nothing beyond coreutils,
   bash and curl, and python3, while present on Pi OS Lite, is not on every Debian 12 the
   preflight accepts; jq is on neither. `grep -o '"tag_name": *"[^"]*"' | head -n 1` finds
   the key in compact or pretty-printed JSON. A `"tag_name"` inside a release body is
   escaped (`\"tag_name\"`) and does not match.
4. **Already installed.** The installed console script's `--version` equal to the tag's
   `X.Y.Z` prints `already at vX.Y.Z` and exits 0 without a download.
5. **Never overwrite.** If `dyness-b3-bms-mqtt-X.Y.Z` exists next to the checkout as
   anything (directory, file, dangling link), stop and tell the user to remove it, pass
   `VERSION`, or run `sudo make install` in it. That also covers running `upgrade` in a
   freshly unpacked, not yet installed newest checkout.
6. **Download and unpack** with `curl -fsSL … | tar -xz -C <parent>`. A failure removes
   the new directory, which step 5 proved did not exist before. An archive that does not
   produce `dyness-b3-bms-mqtt-X.Y.Z/Makefile` is an error.
7. **Ownership.** `chown -R --reference=<checkout>` gives the new checkout the old one's
   owner, so `sudo` does not leave a root-owned directory in the user's home (tar running
   as root also keeps the archive's root ownership).
8. **Install** with a sub-make, `make -C <new> install`, then print the new path and that
   the old checkout can be removed. The sub-make is called through `$(SUBMAKE)`, not
   `$(MAKE)`, because make runs any recipe line naming `$(MAKE)` even under `-n`; this
   keeps `make -n upgrade` a dry run that downloads nothing.

Write set: the new checkout next to the old one, plus what `install` writes (INST-03).

## Alternatives considered

* **A system-wide helper script** (say `/usr/local/bin/dyness-b3-bms-mqtt-upgrade`) that
  needs no checkout. Rejected: another installed path and root-owned script to keep in
  INST-03 and uninstall, for no gain while the README already keeps a checkout around.
* **`/releases/latest`.** Rejected: GitHub's "latest" excludes prereleases, and every
  release is a prerelease until 1.0 (INST-18), so it returns 404 today.
* **Upgrade in place** (unpack over the existing checkout). Rejected: the directory would
  keep the old version in its name, and a failed unpack would leave a mixed tree.
* **python3 `json`** for the response. Rejected for the dependency reason in step 3.

## Spec changes

* `10-installation.md`: INST-19 (new `### upgrade` section); INST-02 lists `upgrade`;
  INST-03 notes upgrade's only extra write; §6's upgrade line names `sudo make upgrade`.
* README quick start: the same upgrade line (TEST checks it against §6).
* `00-overview.md`: the `10-installation.md` index row mentions `make upgrade`.

## Tests

`tests/packaging/test_makefile.py`, all `INST-19`: a copy of the Makefile in a temporary
`dyness-b3-bms-mqtt-0.1.2` checkout, fake `curl` replying with canned API JSON and, through
`Sandbox.script_when`, a generated archive whose `Makefile` has a stub `install`. Covered:
newest-tag resolution (compact and pretty JSON, with a decoy in the body), `VERSION`
override, malformed tags, the "already at" short-circuit, refusal for an existing
directory, file or link, API, download and unpack failures, a missing `curl`, an archive
with the wrong layout, the root check and the dry run.

## Risks

* **GitHub API rate limits.** Unauthenticated requests are limited to 60 an hour per IP;
  past it the response has no `tag_name` and `upgrade` says so. `VERSION` avoids the API.
* **Response format.** The grep relies on GitHub serialising `"tag_name": "…"` as a string
  key of the release; a format change shows as "no tag_name", not a wrong upgrade.
* **Old checkouts pile up.** `upgrade` only says the old one can be removed; it never
  deletes a directory it did not create.
