"""Tests of the requirement traceability plugin (``tests/plugins/reqtrace.py``).

The parser is checked directly against a throwaway spec tree and against the real
``docs/specs/``. The session rules run in a throwaway pytest run whose ``docs/specs/`` holds
one spec file with the prefix ``DEMO``.
"""

from pathlib import Path

import pytest

from tests.plugins.reqtrace import ID_PATTERN, classify, known_prefixes, parse_specs
from tests.support.inner_run import configure_inner_run

ROOT = Path(__file__).resolve().parents[2]

OVERVIEW = """\
# 00 — Overview

### 4.1 Requirement identifiers

| File | Prefix |
|---|---|
| `01-demo.md` | `DEMO` |
| `02-other.md` | `OTHER` |

### 4.2 Requirement language

| `99-not-a-prefix.md` | `NOPE` |
"""

DEMO = """\
# 01 — Demo (`DEMO`)

## 1. Requirements

* **DEMO-01** The service MUST do one.
* **DEMO-02** The service SHOULD do two.
* **DEMO-03** **MANUAL** On the real stack the service MUST do three.
* **DEMO-04** (withdrawn) The service MUST do four.
* **DEMO-05** The service MAY log
  and MUST NOT crash.
"""

PARSE_DEMO = """\
# 01 — Demo (`DEMO`)

## 1. Requirements

* **DEMO-01** The service MUST do one.
* **DEMO-02** The service SHOULD do two; it MAY do more.
* **DEMO-03** **MANUAL** On the real stack the service MUST do three.
* **DEMO-04** (withdrawn) The service MUST do four.
* **DEMO-05** The service MAY log
  and MUST NOT crash.
* **DEMO-06** The service SHOULD do six:

  the service MUST be ignored here, after the blank line.
* **DEMO-07** The service SHOULD do seven.
### A heading that MUST NOT count
* **DEMO-08** The service SHOULD do eight.
| a table row that MUST NOT count |
* **DEMO-09** The service SHOULD do nine.
```text
the fence MUST NOT count
```
* **DEMO-10** The service SHOULD do ten. **DEMO-11** The service MUST do eleven.
* **DEMO-12** The service SHOULD name `MUST` and `**MANUAL**` only in code.

Plain references to DEMO-01 and `**DEMO-20**` in a code span define nothing, nor does
**NOPE-01**, which MUST be ignored because its prefix is not in the table.

```markdown
* **DEMO-21** Fenced text MUST NOT define.
```
"""

OTHER = """\
# 02 — Other (`OTHER`)

* **OTHER-01** The other service SHOULD exist.
"""


def _write_specs(base: Path, **files: str) -> Path:
    spec_dir = base / "docs" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    for name, text in {"00-overview.md": OVERVIEW, **files}.items():
        (spec_dir / name).write_text(text)
    return spec_dir


@pytest.fixture
def harness(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """Return a pytester directory configured like the project, with a DEMO spec tree."""
    configure_inner_run(pytester, monkeypatch, ["tests.plugins.markers", "tests.plugins.reqtrace"])
    _write_specs(pytester.path, **{"01-demo.md": DEMO})
    return pytester


@pytest.mark.req("TEST-05")
def test_parse_classifies_definitions(tmp_path: Path) -> None:
    spec_dir = _write_specs(tmp_path, **{"01-demo.md": PARSE_DEMO, "02-other.md": OTHER})
    assert known_prefixes(spec_dir) == {"DEMO": "01-demo.md", "OTHER": "02-other.md"}

    requirements, duplicates = parse_specs(spec_dir)
    assert duplicates == {}
    assert {ident: req.status for ident, req in requirements.items()} == {
        "DEMO-01": "must",
        "DEMO-02": "should",
        "DEMO-03": "manual",
        "DEMO-04": "withdrawn",
        "DEMO-05": "must",
        "DEMO-06": "should",
        "DEMO-07": "should",
        "DEMO-08": "should",
        "DEMO-09": "should",
        "DEMO-10": "should",
        "DEMO-11": "must",
        "DEMO-12": "should",
        "OTHER-01": "should",
    }
    assert requirements["DEMO-01"].spec == "01-demo.md"
    assert requirements["OTHER-01"].spec == "02-other.md"
    assert all(req.covered_by == [] for req in requirements.values())

    assert classify("The service MUST NOT crash.") == "must"
    assert classify("**MANUAL** It MUST work on the stack.") == "manual"
    assert classify("(withdrawn) **MANUAL** It MUST work.") == "withdrawn"
    assert classify("It SHOULD work; it MAY log.") == "should"
    assert classify("A MUSTARD-coloured LED SHOULD light.") == "should"

    assert ID_PATTERN.pattern == "^[A-Z]+-[0-9]{2}$"
    assert ID_PATTERN.fullmatch("PROTO-01")
    for bad in ("PROTO-1", "PROTO-001", "proto-01", "PROTO01", "PROTO-01 ", "PROTO-01\n", "-01"):
        assert not ID_PATTERN.fullmatch(bad), bad


@pytest.mark.req("TEST-05", "TEST-06")
def test_real_specs_parse_without_duplicates() -> None:
    spec_dir = ROOT / "docs" / "specs"
    assert len(known_prefixes(spec_dir)) == 12
    requirements, duplicates = parse_specs(spec_dir)
    assert duplicates == {}
    assert requirements["PROTO-01"].status == "must"
    assert requirements["PROTO-01"].spec == "01-protocol.md"
    assert requirements["CAN-17"].status == "manual"
    assert requirements["MQTT-22"].status == "should"


@pytest.mark.req("TEST-06", "TEST-09")
def test_covered_run_passes_with_summary(harness: pytest.Pytester) -> None:
    harness.makepyfile(
        """
        import pytest

        @pytest.mark.req("DEMO-01", "DEMO-02")
        def test_one():
            pass

        @pytest.mark.req("DEMO-05", "DEMO-03")
        def test_five():
            pass
        """
    )
    result = harness.runpytest("--req-strict")
    result.assert_outcomes(passed=2)
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["reqtrace: 2 must / 2 covered / 1 manual / 1 should"])
    result.stdout.no_fnmatch_line("*reqtrace: error*")
    result.stdout.no_fnmatch_line("*reqtrace: warning*")


