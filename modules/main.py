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
        flash_binary(device, "../bin/gpt-cupcake.bin", 0, 34 * 0x200)

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

    # 2.1) Clear preloader so, we get into bootrom without shorting, should the script stall (we flash preloader as last step)
    log("Clear preloader header")
    flash_data(device, b"EMMC_BOOT" + b"\x00" * ((0x200 * 8) - 9), 0)

    # 3) Sanity check rpmb
    log("Check rpmb")
    rpmb = device.rpmb_read()
    if rpmb[0:4] != b"AMZN":
        thread = UserInputThread(msg = "rpmb looks broken; if this is expected (i.e. you're retrying the exploit) press enter, otherwise terminate with Ctrl+C")
        thread.start()
        while not thread.done:
            device.kick_watchdog()
            time.sleep(1)

    # 4) Zero out rpmb to enable downgrade
    log("Downgrade rpmb")
    device.rpmb_write(b"\x00" * 0x100)
    log("Recheck rpmb")
    rpmb = device.rpmb_read()
    if rpmb != b"\x00" * 0x100:
        device.reboot()
        raise RuntimeError("downgrade failure, giving up")
    log("rpmb downgrade ok")
    device.kick_watchdog()

    # 5) Reset BCB
    log("Reset BCB")
    switch_user(device)
    reset_bcb(device, gpt)

    # 6) Flash original tee to tee2
    log("Flash tee2")
    flash_binary(device, "../bin/tee-cupcake.bin", gpt["tee2"][0], gpt["tee2"][1] * 0x200)

    # 7) Flash original LKs to both slots
    log("Flash lk")
    flash_binary(device, "../bin/lk-cupcake.bin", gpt["lk_a"][0], gpt["lk_a"][1] * 0x200)
    flash_binary(device, "../bin/lk-cupcake.bin", gpt["lk_b"][0], gpt["lk_b"][1] * 0x200)

    # 8) Flash kaeru
    log("Flash kaeru")
    flash_binary(device, "../bin/kaeru-cupcake.bin", gpt["expdb"][0], gpt["expdb"][1] * 0x200)

    # 9) Flash tee w/ payload to tee1
    log("Flash payload")
    flash_binary(device, "../bin/tee-cupcake-payload.bin", gpt["tee1"][0], gpt["tee1"][1] * 0x200)

    # 10) Downgrade preloader
    log("Flash preloader")
    switch_boot0(device)
    flash_binary(device, "../bin/preloader-cupcake.bin", 0)

    # 11) Force fastboot mode
    log("Force fastboot mode")
    device.set_fastboot_flag()

    # 12) Wait some time so data is flushed to EMMC
    time.sleep(5)

    # Reboot (to fastboot)
    log("Reboot to unlocked fastboot")
    device.reboot()


if __name__ == "__main__":
    device = Device().find()
    main(device)
