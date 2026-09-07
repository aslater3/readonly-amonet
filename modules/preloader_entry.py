"""Preloader-stage bridge into BROM download mode (0e8d:0003).

Why this exists
---------------
On the MT8516/MT8167 Echo Studio the action-button BROM entry is not
reachable (disassembled unit) and a shorted eMMC line makes the BROM halt
*before* it initialises USB (verified via UART: "System halt!" with no
0e8d:0003 enumeration). The one door that reliably enumerates is the
preloader tool window: the device appears as ``0e8d:2000`` for ~10 s at
every power-on ("Tool connection is unlocked" in the UART log).

This module speaks the preloader's usbdl protocol -- byte-identical to
the BROM protocol (handshake ``a0 0a 50 05`` with per-byte complements,
per mtkclient Port.py, which sends an extra leading 0xA0 only for
preloader PIDs) -- and performs the standard MediaTek "reset to BROM"
sequence (mtkclient mtk_preloader.reset_to_brom):

1. handshake,
2. watchdog disable (D4) so it cannot bite before the arming writes,
3. misc_lock = MISC_MAGIC, misc_lock+8 = 1, misc_lock = 0 (relock),
4. usbdl_flag (misc_lock-0x20) = USBDL_MAGIC | timeout | EN | ~BROM-bit,
5. watchdog bite -> warm reset -> BROM sees valid usbdl flag -> 0e8d:0003.

Everything here is RAM/register state: no flash, RPMB, or persistent
write. Power removal clears the arm.

misc_lock address note
----------------------
mtkclient has NO misc_lock for hwcode 0x8167 (it never implements this
path for the chip). The value used here, 0x10002050, is the TOPRGU
misc-lock register shared by the adjacent MT8163/MT8127/MT8135 IoT-class
blocks in mtkclient's own brom_config. If BROM does not appear, retry
with --misc-lock 0x10001838 then 0x1000141C (the other values in that
config file).
"""

import struct
import time

import usb.core
import usb.util

from logger import log

PRELOADER_PID = 0x2000
MTK_VID = 0x0E8D
MISC_MAGIC = 0xAD98
USBDL_MAGIC = 0x444C0000
USBDL_BIT_EN = 0x00000001      # download bit enabled
USBDL_BROM = 0x00000002        # 0 = usbdl by BROM (what we want)
USBDL_TIMEOUT_MASK = 0x0000FFFC
HANDSHAKE = b"\xA0\x0A\x50\x05"
DEFAULT_MISC_LOCK = 0x10002050
DEFAULT_TIMEOUT_S = 600        # 14-bit seconds field; 600s >> our run


class PreloaderBridgeError(RuntimeError):
    pass


def _to_bytes(value, size):
    fmt = {1: ">B", 2: ">H", 4: ">I"}.get(size)
    if fmt is None:
        raise PreloaderBridgeError("invalid size {}".format(size))
    return struct.pack(fmt, value)


