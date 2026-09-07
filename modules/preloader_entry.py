"""Preloader-stage bridge into BROM download mode (0e8d:0003).

Why this exists
---------------
On the MT8516/MT8167 Echo Studio the action-button BROM entry is not
reachable (disassembled unit) and a shorted eMMC line makes the BROM halt
*before* it initialises USB (verified via UART: "System halt!" with no
0e8d:0003 enumeration). The one door that reliably enumerates is the
preloader tool window: the device appears as ``0e8d:2000`` for ~10 s at
every power-on ("Tool connection is unlocked" in the UART log).

Protocol facts verified on hardware (2026-09-07)
------------------------------------------------
* ``0e8d:2000`` is found, the ``a0 0a 50 05`` complement handshake
  succeeds (extra leading 0xA0 first, per mtkclient Port.py), the UART
  confirms "usb_listen", and WRITE32 (0xD4) commands echo cleanly.
* READ32 (0xD1) is NOT answered: the data phase times out. The first
  bridge attempt stalled 8 s on a READ32 pre-read, blew the ~10 s tool
  window, and let the preloader boot the OS (which consumes one IDME
  boot_count).  This version therefore performs ZERO 0xD1 reads: every
  step is WRITE32 (0xD4) with strict echo/status verification, so the
  whole arm + reset fires in ~1 s, far inside the window.

Mechanism (volatile only)
-------------------------
1. find ``0e8d:2000``, handshake, D4-register watchdog disable,
2. misc_lock = 0xAD98 (unlock), misc_lock+8 = 1 (watchdog-resettable),
   misc_lock = 0 (relock),
3. misc_lock-0x20 = 0x444C magic | timeout | EN | ~BROM-bit: asks the
   BROM to enter usbdl on the *next warm reset*,
4. TOPRGU SWRST (base+0x14 <- 0x1209): immediate warm reset,
5. watch what comes back:
   * ``0e8d:0003``  -> the BROM honoured the flag: SUCCESS, dump runs;
   * ``0e8d:2000``  -> the SoC reset but the BROM ignored the flag at
     this address: automatically re-handshake and retry the next
     misc_lock candidate (a fresh preloader instance, same power-up,
     no OS boot -> boot_count untouched);
   * continuous ``2000`` with no gap -> our writes were no-ops (the
     tool is DAA-gated on this unit): stop and report; the preloader
     will time out to a normal boot (one boot_count spent);
   * reset with neither device -> report and let the operator check.

Everything is RAM/register state; no flash, RPMB, or persistent write.
Power removal clears everything.

misc_lock candidates
--------------------
mtkclient has NO misc_lock for hwcode 0x8167.  Candidates, in order:
0x10002050 (MT8163/MT8127/MT8135 family), 0x10001838, 0x1000141C,
0x1001a100.  All come from mtkclient's own Chipconfig table.
"""

import struct
import time

import usb.core
import usb.util

from logger import log

PRELOADER_PID = 0x2000
BROM_PID = 0x0003
MTK_VID = 0x0E8D
MISC_MAGIC = 0xAD98
USBDL_MAGIC = 0x444C0000
USBDL_BIT_EN = 0x00000001      # download bit enabled
USBDL_BROM = 0x00000002        # 0 = usbdl by BROM (what we want)
USBDL_TIMEOUT_MASK = 0x0000FFFC
HANDSHAKE = b"\xA0\x0A\x50\x05"
WDT_BASE = 0x10007000          # mtkclient chipconfig for hwcode 0x8167
WDT_DISABLE_VALUE = 0x22000064 # key + reload, ENABLE bit clear
WDT_SWRST_OFFSET = 0x14        # MTK_WDT_SWRST (mt_wdt.h / mainline)
WDT_SWRST_KEY = 0x1209         # MTK_WDT_SWRST_KEY / SW_RST_MAGIC_NUM
CMD_WRITE32 = 0xD4             # the ONLY command byte this bridge sends
CMD_GET_HW_CODE = 0xFD         # optional liveness probe, non-fatal
DEFAULT_MISC_LOCK = 0x10002050
MISC_LOCK_FALLBACKS = (0x10002050, 0x10001838, 0x1000141C, 0x1001a100)
DEFAULT_TIMEOUT_S = 600        # 14-bit seconds field; 600s >> our run
READ_TIMEOUT = 2               # seconds; the tool window is only ~10 s


class PreloaderBridgeError(RuntimeError):
    pass


def _to_bytes(value, size):
    fmt = {1: ">B", 2: ">H", 4: ">I"}.get(size)
    if fmt is None:
        raise PreloaderBridgeError("invalid size {}".format(size))
    return struct.pack(fmt, value)


