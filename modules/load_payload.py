#!/usr/bin/env python3
import sys
import time

import usb
import array

from common import from_bytes, to_bytes
from logger import log
from functions import UserInputThread, check_modemmanager
from brom_diag import describe_status_error, log_brom_identity

import usb.core
import usb.util

import struct
import os

def da_read(device, linecode, address, length, check_result = True):
    return da_read_write(device, linecode, 0, address, length, None, check_result)

def da_write(device, linecode, address, length, data, check_result = True):
    return da_read_write(device, linecode, 1, address, length, data, check_result)

def da_read_write(device, linecode, direction, address, length, data = None, check_result = True):
    ptr_da = 0xD7AC
    addr = 0x10007000 + 0x50

    try:
        device.cmd_da(0,0,1)
        device.read32(addr)
    except:
        pass

    for i in range(3):
        device.udev.ctrl_transfer(0x21, 0x20, 0, 0, linecode + array.array('B', to_bytes(ptr_da + 8 - 3 + i, 4, '<')))
        device.udev.ctrl_transfer(0x80, 0x6, 0x0200, 0, 9)

    if address < 0x40:
        for i in range(4):
            device.udev.ctrl_transfer(0x21, 0x20, 0, 0, linecode + array.array('B', to_bytes(ptr_da - 6 + (4 - i), 4, '<')))
            device.udev.ctrl_transfer(0x80, 0x6, 0x0200, 0, 9)
        return device.cmd_da(direction, address, length, data, check_result)
    else:
        for i in range(3):
            device.udev.ctrl_transfer(0x21, 0x20, 0, 0, linecode + array.array('B', to_bytes(ptr_da - 5 + (3 - i), 4, '<')))
            device.udev.ctrl_transfer(0x80, 0x6, 0x0200, 0, 9)
        return device.cmd_da(direction, address - 0x40, length, data, check_result)

def p32(x):
    return struct.pack(">I", x)

def load_payload_file(path):
    with open(path, "rb") as fin:
        payload = fin.read()
    log("Load payload from {} = 0x{:X} bytes".format(path, len(payload)))
    while len(payload) % 4 != 0:
        payload += b"\x00"

    return payload

def noop(*args, **kwargs):
    pass

def load_payload(device):
    log("Handshake")
    device.handshake()

    log_brom_identity(device)

    log("Disable watchdog")
    device.write32(0x10007000, 0x22000000)

    thread = UserInputThread()
    thread.start()
    while not thread.done:
        device.write32(0x10007008, 0x1971) # low-level watchdog kick
        time.sleep(1)

    stage1 = load_payload_file("../brom-payload/stage1/stage1.bin")

    if len(stage1) >= 0xA00:
        raise RuntimeError("payload too large")

    try:
        ptr_usbdl = 0xd2e4
        payload_address = 0x100A00
        linecode = device.udev.ctrl_transfer(0xA1, 0x21, 0, 0, 7) + array.array('B', [0])
        try:
            ptr_send = from_bytes(da_read(device, linecode, ptr_usbdl, 4), 4, '<') + 8
        except RuntimeError as error:
            raise RuntimeError(describe_status_error(error)) from error

        log("Let's rock")
        da_write(device, linecode, payload_address, len(stage1), stage1)
        da_write(device, linecode, ptr_send, 4, to_bytes(payload_address, 4, '<'), False)
    except usb.core.USBError as e:
        print(e)

    # We don't need to wait long, if we succeeded
    # noinspection PyBroadException
    try:
        device.dev.timeout = 1
    except Exception:
        pass

    log("Waiting for stage 1 to come online...")
    data = device.read(4)
    if data != b"\xA1\xA2\xA3\xA4":
        raise RuntimeError("received {} instead of expected pattern".format(data))

    device.kick_watchdog()

    log("Load 2nd stage payload")
    stage2 = load_payload_file("../brom-payload/stage2/stage2.bin")

    log("Send 2nd stage payload")
    # magic
    device.write(p32(0xf00dd00d))
    # cmd
    device.write(p32(0x4000))
    # address to write
    device.write(p32(0x201000))
    # length
    device.write(p32(len(stage2)))
    # data
    device.write(stage2)

    code = device.read(4)
    if code != b"\xd0\xd0\xd0\xd0":
        raise RuntimeError("device failure")

    device.kick_watchdog()

    log("Party time")
    # magic
    device.write(p32(0xf00dd00d))
    # cmd
    device.write(p32(0x4001))
    # address to write
    device.write(p32(0x201000))

    log("Waiting for stage 2 to come online...")

    data = device.read(4)
    if data != b"\xB1\xB2\xB3\xB4":
        raise RuntimeError("received {} instead of expected pattern".format(data))

    log("All good")

    device.kick_watchdog()