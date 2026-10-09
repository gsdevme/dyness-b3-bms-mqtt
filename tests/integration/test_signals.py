"""The real console entry point stops cleanly on SIGTERM, SIGINT and SIGHUP (CAN-25, SVC-11).

Each test starts ``dyness-b3-bms-mqtt`` as a subprocess on python-can's ``virtual``
interface with a private state directory, a fake ``NOTIFY_SOCKET`` and a broker address
nothing listens on. Once the service has sent ``READY=1`` the test sends one signal and
expects exit status 0 within the SVC-12 budget, with the bus shut down on the way out
(observable as the transport's ``can bus shut down`` log line).
"""

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.fakes.notify import NotifySocket

pytestmark = pytest.mark.integration

READY_TIMEOUT_S = 6.0
STOP_TIMEOUT_S = 3.0


def entry_point() -> list[str]:
    """Return the command of the installed console script, or ``python -m`` without one."""
    script = shutil.which("dyness-b3-bms-mqtt", path=str(Path(sys.executable).parent))
    return [script] if script else [sys.executable, "-m", "dyness_b3_bms_mqtt"]


def closed_port() -> int:
    """Return a local TCP port that nothing is listening on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def wait_for(condition: Callable[[], bool], timeout: float) -> bool:
    """Poll ``condition`` every 20 ms until it holds or ``timeout`` passes."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


@pytest.mark.parametrize(
    "sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP], ids=lambda sig: sig.name
)
@pytest.mark.req("CAN-25", "CAN-24", "SVC-11", "SVC-12", "SVC-06", "SVC-09", "HEALTH-27")
def test_signal_clean_stop(
    sig: signal.Signals, tmp_path: Path, notify_socket: NotifySocket
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "CAN_INTERFACE": "virtual",
        "CAN_CHANNEL": f"signals-{uuid.uuid4().hex[:8]}",
        "MQTT_HOST": "127.0.0.1",
        "MQTT_PORT": str(closed_port()),
        "STATE_DIRECTORY": str(state),
        "NOTIFY_SOCKET": notify_socket.path,
    }
    process = subprocess.Popen(
        entry_point(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert wait_for(lambda: "READY=1" in notify_socket.messages(), READY_TIMEOUT_S)
        process.send_signal(sig)
        sent = time.monotonic()
        output, _ = process.communicate(timeout=STOP_TIMEOUT_S)
        elapsed = time.monotonic() - sent
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0, output
    assert elapsed < STOP_TIMEOUT_S
    lines = output.splitlines()
    assert f"INFO stopping on {sig.name}: exit status 0" in lines
    assert "INFO can bus shut down" in lines
    assert lines.index("INFO can bus shut down") > lines.index(
        f"INFO stopping on {sig.name}: exit status 0"
    )
    assert notify_socket.messages()[-1] == "STOPPING=1"
    assert not any(line.startswith(("ERROR", "Traceback")) for line in lines)
