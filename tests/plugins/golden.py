"""Golden files: compare JSON output against files under ``tests/golden/`` (TEST-31, TEST-32).

A test asks for the ``golden`` fixture and calls ``golden(name, actual)``, where ``name`` is
a path relative to ``tests/golden/`` under the session's root directory and ``actual`` is any
JSON-serialisable value. Comparison is on parsed JSON, so formatting and key order in the
file do not matter; a missing or differing file fails the test with a diff.

``pytest --update-golden`` writes ``actual`` instead of comparing, with sorted keys,
2-space indent, literal non-ASCII characters and a trailing newline, so diffs of the
regenerated files are readable in review.
"""

import difflib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

GOLDEN_DIR = Path("tests", "golden")

type Golden = Callable[[str, object], None]


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--update-golden``."""
    group = parser.getgroup("dyness", "dyness-b3-bms-mqtt markers")
    group.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite the golden files under tests/golden/ instead of comparing against them",
    )


def render(value: object) -> str:
    """Return ``value`` as golden-file text: sorted keys, 2-space indent, final newline."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@pytest.fixture
def golden(request: pytest.FixtureRequest) -> Golden:
    """Return ``golden(name, actual)``, which compares or (with ``--update-golden``) writes."""
    root = request.config.rootpath / GOLDEN_DIR
    update = bool(request.config.getoption("--update-golden"))

    def check(name: str, actual: object) -> None:
        path = root / name
        text = render(actual)
        if update:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return
        if not path.is_file():
            pytest.fail(
                f"golden file {path} is missing; run `uv run pytest --update-golden` and review it",
                pytrace=False,
            )
        expected = json.loads(path.read_text(encoding="utf-8"))
        if expected != json.loads(text):
            diff = difflib.unified_diff(
                render(expected).splitlines(),
                text.splitlines(),
                fromfile=f"{name} (golden)",
                tofile=f"{name} (actual)",
                lineterm="",
            )
            pytest.fail(
                f"golden file {path} differs from the actual output; if the change is "
                "intended, run `uv run pytest --update-golden` and review the diff\n"
                + "\n".join(diff),
                pytrace=False,
            )

    return check
