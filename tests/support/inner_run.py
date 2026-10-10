"""Set up a throwaway pytest run configured like the project, for the harness's own tests.

The inner run gets the project's registered markers, ``--strict-markers``,
``--strict-config`` and ``-ra``, imports ``tests.*`` from this checkout, and loads the
given plugins. ``MQTT_HOST`` and ``MQTT_PORT`` are cleared so the outer environment (for
example the CI ``mqtt`` job's) cannot change the inner run; a test that needs them sets
them itself.
"""

import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def configure_inner_run(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    plugins: Sequence[str],
    *,
    conftest: str = "",
) -> dict[str, object]:
    """Write the inner ``pytest.ini`` and ``conftest.py``; return the project's ini options.

    ``conftest`` is appended to the generated ``conftest.py`` after ``pytest_plugins``; code
    there that changes a plugin's module constants needs ``runpytest_subprocess``, or the
    change leaks into the outer run. The suite-wide ``timeout`` is copied from
    ``pyproject.toml``.
    """
    with (ROOT / "pyproject.toml").open("rb") as handle:
        ini: dict[str, object] = tomllib.load(handle)["tool"]["pytest"]["ini_options"]
    markers = ini["markers"]
    assert isinstance(markers, list)
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    for name in ("MQTT_HOST", "MQTT_PORT"):
        monkeypatch.delenv(name, raising=False)
    pytester.makeini(
        "[pytest]\n"
        "addopts = --strict-markers --strict-config -ra\n"
        f"timeout = {ini['timeout']}\n"
        "markers =\n" + "".join(f"    {marker}\n" for marker in markers)
    )
    pytester.makeconftest(f"pytest_plugins = {list(plugins)!r}\n{conftest}")
    return ini
