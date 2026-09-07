"""Raw byte-level observer for the Amazon preloader tool window (0e8d:2000).

Purpose: settle what the tool pipe actually speaks. Bridge runs 1-3 gave
three different pipe behaviours (clean strict responses; shifted echo;
total EIO) and no more guessing on hardware. This script performs NO
mode arm and NO reset: worst case the preloader simply times out to a
normal boot.

Usage (no sudo; repo root):
    python3 modules/bridge_diag.py              # observe + wdt-disable write
    python3 modules/bridge_diag.py --no-write   # zero writes beyond handshake

Trace is written to bridge-diag.txt (next to the run log if
AMONET_LOG_FILE is set, else the current directory) and echoed to stdout.
"""

import os
import sys
import time

import usb.core
import usb.util

MTK_VID = 0x0E8D
PRELOADER_PID = 0x2000
HANDSHAKE = b"\xA0\x0A\x50\x05"


class Trace:
    def __init__(self, path):
        self.path = path
        self.handle = open(path, "w", encoding="utf-8")

    def __call__(self, text):
        line = "[{:9.3f}] {}".format(time.time() - T0, text)
        print(line, flush=True)
        self.handle.write(line + "\n")
        self.handle.flush()

    def close(self):
        self.handle.close()


T0 = time.time()


def find_preloader(trace, deadline_s=120):
    trace("waiting for 0e8d:2000 (power-cycle the device)...")
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        udev = usb.core.find(idVendor=MTK_VID, idProduct=PRELOADER_PID)
        if udev is not None:
            trace("found device, detaching kernel driver immediately")
            for intf in (0, 1):
                try:
                    if udev.is_kernel_driver_active(intf):
                        udev.detach_kernel_driver(intf)
                except (NotImplementedError, usb.core.USBError) as error:
                    trace("detach intf {}: {}".format(intf, error))
            try:
                udev.set_configuration()
            except usb.core.USBError as error:
                trace("set_configuration: {}".format(error))
            cdc = usb.util.find_descriptor(
                udev.get_active_configuration(), bInterfaceClass=0xA)
            ep_in = usb.util.find_descriptor(
                cdc, custom_match=lambda e:
                usb.util.endpoint_direction(e.bEndpointAddress)
                == usb.util.ENDPOINT_IN)
            ep_out = usb.util.find_descriptor(
                cdc, custom_match=lambda e:
                usb.util.endpoint_direction(e.bEndpointAddress)
                == usb.util.ENDPOINT_OUT)
            trace("ep_in=0x{:02X} (max {}), ep_out=0x{:02X} (max {})".format(
                ep_in.bEndpointAddress, ep_in.wMaxPacketSize,
                ep_out.bEndpointAddress, ep_out.wMaxPacketSize))
            return udev, ep_in, ep_out
        time.sleep(0.1)
    raise SystemExit("device never appeared")


def slurp(ep_in, trace, label, seconds, packet_timeout=500):
    """Read every IN packet for `seconds`; log each one verbatim."""

    trace("--- {}: listening {}s ---".format(label, seconds))
    deadline = time.time() + seconds
    total = 0
    while time.time() < deadline:
        try:
            data = bytes(ep_in.read(ep_in.wMaxPacketSize, packet_timeout))
            total += len(data)
            trace("IN  {}B: {}".format(len(data), data.hex(" ")))
        except usb.core.USBError as error:
            if error.errno == 110:          # timeout = quiet pipe, keep going
                continue
            trace("IN  ERROR: {}".format(error))
            return total, False
    trace("--- {}: {} bytes total ---".format(label, total))
    return total, True


def send(udev, ep_out, trace, label, payload):
    trace("OUT {}: {}".format(label, payload.hex(" ")))
    for attempt in (1, 2):
        try:
            ep_out.write(payload, 2000)
            trace("OUT {}: accepted by host stack".format(label))
            return True
        except usb.core.USBError as error:
            trace("OUT {}: ERROR {} (attempt {})".format(
                label, error, attempt))
            if error.errno == 5 and attempt == 1:
                # EIO on a freshly-detached interface usually means a
                # halted pipe / stale data toggle from the previous
                # session: clear halt on both bulk endpoints and retry.
                for ep in (ep_out,):
                    try:
                        udev.clear_halt(ep)
                        trace("  clear_halt(ep 0x{:02X}) ok".format(
                            ep.bEndpointAddress))
                    except usb.core.USBError as halt_error:
                        trace("  clear_halt: {}".format(halt_error))
            else:
                return False
    return False


def main(argv):
    do_write = "--no-write" not in argv
    out_dir = os.path.dirname(os.path.abspath(
        os.environ.get("AMONET_LOG_FILE", "amonet.log")))
    path = os.path.join(out_dir, "bridge-diag.txt")
    trace = Trace(path)
    try:
        udev, ep_in, ep_out = find_preloader(trace)

        # Phase 1: greeting only. How big is it, what is in it, and does
        # it keep streaming (mirror) after the first burst?
        total, alive = slurp(ep_in, trace, "greeting window", 4.0)

        # Phase 2: handshake lead + bytes, raw responses, no matching.
        if alive:
            send(udev, ep_out, trace, "lead a0", b"\xA0")
            slurp(ep_in, trace, "lead response", 1.0)
            for index, byte in enumerate(HANDSHAKE):
                send(udev, ep_out, trace, "hs[{}]".format(index),
                     bytes([byte]))
                slurp(ep_in, trace, "hs[{}] response".format(index), 1.0)

        # Phase 3: GET_HW_CODE. mtkclient expects echo fd + 2B hw + 2B
        # status. We only record; 0xFD is safe/read-only.
        if alive:
            send(udev, ep_out, trace, "cmd fd (GET_HW_CODE)", b"\xFD")
            slurp(ep_in, trace, "fd response", 2.0)

        # Phase 4: one harmless WRITE32: TOPRGU watchdog MODE key+reload
        # (same write run 1 proved end-to-end). Read-only regarding
        # storage; at worst the watchdog bites (preloader has no WDT
        # armed during tool listen per run 1 evidence).
        if alive and do_write:
            frame = bytes.fromhex("d4") + (0x10007000).to_bytes(4, "big") \
                + (1).to_bytes(4, "big") + (0x22000064).to_bytes(4, "big")
            send(udev, ep_out, trace, "D4 wdt-disable frame", frame)
            slurp(ep_in, trace, "wdt frame response", 2.0)

        trace("done; no arm flag and no reset were issued; the preloader "
              "will time out to a normal boot")
    finally:
        trace.close()
        print("trace saved: {}".format(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))