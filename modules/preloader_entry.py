"""Preloader-stage bridge into BROM/fastboot mode (Amazon tool window).

Protocol: FULLY decoded on hardware 2026-09-07 via bridge_diag trace.
---------------------------------------------------------------------
The Amazon preloader tool window (0e8d:2000, "Tool connection is
unlocked") is NOT the MediaTek usbdl complement protocol. After the
endpoint opens it sends a 5-byte preamble (5e 0a ca d7 83) followed by
ASCII "READY" tokens every ~20 ms until it gets a command. It wants the
amonet "handshake2" flow -- the exact code sits in the amonet trees for
sibling Echo devices:

    modules/common.py:  handshake2(dev, cmd='FACTFACT')
        # look for start byte
        while c != b'Y': c = dev.read()
        dev.write(b'FACTFACT')

FACTFACT on Amazon MT81xx Echos asks the preloader to reboot into the
factory fastboot/LK stage. Whatever stage comes back is transport
evidence; the register-arm path below remains as a secondary attempt if
the tool never enters READY mode.

Bridge flow
-----------
1. find 0e8d:2000, detach cdc_acm FAST (before it eats the preamble),
2. read packets until a packet ends with 'Y' (end of a READY token),
3. send the mode string (default FACTFACT, override with tool_cmd),
4. watch enumeration:
     0e8d:0003   -> BROM download mode: success, dumper continues;
     fastboot    -> unlocked LK stage: success (report; flashing via
                    fastboot would be a separate authorised step);
     0e8d:2000 back -> command ignored by this preloader; fall through
                    to the volatile register arm on the same power-up;
     nothing     -> device booted normally.

Register arm (secondary, volatile only, mtkclient reset_to_brom):
  misc_lock=0xAD98, misc_lock+8=1, misc_lock=0,
  misc_lock-0x20 = 0x444C magic|timeout|EN|~BROM, TOPRGU SWRST.
  Written blind (no response reads; the READY stream makes response
  parsing meaningless), verified by the enumeration verdict.

No flash, RPMB, or persistent write anywhere in this module.
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
USBDL_BIT_EN = 0x00000001
USBDL_BROM = 0x00000002
USBDL_TIMEOUT_MASK = 0x0000FFFC
WDT_BASE = 0x10007000
WDT_DISABLE_VALUE = 0x22000064
WDT_SWRST_OFFSET = 0x14
WDT_SWRST_KEY = 0x1209
CMD_WRITE32 = 0xD4
DEFAULT_MISC_LOCK = 0x10002050
MISC_LOCK_FALLBACKS = (0x10002050, 0x10001838, 0x1000141C, 0x1001a100)
DEFAULT_TIMEOUT_S = 600
DEFAULT_TOOL_CMD = "FACTFACT"
READY_TOKEN = b"READY"
READ_TIMEOUT = 2


class PreloaderBridgeError(RuntimeError):
    pass


def _to_bytes(value, size):
    fmt = {1: ">B", 2: ">H", 4: ">I"}.get(size)
    if fmt is None:
        raise PreloaderBridgeError("invalid size {}".format(size))
    return struct.pack(fmt, value)


def brom_arm_value(timeout_s: int = DEFAULT_TIMEOUT_S) -> int:
    """USBdl flag value: magic + timeout + enable, 'by bootloader' clear."""

    timeout_s &= USBDL_TIMEOUT_MASK >> 2
    value = USBDL_MAGIC | ((timeout_s << 2) & USBDL_TIMEOUT_MASK)
    value |= USBDL_BIT_EN
    value &= ~USBDL_BROM
    return value


def misc_lock_candidates(primary=DEFAULT_MISC_LOCK):
    seen, ordered = set(), []
    for value in (primary,) + MISC_LOCK_FALLBACKS:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def write32_frame(addr, value):
    return (_to_bytes(CMD_WRITE32, 1) + _to_bytes(addr, 4)
            + _to_bytes(1, 4) + _to_bytes(value, 4))


class PreloaderDevice:
    def __init__(self, timeout=READ_TIMEOUT):
        self.timeout = timeout
        self.udev = None
        self.ep_in = None
        self.ep_out = None

    def find(self, deadline=None):
        log("Waiting for preloader device 0e8d:{:04x} (power-cycle the "
            "device when prompted)".format(PRELOADER_PID))
        while deadline is None or time.time() < deadline:
            udev = usb.core.find(idVendor=MTK_VID, idProduct=PRELOADER_PID)
            if udev is not None:
                self.udev = udev
                log("Found preloader device 0e8d:{:04x}".format(PRELOADER_PID))
                for intf in (0, 1):
                    try:
                        if udev.is_kernel_driver_active(intf):
                            udev.detach_kernel_driver(intf)
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

    def _read_packet(self, timeout_ms=None):
        chunk = self.ep_in.read(self.ep_in.wMaxPacketSize,
                                timeout_ms or self.timeout * 1000)
        if not len(chunk):
            raise PreloaderBridgeError("empty read from tool IN pipe")
        return bytes(chunk)

    def _write(self, data):
        for attempt in (1, 2):
            try:
                self.ep_out.write(data, self.timeout * 1000)
                return
            except usb.core.USBError as error:
                if error.errno == 5 and attempt == 1:
                    log("  OUT EIO: clearing endpoint halt and retrying")
                    try:
                        self.udev.clear_halt(self.ep_out)
                    except usb.core.USBError:
                        pass
                else:
                    raise

    def wait_ready(self, deadline_s=8.0):
        """amonet handshake2 semantics: read until a READY token lands.

        Trace-verified: preamble packet then ASCII 'READY' every ~20 ms.
        Any packet ending in 'Y' counts (amonet matches the 'Y' byte).
        """

        deadline = time.time() + deadline_s
        preamble_seen = None
        ready_count = 0
        while time.time() < deadline:
            try:
                packet = self._read_packet(timeout_ms=500)
            except usb.core.USBError as error:
                if error.errno == 110:
                    continue
                if error.errno == 5:
                    # The tool aborts its TX stream once its listen timer
                    # expires; treat EIO as "window closing".
                    raise PreloaderBridgeError(
                        "tool IN pipe died (window likely closing): {}"
                        .format(error))
                continue
            if preamble_seen is None:
                preamble_seen = packet
                log("  preamble: {}".format(packet.hex(" ")))
            if packet.endswith(b"Y") or packet.endswith(READY_TOKEN):
                ready_count += 1
                if ready_count >= 1:
                    log("  tool READY after {} READY token(s)".format(
                        ready_count))
                    return True
        raise PreloaderBridgeError("no READY token within {:.0f}s".format(
            deadline_s))

    def send_command(self, command, pad_to=0):
        payload = command.encode("ascii", "ignore")
        if pad_to:
            if len(payload) > pad_to:
                raise PreloaderBridgeError("command longer than frame")
            payload = payload.ljust(pad_to, b"\x00")
        log("  sending tool command {!r}{}".format(
            command, f" padded to {pad_to}B frame" if pad_to else ""))
        self._write(payload)

    # ---- secondary: volatile register arm (blind, verified by outcome) --
    def arm_brom_blind(self, misc_lock=DEFAULT_MISC_LOCK,
                       timeout_s=DEFAULT_TIMEOUT_S):
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
            log("  arm: send {} 0x{:08X} <= 0x{:08X}".format(
                label, addr, value))
            try:
                self._write(write32_frame(addr, value))
            except usb.core.USBError as error:
                if label == "TOPRGU SWRST":
                    log("  SWRST transfer ended: {} (reset may already have "
                        "happened)".format(error))
                else:
                    raise PreloaderBridgeError(
                        "{} transfer failed: {}".format(label, error))


def _usb_present(*pids):
    return any(usb.core.find(idVendor=MTK_VID, idProduct=p) is not None
               for p in pids)


FASTBOOT_CLASS, FASTBOOT_SUBCLASS, FASTBOOT_PROTOCOL = 0xFF, 0x42, 0x03


def _is_fastboot(device):
    """True iff any interface matches the Android fastboot USB signature.

    Fastboot is identified by interface class ff/42/03 (01 on some LK
    builds), NOT by PID: 0e8d:2008 is just this device's booted-OS
    gadget ("AEOOT") and must never be reported as fastboot.
    """

    try:
        cfg = device.get_active_configuration()
    except usb.core.USBError:
        return False
    for intf in cfg:
        if (intf.bInterfaceClass == FASTBOOT_CLASS
                and intf.bInterfaceSubClass == FASTBOOT_SUBCLASS
                and intf.bInterfaceProtocol in (FASTBOOT_PROTOCOL, 0x01)):
            return True
    return False


def _other_mtk_stages():
    """List (vid,pid) of every 0e8d device that is NOT preloader/BROM."""

    found = []
    for device in usb.core.find(find_all=True, idVendor=MTK_VID):
        if device.idProduct not in (PRELOADER_PID, BROM_PID):
            found.append(device.idProduct)
    return found


def _other_stage_is_fastboot():
    for device in usb.core.find(find_all=True, idVendor=MTK_VID):
        if device.idProduct in (PRELOADER_PID, BROM_PID):
            continue
        if _is_fastboot(device):
            return True
    return False


def _watch_verdict(deadline_s=60.0):
    """Classify enumeration after a command/reset.

    Returns 'brom', 'fastboot' (ff/42/03 interface signature only),
    'booted_os' (a non-BROM 0e8d device that is NOT fastboot, e.g. the
    2008/AEOOT FireOS gadget -> mode request did not gate the boot),
    'preloader_back', 'gone_booting', 'timeout'.
    """

    deadline = time.time() + deadline_s
    seen_gap = False
    gone_since = None
    other_since = None
    while time.time() < deadline:
        if _usb_present(BROM_PID):
            log("  verdict: BROM 0e8d:0003 is up")
            return "brom"
        others = _other_mtk_stages()
        if others:
            names = ", ".join("0e8d:{:04x}".format(p) for p in others)
            if _other_stage_is_fastboot():
                log("  verdict: FASTBOOT stage (ff/42/03) at {}".format(
                    names))
                return "fastboot"
            # Booted-OS gadget (e.g. 2008 AEOOT): hold the observation
            # open briefly in case fastboot enumerates right behind it,
            # then classify as a normal boot.
            other_since = other_since or time.time()
            if time.time() - other_since > 5.0:
                log("  verdict: {} is up but is not fastboot -> device "
                    "booted the OS normally (mode request did not take "
                    "effect)".format(names))
                return "booted_os"
        else:
            other_since = None
        if _usb_present(PRELOADER_PID):
            if seen_gap:
                log("  verdict: preloader came back -> mode request ignored")
                return "preloader_back"
            # still the same instance: mode request had no effect yet
            if time.time() > deadline - deadline_s + 25:
                log("  verdict: preloader never blinked -> command had no "
                    "effect at all")
                return "preloader_back"
        else:
            if not seen_gap:
                log("  device went away (mode change or reset in progress)")
            seen_gap = True
            gone_since = gone_since or time.time()
            if time.time() - gone_since > 35:
                log("  verdict: long gap with no MTK device -> booted to OS")
                return "gone_booting"
        time.sleep(0.25)
    log("  verdict: timeout")
    return "timeout"


def bridge_to_brom(misc_lock=DEFAULT_MISC_LOCK, tool_cmd=DEFAULT_TOOL_CMD,
                   first_window_deadline=None):
    """Tool-window bridge. Returns one of:

    'brom'      BROM 0003 reached (dumper continues),
    'fastboot'  factory fastboot reached (report; separate step),
    False       no mode achieved.
    """

    dev = PreloaderDevice()
    if not dev.find(deadline=first_window_deadline):
        return False

    # Stage 1: documented Amazon mode request over the READY protocol.
    # Hardware evidence: UART prints "USB_HANDSHAKE: should be 8 bytes
    # less than 512 bytes" exactly when we sent the 8-byte FACTFACT --
    # i.e. N is the byte count the tool received and it wants a FULL
    # 512-byte frame. A bare 8-byte write sits buffered until the listen
    # timer expires ("usb listen timeout / cannot detect tools!") and
    # the preloader boots normally. Try raw first (xyzz Dot behaviour),
    # then zero-padded to a 512-byte frame on a fresh window.
    frames = [0, 512] if len(tool_cmd.encode("ascii", "ignore")) < 512 \
        else [0]
    verdict = "timeout"
    for pad_to in frames:
        try:
            dev.wait_ready()
            dev.send_command(tool_cmd, pad_to=pad_to)
        except (PreloaderBridgeError, usb.core.USBError) as error:
            log("stage 1 (READY/{}, {} frame) failed: {}".format(
                tool_cmd, f"{pad_to}B" if pad_to else "raw", error))
            return False
        verdict = _watch_verdict()
        if verdict in ("brom", "fastboot"):
            return verdict
        booted = verdict in ("booted_os", "gone_booting")
        if booted and pad_to == 0 and len(frames) > 1:
            # Device booted normally after the raw 8-byte command: the
            # UART says exactly why (it waits for a full 512-byte
            # frame). Escalate the framing on a fresh window.
            log("raw command left the tool waiting for the rest of the "
                "512-byte frame; power-cycle the device and the run "
                "will retry with a padded 512-byte frame automatically")
            dev = PreloaderDevice()
            if not dev.find(deadline=time.time() + 240):
                log("no fresh tool window appeared; rerun and power-cycle")
                return False
            continue
        break               # preloader_back / timeout
    if verdict in ("booted_os", "gone_booting"):
        log("mode request did not gate the boot even as a full 512B "
            "frame -- this preloader does not honour it over USB")
        return False

    # Stage 2: volatile register arm, only if we can still talk to the
    # same-window preloader (it came back) -- same power-up, no OS boot.
    if verdict == "preloader_back":
        log("mode request ignored; falling back to volatile register arm")
        for candidate in misc_lock_candidates(misc_lock):
            fresh = PreloaderDevice()
            if not fresh.find(deadline=time.time() + 20):
                log("preloader window gone; power-cycle and rerun")
                return False
            log("=== register arm attempt: misc_lock=0x{:08X} ===".format(
                candidate))
            try:
                try:
                    fresh.wait_ready()      # stay polite; stream-tolerant
                except PreloaderBridgeError:
                    pass                    # blind frames anyway
                fresh.arm_brom_blind(misc_lock=candidate)
            except (PreloaderBridgeError, usb.core.USBError) as error:
                log("register arm failed: {}".format(error))
                return False
            verdict = _watch_verdict()
            if verdict in ("brom", "fastboot"):
                return verdict
            if verdict != "preloader_back":
                return False
    log("bridge ended with verdict: {}".format(verdict))
    return False
