#!/usr/bin/env python3

import sys
import time

from device import Device
from logger import log
from load_payload import load_payload
from functions import *

def main(device):
    load_payload(device)

    if len(sys.argv) == 2 and sys.argv[1] == "fixgpt":
        device.emmc_switch(0)
        log("Flashing GPT")
        flash_binary(device, "../bin/gpt-mantis.bin", 0, 34 * 0x200)

    # 1) Sanity check GPT
    log("Check GPT")
    switch_user(device)

    # 1.1) Parse gpt
    gpt = parse_gpt(device)
    log("gpt_parsed = {}".format(gpt))
    if "lk_a" not in gpt or "tee1" not in gpt or "boot_a" not in gpt:
        raise RuntimeError("bad gpt")

    # 2) Sanity check boot0
    log("Check boot0")
    switch_boot0(device)

    # 3) Sanity check rpmb
    log("Check rpmb")
    rpmb = device.rpmb_read()
    if rpmb[0:4] != b"AMZN":
        thread = UserInputThread(msg = "rpmb looks broken; if this is expected (i.e. you're retrying the exploit) press enter, otherwise terminate with Ctrl+C")
        thread.start()
        while not thread.done:
            device.kick_watchdog()
            time.sleep(1)


    # 5) Zero out rpmb to enable downgrade
    log("Downgrade rpmb")
    device.rpmb_write(b"\x00" * 0x100)
    log("Recheck rpmb")
    rpmb = device.rpmb_read()
    if rpmb != b"\x00" * 0x100:
        device.reboot()
        raise RuntimeError("downgrade failure, giving up")
    log("rpmb downgrade ok")
    device.kick_watchdog()


    # 9.1) Wait some time so data is flushed to EMMC
    time.sleep(5)

    # Reboot (to fastboot or recovery)
    log("Reboot")
    device.reboot()


if __name__ == "__main__":
    device = Device().find()
    main(device)
