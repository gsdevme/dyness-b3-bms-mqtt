"""Static checks of the suite's shape: which tests need what, verified from the source.

Tests that need a broker live only in ``tests/integration/test_mosquitto.py`` behind the
``mosquitto`` and ``integration`` markers, so the default run collects them but skips them
unless a broker answers (TEST-02, TEST-37). Everywhere else a ``PahoPublisher`` is built
only on an injected client factory and no real paho client is constructed.

Tests that need the real stack live only in ``tests/hardware/`` behind the ``hardware``
marker. They run listen-only unless ``--hardware-poll`` is given, and the only transmission
they may make is ``send_poll``, from a test that takes the ``poll_allowed`` fixture, which
skips without that option (TEST-38).
"""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
MOSQUITTO_SUITE = TESTS / "integration" / "test_mosquitto.py"
TEST_37_TESTS = {
    "test_retained_discovery_and_availability",
    "test_lwt_on_abrupt_close",
    "test_explicit_offline_on_stop",
    "test_reconnect_after_drop",
}


def marker_names(decorators: list[ast.expr]) -> set[str]:
    """Return the ``pytest.mark.<name>`` names among ``decorators`` (called or bare)."""
    names = set()
    for decorator in decorators:
        node = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
        ):
            names.add(node.attr)
    return names


def module_markers(tree: ast.Module) -> set[str]:
    """Return the marker names of a module-level ``pytestmark`` list or single marker."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
        ):
            value = node.value
            items = value.elts if isinstance(value, ast.List | ast.Tuple) else [value]
            return marker_names(list(items))
    return set()


def iter_test_functions(tree: ast.Module) -> Iterator[ast.FunctionDef]:
    """Yield the module-level test functions of ``tree``."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            yield node


def calls(tree: ast.Module) -> Iterator[ast.Call]:
    """Yield every call expression in ``tree``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def call_name(call: ast.Call) -> str | None:
    """Return the called name: ``f`` for ``f(...)`` and ``g`` for ``x.g(...)``."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def iter_test_files() -> Iterator[Path]:
    """Yield every test module of the suite."""
    yield from sorted(TESTS.rglob("test_*.py"))


@pytest.mark.req("TEST-37", "TEST-02")
def test_mosquitto_suite_shape() -> None:
    tree = ast.parse(MOSQUITTO_SUITE.read_text(encoding="utf-8"))
    shared = module_markers(tree)
    assert {"mosquitto", "integration"} <= shared
    names = set()
    for function in iter_test_functions(tree):
        names.add(function.name)
        assert "hardware" not in shared | marker_names(function.decorator_list)
    assert names == TEST_37_TESTS
    source = MOSQUITTO_SUITE.read_text(encoding="utf-8")
    assert "PahoPublisher(" in source
    assert "FakePublisher" not in source

    for path in iter_test_files():
        if path == MOSQUITTO_SUITE:
            continue
        module = ast.parse(path.read_text(encoding="utf-8"))
        module_level = module_markers(module)
        for function in iter_test_functions(module):
            markers = module_level | marker_names(function.decorator_list)
            assert "mosquitto" not in markers, f"{path.name}::{function.name}"
        for call in calls(module):
            name = call_name(call)
            assert name != "Client", f"{path.name}:{call.lineno} builds a real paho client"
            if name == "PahoPublisher":
                keywords = {keyword.arg for keyword in call.keywords}
                assert "client_factory" in keywords, f"{path.name}:{call.lineno}"


HARDWARE_DIR = TESTS / "hardware"
POLL_GATE = "poll_allowed"
TRANSMITTING = {"send", "send_poll", "send_periodic", "Message"}


def nested_calls(node: ast.AST) -> Iterator[ast.Call]:
    """Yield every call expression under ``node``."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            yield child


@pytest.mark.req("TEST-38", "TEST-02")
def test_hardware_suite_shape() -> None:
    files = sorted(HARDWARE_DIR.glob("test_*.py"))
    assert files
    for path in files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        shared = module_markers(tree)
        assert "hardware" in shared, path.name
        assert '"--hardware-poll"' in source and '"CAN_LISTEN_ONLY"' in source, path.name
        functions = list(iter_test_functions(tree))
        assert functions, path.name
        for function in functions:
            markers = shared | marker_names(function.decorator_list)
            assert not {"integration", "mosquitto"} & markers, function.name
            sends = [c for c in nested_calls(function) if call_name(c) in TRANSMITTING]
            assert {call_name(c) for c in sends} <= {"send_poll"}, function.name
            if sends:
                assert POLL_GATE in {a.arg for a in function.args.args}, function.name
        outside = [
            c.lineno for c in nested_calls(tree) if call_name(c) in TRANSMITTING - {"send_poll"}
        ]
        assert outside == [], path.name

    for path in iter_test_files():
        if path.parent == HARDWARE_DIR:
            continue
        module = ast.parse(path.read_text(encoding="utf-8"))
        module_level = module_markers(module)
        for function in iter_test_functions(module):
            markers = module_level | marker_names(function.decorator_list)
            assert "hardware" not in markers, f"{path.name}::{function.name}"
