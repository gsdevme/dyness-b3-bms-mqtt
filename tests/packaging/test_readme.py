"""The README's quick start is the one ``10-installation.md`` §6 promises, byte for byte."""

from pathlib import Path

import pytest

from tests.support.spec import spec_text

ROOT = Path(__file__).resolve().parents[2]


def promised_block() -> str:
    """Return §6's quick-start code block and its upgrade and removal line, as one text."""
    text = spec_text("10-installation.md")
    body = text[text.index("## 6. README promises") :]
    start = body.index("```sh\n")
    end = body.index("\n* **INST-15**")
    return body[start:end]


def quick_start_section() -> str:
    """Return the body of the README's ``## Quick start`` section, up to the next heading."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index("## Quick start\n") + len("## Quick start\n")
    end = text.index("\n## ", start)
    return text[start:end]


@pytest.mark.req("INST-06", "INST-08", "INST-09")
def test_readme_quick_start_exact() -> None:
    block = promised_block()
    assert block.startswith("```sh\nsudo apt-get install -y curl make\n")
    assert block.endswith("to also remove\nconfiguration and state.\n")
    section = quick_start_section()
    assert block in section
    assert section.count("```") == 2
