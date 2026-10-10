"""Print a snapshot of everything a dyness-b3-bms-mqtt deployment publishes under its base topic.

Used by the verify-mqtt Claude Code skill during hardware acceptance. Reads the broker settings
from MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_TLS and MQTT_BASE_TOPIC (empty
means unset; defaults as in docs/specs/08-configuration.md), subscribes to ``<base>/#`` for
``--seconds`` and prints, per topic, the message count, the retained flag, the last payload as
compact sorted JSON and the age of its ``ts``. Credentials are never printed.

Exit status: 0 with at least one message, 1 when the broker cannot be reached or refuses the
connection, 2 for a usage or configuration error, 3 when nothing was received.
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any, NoReturn

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from paho.mqtt.reasoncodes import ReasonCode

TRUE = {"1", "true", "yes", "on"}
FALSE = {"0", "false", "no", "off"}


def env(name: str, default: str | None = None) -> str | None:
    """Return the environment variable ``name``, treating an empty value as unset."""
    return os.environ.get(name) or default


def fail(code: int, message: str) -> NoReturn:
    """Print ``message`` to stderr and exit with ``code``."""
    print(f"mqtt_snapshot: {message}", file=sys.stderr)
    sys.exit(code)


def render(payload: bytes) -> tuple[str, str]:
    """Return the payload as compact sorted JSON (or text) and the age of its ``ts``."""
    text = payload.decode("utf-8", errors="replace")
    try:
        data: Any = json.loads(text)
    except ValueError:
        return text, "-"
    age = "-"
    if isinstance(data, dict) and isinstance(data.get("ts"), str):
        try:
            ts = datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
            age = f"{(datetime.now(UTC) - ts).total_seconds():.1f}s"
        except ValueError:
            age = "unparsable"
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False), age


def main() -> None:
    """Connect, collect for the requested time and print the snapshot."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--seconds", type=float, default=30.0, help="collect time (default 30)")
    parser.add_argument("--timeout", type=float, default=10.0, help="connect timeout (default 10)")
    args = parser.parse_args()

    host = env("MQTT_HOST", "localhost") or "localhost"
    base = env("MQTT_BASE_TOPIC", "dyness_b3") or "dyness_b3"
    tls_raw = (env("MQTT_TLS", "false") or "false").lower()
    if tls_raw not in TRUE | FALSE:
        fail(2, f"MQTT_TLS must be one of 1/0, true/false, yes/no, on/off, not {tls_raw!r}")
    try:
        port = int(env("MQTT_PORT", "1883") or "1883")
    except ValueError:
        fail(2, "MQTT_PORT must be an integer")

    client = mqtt.Client(
        CallbackAPIVersion.VERSION2,
        client_id=f"verify-mqtt-{os.getpid()}",
        protocol=MQTTProtocolVersion.MQTTv311,
    )
    username = env("MQTT_USERNAME")
    if username:
        client.username_pw_set(username, env("MQTT_PASSWORD"))
    if tls_raw in TRUE:
        client.tls_set()

    lock = threading.Lock()
    connected = threading.Event()
    refused: list[str] = []
    seen: dict[str, tuple[int, bool, bytes]] = {}

    def on_connect(c: mqtt.Client, _u: Any, _f: Any, rc: ReasonCode, _p: Any) -> None:
        if rc.is_failure:
            refused.append(str(rc))
        else:
            c.subscribe(f"{base}/#", qos=1)
        connected.set()

    def on_message(_c: mqtt.Client, _u: Any, msg: mqtt.MQTTMessage) -> None:
        with lock:
            count = seen.get(msg.topic, (0, False, b""))[0]
            seen[msg.topic] = (count + 1, bool(msg.retain), msg.payload)

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(host, port, keepalive=30)
    except OSError as exc:
        fail(1, f"cannot connect to {host}:{port}: {exc}")
    client.loop_start()
    try:
        if not connected.wait(args.timeout):
            fail(1, f"no CONNACK from {host}:{port} within {args.timeout:g}s")
        if refused:
            fail(1, f"broker {host}:{port} refused the connection: {refused[0]}")
        time.sleep(args.seconds)
    finally:
        client.disconnect()
        client.loop_stop()

    print(f"# {host}:{port} tls={tls_raw in TRUE} base={base} window={args.seconds:g}s")
    print(f"# now {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    with lock:
        for topic in sorted(seen):
            count, retained, payload = seen[topic]
            body, age = render(payload)
            print(f"{topic}  count={count} retained={retained} ts_age={age}\n  {body}")
    if not seen:
        fail(3, f"no messages on {base}/# in {args.seconds:g}s")


if __name__ == "__main__":
    main()
