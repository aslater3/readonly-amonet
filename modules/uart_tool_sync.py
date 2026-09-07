#!/usr/bin/env python3
"""UART tool-sync experiment for the Echo Studio preloader (read-mostly).

UART evidence shows the Amazon preloader tool does NOT only listen on
USB: after the USB listen timer expires it prints

    [TOOL] <UART> wait sync time 150ms->5ms
    [TOOL] <UART> receieved data: ()

i.e. it then waits ~150 ms (+5 ms retry) for the MTK tool sync sequence
(a0 0a 50 05) on UART0 @ 921600 8N1, and on every capture so far it
received nothing, then booted normally.

This script is the button-free probe for that door:
  1. watch the console port,
  2. the instant "wait sync" appears, send the 4 sync bytes,
  3. report exactly what the tool echoes back.

A correct handshake yields byte-wise complements (5f f5 af fa) and the
tool stays in command mode; a wall of silence means Amazon's UART tool
channel is dead like the USB one and we stop there.

This script NEVER sends anything beyond the 4 sync bytes -- no
read32/write32 frames, no register semantics, no eMMC access. Command
mode follow-up (if achieved) is analysed before anything else is sent.

Usage:
  python3 modules/uart_tool_sync.py /dev/ttyUSB1 [--baud 921600]
  (then power-cycle the device; keep the OTHER console capture running
   if you have one -- do not open the same port twice)
"""
import argparse
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pip install pyserial (or run inside /tmp/amonet-venv)")

SYNC = b"\xa0\x0a\x50\x05"
TRIGGERS = (b"wait sync", b"<UART>")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}.{int(time.time() % 1 * 1000):03d}] "
          f"{msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--send-sync", action="store_true",
                    help="actually send the 4 sync bytes (default: "
                         "observe only, so you can re-run the safe way)")
    ap.add_argument("--window", type=float, default=45.0,
                    help="seconds to watch after trigger for responses")
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=0.05, rtscts=False)
    log(f"watching {args.port} @ {args.baud}; power-cycle the device now")

    t0 = time.time()
    tail = b""
    while time.time() - t0 < 120:
        chunk = ser.read(256)
        if not chunk:
            continue
        tail = (tail + chunk)[-4096:]
        if any(t in tail for t in TRIGGERS):
            log("UART tool-sync window detected")
            if args.send_sync:
                ser.reset_input_buffer()
                ser.write(SYNC)
                ser.flush()
                log("sent 4 sync bytes a0 0a 50 05")
            else:
                log("--send-sync not given: observing only (nothing sent)")
            break
    else:
        log("no sync-window banner seen in 120s; is this the right port?")
        return 1

    end = time.time() + args.window
    resp = bytearray()
    while time.time() < end:
        chunk = ser.read(256)
        if chunk:
            resp.extend(chunk)
            # show live so the preloader boot banner is still visible
            sys.stdout.write(chunk.decode("latin-1"))
            sys.stdout.flush()
        if len(resp) > 4096:
            break

    log(f"captured {len(resp)} bytes after trigger")
    head = bytes(resp[:24])
    log("first bytes: " + " ".join(f"{b:02x}" for b in head))
    if SYNC == b"\xa0\x0a\x50\x05" and args.send_sync:
        if b"\x5f\xf5\xaf\xfa" in resp:
            print("verdict: UART tool handshake COMPLETED (complements "
                  "5f f5 af fa) -- preloader is in UART command mode; "
                  "next step is read-only verification from here")
        else:
            print("verdict: no complement sequence seen -- UART tool "
                  "channel ignores sync or wants different framing; "
                  "send this capture for analysis")
    return 0


if __name__ == "__main__":
    sys.exit(main())