def brom_arm_value(timeout_s: int = DEFAULT_TIMEOUT_S) -> int:
    """Exact 32-bit value for the BROM usbdl flag register.

    USBDL magic, second-granularity timeout, download-enable bit SET,
    'handled by bootloader' bit CLEAR (mtkclient reset_to_brom)."""

    timeout_s &= USBDL_TIMEOUT_MASK >> 2
    value = USBDL_MAGIC | ((timeout_s << 2) & USBDL_TIMEOUT_MASK)
    value |= USBDL_BIT_EN
    value &= ~USBDL_BROM
    return value


def misc_lock_candidates(primary=DEFAULT_MISC_LOCK):
    """Primary first, then the remaining known values, order preserved."""

    seen, ordered = set(), []
    for value in (primary,) + MISC_LOCK_FALLBACKS:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


class PreloaderDevice:
    """WRITE32-only usbdl client for the preloader (0e8d:2000) window."""

    def __init__(self, timeout=READ_TIMEOUT):
        self.timeout = timeout
        self.rxbuffer = b""
        self.udev = None
        self.ep_in = None
        self.ep_out = None

    def find(self, deadline=None):
        log("Waiting for preloader device 0e8d:{:04x} (plug in / power-cycle"
            " the device; the tool window is ~10s per boot)".format(
                PRELOADER_PID))
        while deadline is None or time.time() < deadline:
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
                    raise PreloaderBridgeError("no CDC interface on 2000")
                self.ep_in = usb.util.find_descriptor(
                    cdc_if, custom_match=lambda e:
                    usb.util.endpoint_direction(e.bEndpointAddress)
                    == usb.util.ENDPOINT_IN)
                self.ep_out = usb.util.find_descriptor(
                    cdc_if, custom_match=lambda e:
                    usb.util.endpoint_direction(e.bEndpointAddress)
                    == usb.util.ENDPOINT_OUT)
                if self.ep_in is None or self.ep_out is None:
                    raise PreloaderBridgeError("preloader CDC lacks bulk eps")
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
                raise PreloaderBridgeError(
                    "read failed: {} (tool window may have expired)".format(
                        error))
            if not len(chunk):
                break
            self.rxbuffer += bytes(chunk)
        result, self.rxbuffer = self.rxbuffer[:size], self.rxbuffer[size:]
        if len(result) < size:
            raise PreloaderBridgeError(
                "short read: wanted {} got {}".format(size, len(result)))
        return result

    def _write(self, data):
        self.ep_out.write(data, self.timeout * 1000)

    def _echo(self, value, size):
        payload = _to_bytes(value, size)
        self._write(payload)
        reply = self._read(size)
        if reply != payload:
            raise PreloaderBridgeError(
                "echo mismatch: sent {} got {}".format(
                    payload.hex(), reply.hex()))

    def _drain(self):
        self.rxbuffer = b""

    # -- protocol (0xD4 WRITE32 only; NO 0xD1 READ32 on this build) --
    def handshake(self):
        self._write(b"\xA0")           # preloader variant lead byte
        try:
            self._read(1)
        except PreloaderBridgeError:
            pass                       # some builds answer the lead, some not
        i = 0
        while i < len(HANDSHAKE):
            self._write(HANDSHAKE[i:i + 1])
            reply = self._read(1)
            if reply and reply[0] == ~HANDSHAKE[i] & 0xFF:
                i += 1
            else:
                i = 0
        log("Preloader handshake OK")

    def liveness_probe(self):
        """Optional GET_HW_CODE (0xFD); never fatal, drains any leftovers."""

        try:
            self._echo(CMD_GET_HW_CODE, 1)
            hw = self._read(2)
            status = self._read(2)
            log("Liveness: GET_HW_CODE echoed, hw=0x{} status=0x{}".format(
                hw.hex(), status.hex()))
        except PreloaderBridgeError as error:
            log("Liveness: 0xFD not answered cleanly ({}) -- continuing "
                "blind".format(error))
        finally:
            self._drain()

    def write32(self, addr, value):
        """Strict D4 write with mtkclient's echo+status checks."""

        self._echo(CMD_WRITE32, 1)
        self._echo(addr, 4)
        self._echo(1, 4)               # one dword
        arg_status = struct.unpack(">H", self._read(2))[0]
        self._echo(value, 4)
        status = struct.unpack(">H", self._read(2))[0]
        if arg_status != 1 or status != 1:
            raise PreloaderBridgeError(
                "WRITE32 0x{:08X} rejected: arg_status={} status={}".format(
                    addr, arg_status, status))

    def arm_brom_and_reset(self, misc_lock=DEFAULT_MISC_LOCK,
                           timeout_s=DEFAULT_TIMEOUT_S):
        """Volatile BROM-mode arm + TOPRGU warm reset.  D4 writes only.

        Returns list of (label, error) for steps that failed; the TOPRGU
        reset is attempted whenever the flag write itself succeeded, so a
        dead step never wastes the whole tool window silently.
        """

        usbdlreg = brom_arm_value(timeout_s)
        usbdl_flag = misc_lock - 0x20
        steps = [
            ("watchdog disable", WDT_BASE, WDT_DISABLE_VALUE),
            ("misc unlock", misc_lock, MISC_MAGIC),
            ("wdt-resettable", misc_lock + 8, 1),
            ("misc relock", misc_lock, 0),
            ("usbdl BROM flag", usbdl_flag, usbdlreg),
        ]
        failures = []
        for label, addr, value in steps:
            try:
                self.write32(addr, value)
                log("  {} -> 0x{:08X} <= 0x{:08X}: accepted".format(
                    label, addr, value))
            except PreloaderBridgeError as error:
                failures.append((label, str(error)))
                log("  {} -> 0x{:08X} FAILED: {}".format(label, addr, error))
        flag_ok = not any(label == "usbdl BROM flag" for label, _ in failures)
        if flag_ok:
            try:
                self.write32(WDT_BASE + WDT_SWRST_OFFSET, WDT_SWRST_KEY)
                log("TOPRGU SWRST issued")
            except PreloaderBridgeError as error:
                # The reset write can error out legitimately: the SoC
                # resets before the echo returns.
                log("SWRST write ended with: {} (a reset may already have "
                    "happened)".format(error))
        else:
            log("usbdl flag write was rejected; NOT issuing reset")
        return failures


