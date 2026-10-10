# dyness-b3-bms-mqtt — `recover` passes `BBB/DDD` to `usbreset`

* **Date:** 2026-10-10
* **Status:** accepted
* **Scope:** step 3 of the USB reset sequence in the `recover` root helper
  (`packaging/recover`, SVC-18)

## Hardware observation

On the target Pi Zero 2 W (Debian Trixie, usbutils `1:018-2`, release 0.3.0), a forced
exit 76 ran the SVC-18 USB reset. Step 3 logged:

```
recover: usbreset /dev/bus/usb/001/005: failed with status 1
recover: No such device found
```

Step 4 (writing `0` and then `1` to `authorized`) then brought the adapter back, `can-up`
configured `can0`, and the service resumed.

## Cause

usbutils' `usbreset` does not take a device-node path. Its usage is:

```
usbreset VVVV:PPPP - reset by vendor and product id
usbreset BBB/DDD   - reset by bus and device number
usbreset SN:SERIAL - reset by serial number
usbreset "Product" - reset by product name
```

`/dev/bus/usb/001/005` matches none of these, so it is treated as a product name, matches no
device, and exits 1. As written, the primary method of step 3 could never succeed; every
reset on a system with usbutils installed fell through to step 4.

## Decision

`recover` forms the zero-padded address `BBB/DDD` from the device's `busnum` and `devnum`
and runs `usbreset BBB/DDD`. The python `USBDEVFS_RESET` fallback, used when `usbreset` is
not on `PATH`, still opens the device node, now built as `$DEV_ROOT/bus/usb/BBB/DDD` from the
same address. The log line follows from `run`, so it becomes `usbreset 001/005: ok` (or
`failed with status N`).

* **Bus/device rather than `VVVV:PPPP`.** The device is already resolved, from the netdev or
  the `CAN_USB_ID` scan (steps 1–2), so its address names exactly that device. A
  vendor:product ID would pick whichever matching device `usbreset` finds first.

## Fallback order is proven

The same run shows the step 3 → step 4 fallback working on hardware: a failed step 3 led to
step 4, which recovered the adapter. The order and the alternatives in SVC-18 are unchanged.

## Spec changes

* `09-service.md` SVC-18 step 3 runs `usbreset BBB/DDD` and says why: usbutils accepts a
  bus/device number, not a device-node path. The python fallback still uses
  `/dev/bus/usb/BBB/DDD`. The lead `MUST` and the step order are unchanged.

## Tests

`tests/packaging/test_recover.py` expects the fake `usbreset` to be called with exactly
`001/004` (or the chosen device's address in the fallback-scan cases), not a `/dev` path.
`test_recover_76_usb_reset_fallback_order` and `test_recover_fallback_scan_by_usb_id` (SVC-18)
failed against the old script and pass after the change.
`test_recover_76_python_ioctl_without_usbreset` still expects the device node.

## Risks

* A `usbreset` that took only a device node would now fail at step 3. Steps 4 and 5 still
  cover it, as they covered the old argument on current usbutils.
