"""Build validated ``Settings`` for tests without touching the real environment."""

from dyness_b3_bms_mqtt.config import Settings, load_settings


def make_settings(**env: str) -> Settings:
    """Return settings for a virtual test bus, overridden by ``env``.

    ``CAN_INTERFACE`` defaults to ``virtual`` and ``CAN_CHANNEL`` to ``test`` so that no
    test can reach a real adapter by accident; the hostname is fixed to ``testhost``.
    """
    return load_settings(
        {"CAN_INTERFACE": "virtual", "CAN_CHANNEL": "test", **env},
        hostname=lambda: "testhost",
    )
