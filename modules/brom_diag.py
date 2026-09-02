"""Read-only BROM diagnostics shared by the dumper and the payload loader.

The BROM returns protocol status words as 16-bit little-endian values.  The
upstream amonet helpers format the raw bytes, which led to opaque errors such
as ``status is 1d1a``.  mtkclient maps the same wire bytes (little-endian
0x1A1D) to a known kamakiri2 failure, so decode them here and name the
recognised cases.
"""

import re
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone

from logger import log

BROM_STATUS_NAMES = {
    0x1A1D: "KAMAKIRI2_CACHE_ISSUE",
}

CACHE_ISSUE_HINT = (
    "Kamakiri2 failed, cache issue: the BROM range-check override did not take "
    "effect during this attempt. Power the device off, re-enter BROM mode, wait "
    "for 0e8d:0003, and run the dumper again; retries after a fresh BROM entry "
    "are normal."
)

BROM_LOG_NAME = "brom-log.txt"
HOST_CONTEXT_NAME = "host-context.txt"


def describe_status(raw: bytes) -> str:
    """Turn a raw BROM status byte pair into a decisive error message."""

    big_endian = int.from_bytes(raw[:2], "big")
    little_endian = int.from_bytes(raw[:2], "little")
    name = BROM_STATUS_NAMES.get(little_endian, BROM_STATUS_NAMES.get(big_endian))
    suffix = f" ({name})" if name else ""
    message = (
        f"BROM rejected the command: status bytes {raw.hex()} "
        f"(LE 0x{little_endian:04x} / BE 0x{big_endian:04x}){suffix}"
    )
    if little_endian == 0x1A1D or big_endian == 0x1A1D:
        message += f". {CACHE_ISSUE_HINT}"
    return message


def describe_status_error(error: RuntimeError) -> str:
    """Decode the raw status pair embedded in an upstream RuntimeError."""

    message = str(error)
    match = re.search(r"\bstatus(?: bytes)?(?: is)? ([0-9a-fA-F]{4})\b", message)
    if match is None:
        return message
    return describe_status(bytes.fromhex(match.group(1)))


def log_brom_identity(device) -> None:
    """Log read-only BROM identity and security config before the exploit.

    Must run after the BROM handshake: before it, the BROM is still in its
    command-echo state and every probe misreads.  Also guards each probe with
    a short-lived no-reset window so a slow/absent reply cannot damage the USB
    link (the upstream read path otherwise issues udev.reset() on timeout).
    """

    device.allow_usb_reset = False
    previous_timeout = device.timeout
    try:
        _log_brom_identity_unlocked(device)
    finally:
        device.timeout = previous_timeout
        device.allow_usb_reset = True


def _log_brom_identity_unlocked(device) -> None:
    """Run every probe with the USB reset guard engaged."""

    try:
        hwcode = device.get_hw_code()
        log("BROM hardware code: 0x{:04x}".format(hwcode))
    except Exception as error:
        log(
            "BROM hardware code unavailable: {}: {}".format(
                type(error).__name__, error
            )
        )

    try:
        hw_sub_code, hw_ver, sw_ver = device.get_hw_dict()
        log(
            "Hardware versions: hw_sub_code=0x{:04x} hw_ver=0x{:04x} "
            "sw_ver=0x{:04x}".format(hw_sub_code, hw_ver, sw_ver)
        )
    except Exception as error:
        log(
            "Hardware versions unavailable: {}: {}".format(
                type(error).__name__, error
            )
        )

    try:
        secure_boot, sla, daa = device.get_target_config()
        log(
            "Target config: secure_boot={} sla={} daa={}".format(
                int(secure_boot), int(sla), int(daa)
            )
        )
    except Exception as error:
        log(
            "Target config unavailable: {}: {}".format(
                type(error).__name__, error
            )
        )

    for name, getter in (("MEID", device.get_me_id), ("SoC ID", device.get_soc_id)):
        try:
            value = getter()
            log("{}: {}".format(name, value.hex().upper()))
        except Exception as error:
            log(
                "{} unavailable: {}: {}".format(
                    name, type(error).__name__, error
                )
            )

    try:
        brom_log = device.get_brom_log()
        if brom_log:
            log(
                "BROM internal log: {} bytes (saved to {})".format(
                    len(brom_log), BROM_LOG_NAME
                )
            )
            _save_brom_log(brom_log)
        else:
            log("BROM internal log: empty (command supported, no data)")
    except Exception as error:
        log(
            "BROM internal log unavailable: {}: {}".format(
                type(error).__name__, error
            )
        )


def _save_brom_log(brom_log: bytes) -> None:
    """Persist the raw BROM UART log next to the other run logs."""

    log_file = os.environ.get("AMONET_LOG_FILE")
    if not log_file:
        return
    output_dir = os.path.dirname(os.path.abspath(log_file))
    text = "".join(
        chr(byte) if 0x20 <= byte < 0x7F or byte in (0x0A, 0x0D, 0x09) else "."
        for byte in brom_log
    )
    with open(os.path.join(output_dir, BROM_LOG_NAME), "w", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def write_host_context() -> None:
    """Record host-side facts that make a user-supplied log bundle decisive."""

    lines = [
        "timestamp: {}".format(datetime.now(timezone.utc).isoformat()),
        "python: {} ({})".format(sys.version.split()[0], sys.executable),
        "platform: {} {}".format(platform.system(), platform.release()),
        "machine: {}".format(platform.machine()),
    ]
    try:
        import usb
        import usb.backend.libusb1 as libusb1

        lines.append("pyusb: {}".format(getattr(usb, "__version__", "unknown")))
        backend = libusb1.get_backend()
        lines.append(
            "libusb1 backend: {}".format("present" if backend else "missing")
        )
    except Exception as error:
        lines.append("pyusb probe failed: {}: {}".format(type(error).__name__, error))
    try:
        lsusb = subprocess.run(
            ["lsusb", "-d", "0e8d:"], capture_output=True, text=True, timeout=5
        )
        detected = lsusb.stdout.strip() or "(no 0e8d device visible right now)"
        lines.append("lsusb 0e8d: {}".format(detected.replace("\n", "; ")))
    except Exception:
        lines.append("lsusb: not available")

    path = host_context_path()
    if path is None:
        return
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    log("Host context written to {}".format(HOST_CONTEXT_NAME))


def host_context_path():
    """Return the run directory's host-context path, or None outside a run."""

    log_file = os.environ.get("AMONET_LOG_FILE")
    if not log_file:
        return None
    return os.path.join(
        os.path.dirname(os.path.abspath(log_file)), HOST_CONTEXT_NAME
    )
