"""Tests of the golden-file plugin (``tests/plugins/golden.py``, TEST-31, TEST-32).

Each case runs a throwaway pytest session in a subprocess whose root directory is the
pytester directory, so the plugin reads and writes ``tests/golden/`` there and never
touches the project's own golden files.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

USES_GOLDEN = """
def test_compares(golden):
    golden("sample/thing.json", {"b": [1, 2], "a": "\N{MULTIPLICATION SIGN}"})
"""


@pytest.fixture
def harness(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """Return a pytester directory that loads only the golden plugin."""
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    pytester.makeini("[pytest]\naddopts = --strict-markers --strict-config -ra\n")
    pytester.makeconftest('pytest_plugins = ["tests.plugins.golden"]\n')
    pytester.makepyfile(USES_GOLDEN)
    return pytester


def golden_path(harness: pytest.Pytester) -> Path:
    return harness.path / "tests" / "golden" / "sample" / "thing.json"


@pytest.mark.req("TEST-32")
def test_missing_golden_fails(harness: pytest.Pytester) -> None:
    result = harness.runpytest_subprocess()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*golden file*sample/thing.json*missing*--update-golden*"])
    assert not golden_path(harness).exists()


@pytest.mark.req("TEST-31", "TEST-32")
def test_differing_golden_fails(harness: pytest.Pytester) -> None:
    path = golden_path(harness)
    path.parent.mkdir(parents=True)
    path.write_text('{"a": "\N{MULTIPLICATION SIGN}", "b": [1, 3]}\n', encoding="utf-8")
    result = harness.runpytest_subprocess()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*golden file*sample/thing.json*differs*", "*-    3*", "*+    2*"])
    assert path.read_text(encoding="utf-8") == '{"a": "\N{MULTIPLICATION SIGN}", "b": [1, 3]}\n'

    path.write_text('{\n"b": [1, 2],   "a": "\\u00d7"}', encoding="utf-8")
    harness.runpytest_subprocess().assert_outcomes(passed=1)


@pytest.mark.req("TEST-31", "TEST-32")
def test_update_golden_writes(harness: pytest.Pytester) -> None:
    path = golden_path(harness)
    harness.runpytest_subprocess("--update-golden").assert_outcomes(passed=1)
    expected = '{\n  "a": "\N{MULTIPLICATION SIGN}",\n  "b": [\n    1,\n    2\n  ]\n}\n'
    assert path.read_text(encoding="utf-8") == expected
    harness.runpytest_subprocess().assert_outcomes(passed=1)

    path.write_text('{"stale": true}\n', encoding="utf-8")
    harness.runpytest_subprocess("--update-golden").assert_outcomes(passed=1)
    assert path.read_text(encoding="utf-8") == expected
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "a": "\N{MULTIPLICATION SIGN}",
        "b": [1, 2],
    }