@pytest.mark.req("TEST-06", "TEST-09", "TEST-10")
def test_uncovered_must_fails_when_strict(harness: pytest.Pytester) -> None:
    harness.makepyfile(
        """
        import pytest

        @pytest.mark.req("DEMO-01")
        def test_one():
            pass
        """
    )
    result = harness.runpytest("--req-strict")
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            "reqtrace: error: MUST requirements not covered by any test:",
            "  01-demo.md: DEMO-05",
            "reqtrace: 2 must / 1 covered / 1 manual / 1 should",
        ]
    )


@pytest.mark.req("TEST-06")
def test_uncovered_must_only_warns_without_strict(harness: pytest.Pytester) -> None:
    harness.makepyfile(
        """
        import pytest

        @pytest.mark.req("DEMO-01")
        def test_one():
            pass
        """
    )
    result = harness.runpytest()
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        [
            "reqtrace: warning: 1 MUST requirement not covered (--req-strict not given)*",
            "reqtrace: 2 must / 1 covered / 1 manual / 1 should",
        ]
    )
    result.stdout.no_fnmatch_line("*reqtrace: error*")


@pytest.mark.req("TEST-08", "TEST-10")
def test_narrowed_run_downgrades_rule_1(harness: pytest.Pytester) -> None:
    harness.makepyfile(
        test_a="""
        import pytest

        @pytest.mark.req("DEMO-01")
        def test_one():
            pass
        """,
        test_b="""
        import pytest

        @pytest.mark.integration
        @pytest.mark.req("DEMO-05")
        def test_five():
            pass
        """,
    )
    harness.runpytest("--req-strict").assert_outcomes(passed=2)

    narrowings = (
        ("test_a.py",),
        ("-k", "test_one"),
        ("-m", "not integration"),
        ("--deselect", "test_b.py::test_five"),
    )
    for narrowing in narrowings:
        result = harness.runpytest("--req-strict", *narrowing)
        assert result.ret == pytest.ExitCode.OK, narrowing
        result.stdout.fnmatch_lines(
            ["reqtrace: warning: 1 MUST requirement not covered (narrowed run: *)"]
        )
        result.stdout.no_fnmatch_line("*reqtrace: error*")