class PreloaderDevice:
    """Minimal usbdl client for the preloader (0e8d:2000) tool window."""

    def __init__(self, timeout=8):
        self.dev = None
        self.ep_in = None
        self.ep_out = None
        self.timeout = timeout
        self.rxbuffer = b""

    def find(self, wait=True, deadline=None):
        """Locate a 0e8d:2000 device; return True when found."""

        if deadline is None:
            deadline = time.time() + (10 ** 9 if wait else 0)
        log("Waiting for preloader device 0e8d:{:04x} (plug in / power-cycle "
            "the device now; the tool window is ~10s per boot)".format(PRELOADER_PID))
        while time.time() < deadline:
            udev = usb.core.find(idVendor=MTK_VID, idProduct=PRELOADER_PID)
            if udev is not None:
                self.udev = udev
                log("Found preloader device 0e8d:{:04x}".format(PRELOADER_PID))
                try:
                    if udev.is_kernel_driver_active(0):
                        udev.detach_kernel_driver(0)
                    if udev.is_kernel_driver_active(1):
                        udev.detach_kernel_driver(1)
                except (NotImplementedError, usb.core.USBError):
                    pass
                try:
                    udev.set_configuration()
                except usb.core.USBError:
                    pass
                cdc_if = usb.util.find_descriptor(
                    udev.get_active_configuration(), bInterfaceClass=0xA)
                if cdc_if is None:
                    raise PreloaderBridgeError("no CDC interface on 2000 device")
                self.ep_in = usb.util.find_descriptor(
                    cdc_if, custom_match=lambda e:
                    usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
                self.ep_out = usb.util.find_descriptor(
                    cdc_if, custom_match=lambda e:
                    usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
                if self.ep_in is None or self.ep_out is None:
                    raise PreloaderBridgeError("preloader CDC lacks bulk endpoints")
                return True
            time.sleep(0.1)
        return False

    # -- raw pipe ----------------------------------------------------
    def _read(self, size):
        while len(self.rxbuffer) < size:
            try:
                chunk = self.ep_in.read(self.ep_in.wMaxPacketSize,
                                        self.timeout * 1000)
            except usb.core.USBError as error:
                raise PreloaderBridgeError("read failed: {}".format(error))
            if not len(chunk):
                break
            self.rxbuffer += bytes(chunk)
        result, self.rxbuffer = self.rxbuffer[:size], self.rxbuffer[size:]
        if len(result) < size:
            raise PreloaderBridgeError(
                "short read: wanted {} got {} (tool window may have expired "
                "-- power-cycle and retry)".format(size, len(result)))
        return result

    def _write(self, data):
        self.ep_out.write(data, self.timeout * 1000)

    # -- usbdl protocol (identical to BROM apart from the 0xA0 lead) --
    def handshake(self):
        # mtkclient Port.run_handshake: for non-BROM PIDs send one extra
        # leading 0xA0 before the shared a0 0a 50 05 complement echo.
        self._write(b"\xA0")
        try:
            self._read(1)
        except PreloaderBridgeError:
            pass  # some builds answer the lead byte, some do not
        i = 0
        while i < len(HANDSHAKE):
            self._write(HANDSHAKE[i:i + 1])
            reply = self._read(1)
            if reply and reply[0] == ~HANDSHAKE[i] & 0xFF:
                i += 1
            else:
                i = 0
        log("Preloader handshake OK")

    def read32(self, addr):
        self._write(_to_bytes(0xD1, 1))
        self._read(1)                      # echo
        self._write(_to_bytes(addr, 4))
        self._read(4)
        self._write(_to_bytes(1, 4))
        self._read(4)
        value = struct.unpack(">I", self._read(4))[0]
        struct.unpack(">H", self._read(2))  # status
        return value

    def write32(self, addr, value):
        self._write(_to_bytes(0xD4, 1))
        self._read(1)
        self._write(_to_bytes(addr, 4))
        self._read(4)
        self._write(_to_bytes(1, 4))
        self._read(4)
        self._read(2)                      # arg-check status
        self._write(_to_bytes(value, 4))
        self._read(4)
        self._read(2)                      # status

    def disable_watchdog(self):
        # 0xD4 is WRITE32 in this protocol (there is no one-byte WDT
        # command); mtkclient's setreg_disablewatchdogtimer writes the
        # TOPRGU key value to the watchdog MODE register.  Use the exact
        # value our proven BROM path uses (key 0x2200_0000 + reload 0x64,
        # ENABLE bit clear => watchdog disabled but loaded).
        self.write32(0x10007000, 0x22000064)
        log("Watchdog disabled via MODE write (0x10007000 <= 0x22000064)")

    def arm_brom_and_reset(self, misc_lock=DEFAULT_MISC_LOCK,
                           timeout_s=DEFAULT_TIMEOUT_S):
        """Volatile BROM-mode arm + watchdog bite. No persistent write."""

        usbdlreg = brom_arm_value(timeout_s)
        usbdl_flag = misc_lock - 0x20
        rst_con = misc_lock + 8

        log("Arming BROM: misc_lock=0x{:08X} rst_con=0x{:08X} "
            "usbdl_flag=0x{:08X} value=0x{:08X}".format(
                misc_lock, rst_con, usbdl_flag, usbdlreg))
        probe = self.read32(misc_lock)
        log("misc_lock pre-value: 0x{:08X}".format(probe))

        self.write32(misc_lock, MISC_MAGIC)
        self.write32(rst_con, 1)           # watchdog resettable
        self.write32(misc_lock, 0)         # relock
        self.write32(usbdl_flag, usbdlreg)
        log("usbdl flag written; requesting TOPRGU software reset")
        # MTK WDT/TOPRGU reset sequence, verified against the MT8163
        # downstream kernel mt_wdt.h (same TOPRGU block; watchdog base
        # 0x10007000 is mtkclient's own value for hwcode 0x8167):
        #   MTK_WDT_MODE  = base+0x00, KEY=0x22000000, EXTEN=0x04
        #   MTK_WDT_SWRST = base+0x14, MTK_WDT_SWRST_KEY = 0x1209
        # wdt_arch_reset() writes MODE with the key bits then SWRST with
        # the key; the SoC resets immediately (this also bypasses the
        # power-key boot check, which is exactly what we need).
        wdt_base = 0x10007000
        try:
            mode = self.read32(wdt_base + 0x00)
            # Preserve ENABLE/reload bits, add key + EXTEN, mirror kernel
            # wdt_arch_reset().
            self.write32(wdt_base + 0x00, mode | 0x22000000 | 0x04)
            self.write32(wdt_base + 0x14, 0x1209)
            log("TOPRGU SWRST issued (mode was 0x{:08X})".format(mode))
        except PreloaderBridgeError as error:
            log("SWRST write failed: {}".format(error))
            raise


def brom_arm_value(timeout_s: int = DEFAULT_TIMEOUT_S) -> int:
    """The exact 32-bit value the BROM usbdl flag register must hold.

    Pure function (unit-tested): USBDL magic, 14-bit second-granularity
    timeout in bits [1:15], download-enable bit set, and the "handled by
    BROM" bit CLEAR (mtkclient reset_to_brom semantics).
    """

    timeout_s &= USBDL_TIMEOUT_MASK >> 2
    value = USBDL_MAGIC | ((timeout_s << 2) & USBDL_TIMEOUT_MASK)
    value |= USBDL_BIT_EN
    value &= ~USBDL_BROM
    return value


def bridge_to_brom(misc_lock=DEFAULT_MISC_LOCK, deadline_s=None):
    """Full path: wait for 2000 window -> handshake -> arm -> reset -> 0003.

    Returns True once the BROM device 0e8d:0003 enumerates.
    """

    dev = PreloaderDevice()
    if not dev.find(wait=True, deadline=(time.time() + deadline_s
                                         if deadline_s else None)):
        return False
    dev.handshake()
    try:
        dev.disable_watchdog()
    except PreloaderBridgeError as error:
        log("Watchdog disable not acknowledged ({}); continuing -- "
            "arming anyway".format(error))
    try:
        dev.arm_brom_and_reset(misc_lock=misc_lock)
    except PreloaderBridgeError as error:
        log("Arm sequence incomplete: {}".format(error))
        return False
    log("Device should reset shortly; watching for 0e8d:0003 ...")
    deadline = time.time() + 60
    while time.time() < deadline:
        brom = usb.core.find(idVendor=MTK_VID, idProduct=0x0003)
        if brom is not None:
            log("BROM 0e8d:0003 enumerated -- bridge succeeded")
            return True
        time.sleep(0.25)
    log("No 0e8d:0003 within 60s. Try --misc-lock 0x10001838 or "
        "0x1000141C, and watch the UART: a normal 'Jump to BL' boot means "
        "the BROM did not honour the flag at that register.")
    return False