def _watch_reset(deadline_s=50.0):
    """After the SWRST: classify what comes back.

    Returns 'success' (0003), 'flag_ignored' (2000 returned after a gap),
    'writes_noop' (2000 never even blinked), or 'booted'/'timeout'."""

    deadline = time.time() + deadline_s
    seen_gap = False
    gone_since = None
    while time.time() < deadline:
        if usb.core.find(idVendor=MTK_VID, idProduct=BROM_PID) is not None:
            log("BROM 0e8d:0003 enumerated -- the BROM honoured the flag")
            return "success"
        preloader_back = usb.core.find(idVendor=MTK_VID,
                                       idProduct=PRELOADER_PID) is not None
        if not preloader_back:
            if not seen_gap:
                log("  device disappeared (reset took effect), waiting for "
                    "BROM or preloader...")
            seen_gap = True
            gone_since = gone_since or time.time()
            if gone_since and time.time() - gone_since > 30:
                log("  gap with neither 0003 nor 2000 for 30s: device "
                    "booted past the tool stage")
                return "booted"
        else:
            if seen_gap:
                log("  preloader 0e8d:2000 is back: the SoC reset cleanly "
                    "but the BROM ignored the flag at this address")
                return "flag_ignored"
            if time.time() > deadline - deadline_s + 32:
                log("  preloader never blinked: our register writes were "
                    "no-ops (DAA-gated tool?); NOT retrying other addresses")
                return "writes_noop"
        time.sleep(0.25)
    log("  no BROM/preloader verdict within the window")
    return "timeout"


def bridge_to_brom(misc_lock=DEFAULT_MISC_LOCK, first_window_deadline=None):
    """Wait for the 2000 window, then try each misc_lock candidate.

    A 'flag_ignored' verdict reuses the SAME power-up: the warm reset
    re-enters the preloader tool window (no OS boot, no boot_count cost)
    and we re-handshake against the next candidate automatically.
    Returns True on success.
    """

    candidates = misc_lock_candidates(misc_lock)
    log("BROM arm candidates (misc_lock): {}".format(
        ", ".join(hex(c) for c in candidates)))
    dev = None
    for attempt, candidate in enumerate(candidates, start=1):
        if dev is None:
            dev = PreloaderDevice()
            if not dev.find(deadline=first_window_deadline):
                return False
        else:
            if not dev.find(deadline=time.time() + 45):
                log("preloader did not come back for candidate {} -- "
                    "power-cycle and rerun".format(attempt))
                return False
        log("=== bridge attempt {}/{}: misc_lock=0x{:08X} ===".format(
            attempt, len(candidates), candidate))
        try:
            dev.handshake()
            dev.liveness_probe()
            dev.arm_brom_and_reset(misc_lock=candidate)
        except PreloaderBridgeError as error:
            log("attempt {} failed before reset: {}".format(attempt, error))
            if attempt == 1:
                # The window is burned; the preloader will boot normally.
                log("tool window is spent; the preloader will now boot the "
                    "OS (one boot_count consumed)")
            return False
        dev = None  # endpoints die at the reset; force fresh instance
        verdict = _watch_reset()
        if verdict == "success":
            return True
        if verdict != "flag_ignored":
            if verdict == "writes_noop":
                log("This unit's tool mode accepted the handshake but "
                    "ignored register writes -- misc_lock guessing cannot "
                    "fix that; the DAA gate is the blocker.")
            return False
    log("all misc_lock candidates ignored; try the keypad-GPIO "
        "button-equivalent next")
    return False