@pytest.mark.req("TEST-06", "TEST-08", "TEST-10")
def test_bad_references_fail_even_when_narrowed(harness: pytest.Pytester) -> None:
    harness.makepyfile(
        test_good="""
        import pytest

        @pytest.mark.req("DEMO-01", "DEMO-05")
        def test_good():
            pass
        """,
        test_bad="""
        import pytest

        @pytest.mark.req("DEMO-99")
        def test_unknown():
            pass

        @pytest.mark.req("NOPE-01")
        def test_unknown_prefix():
            pass

        @pytest.mark.req("DEMO-04")
        def test_withdrawn():
            pass

        @pytest.mark.req("DEMO-1", 5)
        def test_malformed():
            pass
        """,
    )
    for narrowing in ((), ("test_bad.py",), ("-k", "test_good")):
        result = harness.runpytest(*narrowing)
        assert result.ret == pytest.ExitCode.TESTS_FAILED, narrowing
        result.stdout.fnmatch_lines(
            [
                "reqtrace: error: req markers naming undefined identifiers:",
                "  01-demo.md: DEMO-99 (test_bad.py::test_unknown)",
                "  (no spec file): NOPE-01 (test_bad.py::test_unknown_prefix)",
                "reqtrace: error: req markers naming withdrawn identifiers:",
                "  01-demo.md: DEMO-04 (test_bad.py::test_withdrawn)",
                "reqtrace: error: malformed req arguments (expected ^[A-Z]+-[0-9]{2}$):",
                "  01-demo.md: 'DEMO-1' (test_bad.py::test_malformed)",
                "  (no spec file): 5 (test_bad.py::test_malformed)",
            ]
        )


@pytest.mark.req("TEST-06", "TEST-10")
def test_duplicate_definition_fails(harness: pytest.Pytester) -> None:
    _write_specs(harness.path, **{"02-other.md": OTHER + "* **DEMO-01** It MUST again.\n"})
    harness.makepyfile(
        """
        import pytest

        @pytest.mark.req("DEMO-01", "DEMO-05")
        def test_one():
            pass
        """
    )
    for narrowing in ((), ("-k", "test_one")):
        result = harness.runpytest(*narrowing)
        assert result.ret == pytest.ExitCode.TESTS_FAILED, narrowing
        result.stdout.fnmatch_lines(
            [
                "reqtrace: error: requirements defined more than once:",
                "  01-demo.md: DEMO-01",
                "  02-other.md: DEMO-01",
            ]
        )


@pytest.mark.req("TEST-07")
def test_hardware_tests_do_not_cover(harness: pytest.Pytester) -> None:
    harness.makepyfile(
        """
        import pytest

        @pytest.mark.req("DEMO-01")
        def test_one():
            pass

        @pytest.mark.hardware
        @pytest.mark.req("DEMO-05")
        def test_on_stack():
            pass

        @pytest.mark.mosquitto
        @pytest.mark.req("DEMO-05")
        def test_on_broker():
            pass
        """
    )
    result = harness.runpytest("--req-strict", "--hardware")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.assert_outcomes(passed=2, deselected=1)
    result.stdout.fnmatch_lines(
        [
            "reqtrace: error: MUST requirements not covered by any test:",
            "  01-demo.md: DEMO-05",
            "reqtrace: 2 must / 1 covered / 1 manual / 1 should",
        ]
    )


@pytest.mark.req("TEST-07")
def test_failing_test_still_covers_and_module_marks_count(harness: pytest.Pytester) -> None:
    harness.makepyfile(
        """
        import pytest

        pytestmark = pytest.mark.req("DEMO-01")

        @pytest.mark.req("DEMO-05")
        class TestFive:
            def test_fails(self):
                assert False
        """
    )
    result = harness.runpytest("--req-strict")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["reqtrace: 2 must / 2 covered / 1 manual / 1 should"])
    result.stdout.no_fnmatch_line("*reqtrace: error*")
    result.stdout.no_fnmatch_line("*reqtrace: warning*")


@pytest.mark.req("TEST-09")
def test_report_prints_matrix_and_manual_list(harness: pytest.Pytester) -> None:
    harness.makepyfile(
        """
        import pytest

        @pytest.mark.req("DEMO-01", "DEMO-02")
        def test_one():
            pass

        @pytest.mark.req("DEMO-01")
        def test_again():
            pass
        """
    )
    result = harness.runpytest("--req-report")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        [
            "*requirement matrix*",
            "01-demo.md:",
            "  DEMO-01  must       test_report_prints_matrix_and_manual_list.py::test_one, "
            "test_report_prints_matrix_and_manual_list.py::test_again",
            "  DEMO-02  should     test_report_prints_matrix_and_manual_list.py::test_one",
            "  DEMO-03  manual     -",
            "  DEMO-04  withdrawn  -",
            "  DEMO-05  must       -",
            "*manual requirements*",
            "  01-demo.md: DEMO-03",
            "reqtrace: 2 must / 1 covered / 1 manual / 1 should",
        ]
    )
    without = harness.runpytest()
    without.stdout.no_fnmatch_line("*requirement matrix*")
    without.stdout.fnmatch_lines(["reqtrace: 2 must / 1 covered / 1 manual / 1 should"])
