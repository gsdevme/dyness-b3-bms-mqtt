# dyness-b3-bms-mqtt — self-contained protocol reference

* **Date:** 2026-10-10
* **Status:** accepted
* **Scope:** removing every live dependency on the dyness-debug repository from the living
  specs, the README, `CLAUDE.md` and the test vectors, and making `01-protocol.md` the
  protocol authority for this project

The protocol was reverse-engineered and confirmed on the real stack in a separate
diagnostic effort, the dyness-debug repository. That repository is private while this one is
public. The living specs and `CLAUDE.md` still named `../dyness-debug/docs/protocol.md` as
the protocol authority and cited its decoders, logger, findings and capture paths, so a
reader of this repository was pointed at files they cannot open, and a contributor without a
local checkout could not follow the authority rule. The spec reconciliation of the same day
flagged this as a follow-up.

## Decision

`docs/specs/01-protocol.md` is the protocol authority for this repository. Its untagged
facts were confirmed on real hardware on 8–9 October 2026. Hardware results still win over
the spec: when the stack behaves differently, the spec is updated to match. dyness-debug is
mentioned only as provenance, named as private, without a path.

## What changed

* **`CLAUDE.md`**: the authority rule now names `docs/specs/01-protocol.md` and says hardware
  results win over it. The `../dyness-debug` reference is gone.
* **`README.md`**: one provenance sentence says the project grew out of a private diagnostic
  repository and that `01-protocol.md` and `tests/vectors/` carry what it needs. The
  "Credits" section, which linked the private repository, is removed.
* **`00-overview.md`**: the introduction and §4.3 no longer cite `../dyness-debug`. §6
  "Source material" describes provenance (the October 2026 diagnostic effort) and points at
  `01-protocol.md` and the vendored captures; the local-checkout note and the paths to the
  protocol document, decoders, logger, captures, `SPEC.md` and `findings.md` are removed.
* **`01-protocol.md`**: the introduction states that this document is the authority and that
  hardware results win. §3.2 names the flag bits after "the reference decoder used during
  diagnosis" instead of a file path. The §5 overnight cell-stream row says the all-night
  stream was observed during overnight listen-only logging on 8–9 October 2026 instead of
  citing the authority's findings file. §7 names the captures as vendored under
  `tests/vectors/captures/`.
* **`02-can-transport.md`** (CAN-05, CAN-16), **`03-health-and-recovery.md`**,
  **`07-home-assistant.md`** (HA-03), **`09-service.md`** (SVC shutdown order) and
  **`12-local-logging.md`**: path citations replaced with self-contained wording or a
  pointer to the spec section that carries the fact.
* **`11-testing.md`**: TEST-14 keeps its MUST NOT on reading paths outside the repository
  without naming dyness-debug. TEST-15 names the header label
  `hardware-capture/2026-10-08/` for the vendored captures. TEST-16 writes expected values
  from `01-protocol.md` and its §7 worked examples. The simulator introduction and the
  hardware acceptance row no longer cite the private repository.
* **Test vectors**: the `# source:` header of `t1-examples.txt`, `t2-examples.txt` and the
  two files under `captures/` now reads `hardware-capture/2026-10-08/<file>`, and
  `tests/vectors/loader.py` uses the same `SOURCE_PREFIX` and docstring wording. Capture
  file names and dates are kept; no frame line or expected value changed.

## What did not change

* No protocol fact changed. Every layout, scale, sign convention, classification rule and
  **UNVERIFIED** tag in `01-protocol.md` is as before; only citations were reworded.
* Tests already used the vendored captures under `tests/vectors/` and never read the sibling
  repository (TEST-14 has a guard test for that). No requirement ID was added or removed.
* Earlier dated design records and plans keep their `../dyness-debug` references as
  history.

## Risks

* Facts that lived only in the private repository and were never restated here are no
  longer reachable from this repository. The reconciliation audit found `01-protocol.md`
  already carries every fact the code relies on, so nothing the service needs is lost.
