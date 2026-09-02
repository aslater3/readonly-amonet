"""Read-only BROM diagnostics shared by the dumper and the payload loader.

The BROM returns protocol status words as 16-bit little-endian values.  The
upstream amonet helpers format the raw bytes, which led to opaque errors such
as ``status is 1d1a``.  mtkclient maps the same wire bytes (little-endian
0x1A1D) to a known kamakiri2 failure, so decode them here and name the
recognised cases.
"""

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


def log_brom_identity(device) -> None:
    """Log read-only BROM identity and security config before the exploit."""

    try:
        hwcode = device.get_hw_code()
        log("BROM hardware code: 0x{:04x}".format(hwcode))
    except Exception as error:
        log(
            "BROM hardware code unavailable: {}: {}".format(
                type(error).__name__, error
            )
        )
        return

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
