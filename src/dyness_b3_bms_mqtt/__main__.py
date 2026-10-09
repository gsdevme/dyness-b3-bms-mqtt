"""``python -m dyness_b3_bms_mqtt``: the same command as the ``dyness-b3-bms-mqtt`` script."""

import sys

from dyness_b3_bms_mqtt.app import main

if __name__ == "__main__":
    sys.exit(main())
