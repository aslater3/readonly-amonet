#!/usr/bin/env python3
import sys
import time

from common import from_bytes, to_bytes
from logger import log
from functions import UserInputThread, check_modemmanager

import usb.core
import usb.util

import struct
import os

def p32(x):
    return struct.pack(">I", x)

def load_payload_file(path):
    with open(path, "rb") as fin:
        payload = fin.read()
    log("Load payload from {} = 0x{:X} bytes".format(path, len(payload)))
    while len(payload) % 4 != 0:
        payload += b"\x00"
    return payload

def attempt2(d, udev):
    payload = load_payload_file("../brom-payload/stage1/stage1.bin")

    d.echo(0xE0)
    d.echo(len(payload), 4)

    status = d.read(2)
    if from_bytes(status, 2) != 0:
        raise RuntimeError("status is {}".format(status.hex()))

    d.write(payload)

    d.read(4)

    try:
        udev._ctx.managed_claim_interface = lambda *args, **kwargs: None
    except AttributeError as e:
        raise RuntimeError("libusb issue") from e

    try:
        udev.ctrl_transfer(0xA1, 0, 0, 204, 0)
    except usb.core.USBError as e:
        print(e)

def load_payload(dev):
    log("Handshake")
    dev.handshake()
    log("Disable watchdog")
    dev.write32(0x10007000, 0x22000000)

    thread = UserInputThread()
    thread.start()
    while not thread.done:
        dev.write32(0x10007008, 0x1971)
        time.sleep(1)

    addr = 0x10007050
    dev.write32(addr, [0xA1000])

    cnt = 15
    for i in range(cnt):
        dev.read32(addr - (cnt - i) * 4, cnt - i + 1)

    udev = dev.udev
    attempt2(dev, udev)

    log("Waiting for stage 1 to come online...")
    pattern = dev.read(4)
    if pattern != b"\xA1\xA2\xA3\xA4":
        raise RuntimeError("received {} instead of expected pattern".format(pattern.hex()))

    dev.kick_watchdog()
    log("All good")

    log("Load 2nd stage payload")
    stage2 = load_payload_file("../brom-payload/stage2/stage2.bin")

    log("Send 2nd stage payload")
    dev.write(p32(0xf00dd00d))
    dev.write(p32(0x4000))
    dev.write(p32(0x201000))
    dev.write(p32(len(stage2)))
    dev.write(stage2)

    code = dev.read(4)
    if code != b"\xd0\xd0\xd0\xd0":
        raise RuntimeError("device failure")

    dev.kick_watchdog()

    log("Party time")
    dev.write(p32(0xf00dd00d))
    dev.write(p32(0x4001))
    dev.write(p32(0x201000))

    log("Waiting for stage 2 to come online...")
    data = dev.read(4)
    if data != b"\xB1\xB2\xB3\xB4":
        raise RuntimeError("received {} instead of expected pattern".format(data.hex()))

    log("All good")
    dev.kick_watchdog()
