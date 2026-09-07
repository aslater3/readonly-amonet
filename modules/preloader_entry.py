"""Preloader-stage bridge into BROM download mode (0e8d:0003).

Why this exists
---------------
On the MT8516/MT8167 Echo Studio the action-button BROM entry is not
reachable (disassembled unit) and a shorted eMMC line makes the BROM halt
*before* it initialises USB (verified via UART: "System halt!" with no
0e8d:0003 enumeration). The one door that reliably enumerates is the
preloader tool window: the device appears as ``0e8d:2000`` for ~10 s at
every power-on ("Tool connection is unlocked" in the UART log).

Protocol facts verified on hardware (2026-09-07, two runs)
----------------------------------------------------------
* ``0e8d:2000`` is found and the ``a0 0a 50 05`` complement handshake
  succeeds (extra leading 0xA0 first, per mtkclient Port.py).
* The Amazon tool mode pushes a ~511-byte greeting on enumeration
  (UART: "USB_HANDSHAKE: should be 1 bytes less than 512 bytes") and
  MIRRORS the host's own bytes back with lag.  Strict fixed-count reads
  therefore desynchronise permanently: one run stalled 8 s on a READ32
  (0xD1) data phase and burned the window; another mispaired D4 echoes
  against still-in-flight handshake bytes.
* The D4 (WRITE32) command frame itself IS accepted (run 1 verified a
  watchdog-mode write end-to-end with strict echo+status checks).

Design consequence: arm BLIND, verify by outcome
------------------------------------------------
The side effects we need are register writes, not response payloads, so
the arm phase performs ZERO reads once the handshake completes: five D4
WRITE32 frames plus the TOPRGU SWRST frame are written back-to-back in
well under a second.  The device's reboot verdict then verifies the
whole chain:

* device resets and ``0e8d:0003`` enumerates  -> flag honoured: SUCCESS;
* device resets and ``0e8d:2000`` returns     -> BROM ignored the flag
  at this address; re-handshake on the SAME power-up (no OS boot, no
  boot_count cost) and auto-retry the next misc_lock candidate;
* device never blinks                          -> writes did not take
  effect at all; stop instead of burning further windows.

Mechanism (volatile only; mirrors mtkclient Preloader.reset_to_brom)
--------------------------------------------------------------------
1. misc_lock = 0xAD98 (unlock), misc_lock+8 = 1 (watchdog-resettable),
   misc_lock = 0 (relock),
2. misc_lock-0x20 = 0x444C magic | timeout | EN | ~BROM-bit,
3. TOPRGU SWRST (base+0x14 <- 0x1209) -> immediate warm reset.

Everything is RAM/register state; no flash, RPMB, or persistent write.
Power removal clears everything.  No 0xD1 READ32, no DA (0xD7/0xD5).

misc_lock candidates
--------------------
mtkclient has NO misc_lock for hwcode 0x8167.  Candidates, in order:
0x10002050 (MT8163/MT8127/MT8135 family), 0x10001838, 0x1000141C,
0x1001a100.  All values come from mtkclient's own Chipconfig table.
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
WDT_SWRST_KEY = 0x1209         # MTK_WDT_SWRST_KEY
CMD_WRITE32 = 0xD4             # the only command byte this bridge sends
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


def write32_frame(addr, value):
    """The exact D4 WRITE32 command frame, as the tool echoes commands."""

    return (_to_bytes(CMD_WRITE32, 1) + _to_bytes(addr, 4)
            + _to_bytes(1, 4) + _to_bytes(value, 4))


class PreloaderDevice:
    """usbdl client for the preloader (0e8d:2000) tool window."""

    def __init__(self, timeout=READ_TIMEOUT):
        self.timeout = timeout
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
                self._drain_greeting()
                return True
            time.sleep(0.1)
        return False

    def _drain_greeting(self):
        """Discard the enumeration greeting blob, non-fatal, max 1 s.

        The tool pushes ~511 bytes on enumeration; a stuck device-side TX
        flush (UART "usbdl_flush timeout") was harmless in testing, so we
        drain briefly at connect and never read again during the arm."""

        deadline = time.time() + 1.0
        total = 0
        while time.time() < deadline:
            try:
                chunk = self.ep_in.read(self.ep_in.wMaxPacketSize, 300)
            except usb.core.USBError:
                break
            if not len(chunk):
                break
            total += len(chunk)
        if total:
            log("  drained {} greeting bytes from IN pipe".format(total))

    def _write(self, data):
        self.ep_out.write(data, self.timeout * 1000)

    def _read_packet(self):
        chunk = self.ep_in.read(self.ep_in.wMaxPacketSize, self.timeout * 1000)
        if not len(chunk):
            raise PreloaderBridgeError("empty read from tool IN pipe")
        return bytes(chunk)

    def handshake(self):
        """Complement handshake with anchored last-byte semantics.

        The tool mirrors host bytes back raw before/around protocol
        replies, so 'last byte of a packet' matching (mtkclient
        ep_in(maxinsize)[-1]) is the only reliable test; anchored loop
        re-reading until the expected complement appears."""

        self._write(b"\xA0")               # preloader variant lead byte
        i = 0
        while i < len(HANDSHAKE):
            self._write(HANDSHAKE[i:i + 1])
            expected = ~HANDSHAKE[i] & 0xFF
            deadline = time.time() + self.timeout
            matched = False
            while time.time() < deadline:
                try:
                    last = self._read_packet()[-1]
                except (usb.core.USBError, PreloaderBridgeError):
                    break
                if last == expected:
                    matched = True
                    break
                if last == HANDSHAKE[i]:
                    continue               # own mirror echo, keep reading
            if matched:
                i += 1
            # otherwise keep sending the same byte from the start of the
            # sequence (mtkclient resets i to 0 on mismatch)
            else:
                i = 0
        log("Preloader handshake OK")

    def arm_brom_blind(self, misc_lock=DEFAULT_MISC_LOCK,
                       timeout_s=DEFAULT_TIMEOUT_S):
        """Write the full arm + TOPRGU reset as six frames, zero reads.

        Verified by outcome (reset verdict), not by protocol replies:
        run 1 proved D4 frames are accepted; response parsing is what
        the mirroring pipe breaks.  Returns nothing; USB write errors are
        raised.
        """

        usbdlreg = brom_arm_value(timeout_s)
        usbdl_flag = misc_lock - 0x20
        frames = [
            ("watchdog disable", WDT_BASE, WDT_DISABLE_VALUE),
            ("misc unlock", misc_lock, MISC_MAGIC),
            ("wdt-resettable", misc_lock + 8, 1),
            ("misc relock", misc_lock, 0),
            ("usbdl BROM flag", usbdl_flag, usbdlreg),
            ("TOPRGU SWRST", WDT_BASE + WDT_SWRST_OFFSET, WDT_SWRST_KEY),
        ]
        for label, addr, value in frames:
            log("  send {} : 0x{:08X} <= 0x{:08X}".format(label, addr, value))
            try:
                self._write(write32_frame(addr, value))
            except usb.core.USBError as error:
                # An error on the SWRST frame is fine: the SoC resets
                # before the transfer can complete.
                if label == "TOPRGU SWRST":
                    log("  SWRST transfer ended: {} (reset likely already "
                        "happened)".format(error))
                else:
                    raise PreloaderBridgeError(
                        "{} transfer failed: {}".format(label, error))
        log("Arm + reset frames sent (no response reads; verdict follows)")


def _watch_reset(deadline_s=50.0):
    """After the reset frames: classify what enumerates.

    Returns 'success' (0003), 'flag_ignored' (2000 returned after a gap),
    'writes_noop' (2000 never blinked), 'booted' or 'timeout'."""

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
                log("  preloader never blinked: the arm/reset frames had no "
                    "effect (tool accepted but gated?); NOT retrying other "
                    "addresses")
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
            dev.arm_brom_blind(misc_lock=candidate)
        except (PreloaderBridgeError, usb.core.USBError) as error:
            log("attempt {} failed before reset: {}".format(attempt, error))
            if attempt == 1:
                log("tool window may be spent; the preloader will boot the "
                    "OS (one boot_count consumed)")
            return False
        dev = None  # endpoints die at the reset; force fresh instance
        verdict = _watch_reset()
        if verdict == "success":
            return True
        if verdict != "flag_ignored":
            return False
    log("all misc_lock candidates ignored; try the keypad-GPIO "
        "button-equivalent next")
    return False
