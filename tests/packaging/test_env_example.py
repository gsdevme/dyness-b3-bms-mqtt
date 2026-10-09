"""The example environment file lists every variable with its default (CFG-08)."""

import re

import pytest

from dyness_b3_bms_mqtt.config import DEFAULTS, VARIABLES
from tests.packaging.helpers import PACKAGING
from tests.support.spec import spec_text, table_rows

EXAMPLE = PACKAGING / "dyness-b3-bms-mqtt.env"
COMMENTED = re.compile(r"^#([A-Z][A-Z0-9_]*)=(.*)$")


def spec_variables() -> list[str]:
    """Return the variable names of every table in ``08-configuration.md``, in order."""
    rows = table_rows(spec_text("08-configuration.md").splitlines())
    return [re.findall(r"`([^`]+)`", cells[0])[0] for cells in rows]


@pytest.mark.req("CFG-08")
def test_env_example_lists_every_variable() -> None:
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    entries = [
        (index, match.group(1), match.group(2))
        for index, line in enumerate(lines)
        if (match := COMMENTED.match(line))
    ]
    assert [name for _, name, _ in entries] == list(VARIABLES) == spec_variables()
    descriptions = {}
    for index, name, value in entries:
        default = DEFAULTS[name]
        assert value == ("" if default is None else default), name
        description = lines[index - 1]
        assert description.startswith("# "), name
        assert description[2:].strip(), name
        assert not COMMENTED.match(description), name
        descriptions[name] = description
    assert "dyness-b3-bms-mqtt-<hostname>" in descriptions["MQTT_CLIENT_ID"]
    for line in lines:
        assert not line or line.startswith("#"), f"uncommented line: {line!r}"
