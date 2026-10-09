"""The shipped systemd unit and udev rule are byte-for-byte the blocks in ``09-service.md``."""

import pytest

from tests.packaging.helpers import PACKAGING
from tests.support.spec import spec_text


def fenced_block(name: str, ident: str, language: str) -> str:
    """Return the first fenced ``language`` block after requirement ``ident`` in spec ``name``.

    The block's lines are returned with a final newline, as the shipped file must end.
    """
    lines = spec_text(name).splitlines()
    start = next(i for i, line in enumerate(lines) if f"**{ident}**" in line)
    opening = lines.index(f"```{language}", start)
    closing = lines.index("```", opening + 1)
    return "\n".join(lines[opening + 1 : closing]) + "\n"


@pytest.mark.req("SVC-01")
def test_unit_file_verbatim() -> None:
    expected = fenced_block("09-service.md", "SVC-01", "ini")
    assert expected.startswith("[Unit]\n")
    assert (PACKAGING / "dyness-b3-bms-mqtt.service").read_bytes() == expected.encode()


@pytest.mark.req("SVC-20")
def test_udev_rule_verbatim() -> None:
    expected = fenced_block("09-service.md", "SVC-20", "udev")
    assert 'RUN+="/usr/local/lib/dyness-b3-bms-mqtt/can-up $name"' in expected
    assert (PACKAGING / "80-dyness-b3-can.rules").read_bytes() == expected.encode()
