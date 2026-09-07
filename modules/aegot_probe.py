#!/usr/bin/env python3
"""
Passive probe for the Amazon "AEOOT" stage (0e8d:2008).

FACTFACT on this Echo Studio drops the preloader into 0e8d:2008
("AEOOT"), not classic fastboot. This observer answers ONE question:
does that stage speak the same READY-stream tool protocol, so further
mode requests might be accepted from inside it?

Strictly passive:
  - sniffs the kernel ttyUSB device for a READY preamble stream
  - watches the bulk IN pipe directly
  - issues ONLY `fastboot devices` (USB enumeration; sends no command
    to the device itself)
  - writes ZERO bytes to the device

Run:  python3 modules/aegot_probe.py [--seconds 20]
Waits for 0e8d:2008, observes, prints a verdict.
"""
import argparse
import glob
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usb.core        # noqa: E402
import usb.util        # noqa: E402

VENDOR = 0x0E8D
AEGOOT_PID = 0x2008


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def tty_for_2008():
    """Return a /dev/ttyUSB* whose parent USB device is 0e8d:2008."""
    for tty in sorted(glob.glob("/sys/bus/usb-serial/devices/ttyUSB*")):
        try:
            node = os.path.realpath(tty)          # .../1-2.1:1.0/ttyUSB0
            usbdev = node.split("/")[6] if len(node.split("/")) > 6 else ""
            idv = f"/sys/bus/usb/devices/{usbdev}/idVendor"
            idp = f"/sys/bus/usb/devices/{usbdev}/idProduct"
            with open(idv) as f:
                v = f.read().strip()
            with open(idp) as f:
                p = f.read().strip()
            if v == "0e8d" and p == "2008":
                return "/dev/" + os.path.basename(tty)
        except OSError:
            continue
    return None


def wait_2008(seconds=60):
    log(f"waiting up to {seconds}s for 0e8d:{AEGOOT_PID:04x} (power-cycle now)")
    end = time.time() + seconds
    while time.time() < end:
        dev = usb.core.find(idVendor=VENDOR, idProduct=AEGOOT_PID)
        if dev is not None:
            return dev
        time.sleep(0.2)
    return None


def sniff_tty(path, seconds):
    """Read whatever the kernel serial driver is already receiving."""
    log(f"sniffing {path} for {seconds}s (no bytes written)")
    buf = bytearray()
    t0 = time.time()
    try:
        import termios
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        attrs = termios.tcgetattr(fd)
        attrs[4] = termios.B115200            # try a common default
        attrs[3] &= ~(termios.CRTSCTS | termios.CLOCAL | termios.CREAD)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except OSError as exc:
        log(f"  cannot open {path}: {exc}")
        return b""
    try:
        while time.time() - t0 < seconds:
            try:
                chunk = os.read(fd, 512)
                if chunk:
                    buf.extend(chunk)
            except BlockingIOError:
                time.sleep(0.05)
            except OSError:
                break
    finally:
        os.close(fd)
    return bytes(buf)


def sniff_bulk(dev, seconds):
    """Watch the bulk IN pipe directly for READY streams / any traffic."""
    log(f"watching bulk IN for {seconds}s (read-only)")
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]
    in_ep = None
    for ep in intf:
        if usb.util.endpoint_direction(ep.bEndpointAddress) == \
                usb.util.ENDPOINT_IN:
            in_ep = ep.bEndpointAddress
    if in_ep is None:
        log("  interface has no IN endpoint")
        return b""
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except usb.core.USBError:
        pass
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    usb.util.claim_interface(dev, 0)
    seen = bytearray()
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            try:
                data = dev.read(in_ep, 64, timeout=500)
                seen.extend(bytes(data))
                if len(seen) > 512:
                    break
            except usb.core.USBError as exc:
                if exc.errno not in (110,):
                    log(f"  bulk IN error: {exc}")
                    break
    finally:
        usb.util.release_interface(dev, 0)
    return bytes(seen)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=20)
    args = ap.parse_args()

    dev = wait_2008()
    if dev is None:
        print("verdict: 0e8d:2008 never appeared")
        return 1
    time.sleep(0.3)
    log("0e8d:2008 present")

    tty = tty_for_2008()
    passive = b""
    if tty:
        passive = sniff_tty(tty, min(6, args.seconds))
    else:
        log("no kernel tty bound to 2008 this time")

    bulk = sniff_bulk(dev, args.seconds)

    out = bytearray()
    for data in (passive, bulk):
        if data:
            out.extend(data[:128])
    log(f"observed {len(passive)} tty bytes, {len(bulk)} bulk bytes")
    if out:
        log("first bytes: " + " ".join(f"{b:02x}" for b in out[:64]))
    printable = bytes(b for b in out if 32 <= b < 127)
    if printable:
        log(f"printable: {printable[:80]!r}")

    fast = subprocess.run(["fastboot", "devices"], capture_output=True,
                          text=True, timeout=10).stdout.strip()
    log(f"fastboot devices: {fast!r}")

    if b"READY" in out:
        print("verdict: AEOOT speaks the READY-stream tool protocol -> "
              "it may accept further mode requests; next step is "
              "protocol analysis, NOT more command guessing")
    elif fast:
        print("verdict: fastboot-visible after all")
    elif out:
        print("verdict: unknown byte stream captured -- send this trace "
              "for analysis")
    else:
        print("verdict: stage is silent (no tty data, no bulk data, no "
              "fastboot)")
    return 0


if __name__ == "__main__":
    sys.exit(main())