"""Static transmit-safety check over the package source (PROTO-30, CAN-19).

The only ``can.Message`` construction and the only python-can ``send`` call in
``src/dyness_b3_bms_mqtt`` must be inside ``PythonCanTransport.send_poll`` in ``canbus.py``.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "dyness_b3_bms_mqtt"
ALLOWED = ("canbus.py", "PythonCanTransport", "send_poll")


def _callee(call: ast.Call) -> str | None:
    """Return ``Message`` or ``send`` if the call constructs a message or sends one."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name if name in ("Message", "send") else None


def transmit_sites(path: Path) -> list[tuple[str, str, str, int]]:
    """Return ``(file, class, function, line)`` for every message construction or send."""
    sites: list[tuple[str, str, str, int]] = []

    def visit(node: ast.AST, owner: str, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name, function)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                visit(child, owner, getattr(child, "name", "<lambda>"))
            else:
                if isinstance(child, ast.Call) and _callee(child):
                    sites.append((path.name, owner, function, child.lineno))
                visit(child, owner, function)

    visit(ast.parse(path.read_text(encoding="utf-8")), "", "")
    return sites


@pytest.mark.req("PROTO-30", "CAN-19")
def test_only_send_poll_constructs_or_sends() -> None:
    sites = [site for path in sorted(SRC.rglob("*.py")) for site in transmit_sites(path)]
    assert [site[:3] for site in sites] == [ALLOWED, ALLOWED]
    imports = [
        node
        for path in sorted(SRC.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
        and node.module == "can"
        and any(alias.name == "Message" for alias in node.names)
    ]
    assert imports == []


@pytest.mark.req("PROTO-30")
def test_static_check_detects_violations(tmp_path: Path) -> None:
    source = tmp_path / "rogue.py"
    source.write_text(
        "import can\n"
        "class Rogue:\n"
        "    def send_poll(self, bus):\n"
        "        bus.send(can.Message(arbitration_id=0x18F26677))\n"
        "def helper(bus, m):\n"
        "    f = lambda: bus.send(m)\n"
        "    return Message(arbitration_id=1)\n",
        encoding="utf-8",
    )
    assert [site[1:] for site in transmit_sites(source)] == [
        ("Rogue", "send_poll", 4),
        ("Rogue", "send_poll", 4),
        ("", "<lambda>", 6),
        ("", "helper", 7),
    ]
