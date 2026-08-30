#!/usr/bin/env python3
"""Read every non-empty user-area GPT partition through the BROM payload.

This module deliberately does not import ``functions.py``: that module also
contains the installer write/reboot helpers.  The only device operations used
here after payload loading are user-area block reads and watchdog kicks.

Run from this repository's ``modules`` directory:

    python3 dump.py [output-directory]

The output directory contains only files named ``<partition-name>.bin``.
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from device import Device
from load_payload import load_payload
from logger import log

SECTOR_SIZE = 0x200
GPT_HEADER_LBA = 1
GPT_SIGNATURE = b"EFI PART"
GPT_MIN_ENTRY_SIZE = 128
GPT_MAX_ENTRY_SIZE = 4096
GPT_MAX_ENTRIES = 4096
WATCHDOG_INTERVAL_BLOCKS = 32
SAFE_PARTITION_NAME = re.compile(r"^[A-Za-z0-9._+\-]+$")


@dataclass(frozen=True)
class Partition:
    """One non-empty GPT partition in user-area LBA coordinates."""

    name: str
    first_lba: int
    last_lba: int

    @property
    def sectors(self) -> int:
        return self.last_lba - self.first_lba + 1

    @property
    def bytes(self) -> int:
        return self.sectors * SECTOR_SIZE


class GptError(RuntimeError):
    """The target's user-area GPT is absent or internally inconsistent."""


def _read_blocks(device, start_lba: int, count: int) -> bytes:
    if start_lba < 0 or count < 0:
        raise ValueError("negative GPT read range")
    return b"".join(device.emmc_read(start_lba + offset) for offset in range(count))


def _parse_header(sector: bytes) -> dict[str, int | bytes]:
    if len(sector) != SECTOR_SIZE or sector[:8] != GPT_SIGNATURE:
        raise GptError("no valid primary GPT header at LBA 1")

    header_size = struct.unpack_from("<I", sector, 12)[0]
    if header_size < 92 or header_size > SECTOR_SIZE:
        raise GptError(f"invalid GPT header size: {header_size}")

    stored_crc = struct.unpack_from("<I", sector, 16)[0]
    crc_header = bytearray(sector[:header_size])
    struct.pack_into("<I", crc_header, 16, 0)
    actual_crc = zlib.crc32(crc_header) & 0xFFFFFFFF
    if actual_crc != stored_crc:
        raise GptError(
            "GPT header CRC mismatch: "
            f"expected 0x{stored_crc:08x}, got 0x{actual_crc:08x}"
        )

    entry_lba = struct.unpack_from("<Q", sector, 72)[0]
    entry_count = struct.unpack_from("<I", sector, 80)[0]
    entry_size = struct.unpack_from("<I", sector, 84)[0]
    table_crc = struct.unpack_from("<I", sector, 88)[0]
    last_lba = struct.unpack_from("<Q", sector, 48)[0]

    if entry_count == 0 or entry_count > GPT_MAX_ENTRIES:
        raise GptError(f"invalid GPT entry count: {entry_count}")
    if (
        entry_size < GPT_MIN_ENTRY_SIZE
        or entry_size > GPT_MAX_ENTRY_SIZE
        or entry_size % 8
    ):
        raise GptError(f"invalid GPT entry size: {entry_size}")
    if entry_lba == 0 or last_lba == 0:
        raise GptError("invalid GPT partition-table location")

    table_bytes = entry_count * entry_size
    table_sectors = (table_bytes + SECTOR_SIZE - 1) // SECTOR_SIZE
    if entry_lba + table_sectors - 1 > last_lba:
        raise GptError("GPT partition table extends past last usable LBA")

    return {
        "entry_lba": entry_lba,
        "entry_count": entry_count,
        "entry_size": entry_size,
        "table_crc": table_crc,
        "last_lba": last_lba,
        "table_sectors": table_sectors,
    }


def parse_gpt(device) -> list[Partition]:
    """Read and validate the primary GPT, returning non-empty partitions."""

    header = _parse_header(_read_blocks(device, GPT_HEADER_LBA, 1))
    table = _read_blocks(device, int(header["entry_lba"]), int(header["table_sectors"]))
    table_bytes = int(header["entry_count"]) * int(header["entry_size"])
    table = table[:table_bytes]
    actual_crc = zlib.crc32(table) & 0xFFFFFFFF
    if actual_crc != int(header["table_crc"]):
        raise GptError(
            "GPT partition-table CRC mismatch: "
            f"expected 0x{int(header['table_crc']):08x}, got 0x{actual_crc:08x}"
        )

    partitions: list[Partition] = []
    entry_size = int(header["entry_size"])
    for index in range(int(header["entry_count"])):
        entry = table[index * entry_size : (index + 1) * entry_size]
        if len(entry) != entry_size:
            raise GptError(f"short GPT entry {index}")
        if entry[:16] == b"\x00" * 16:
            continue

        first_lba, last_lba = struct.unpack_from("<QQ", entry, 32)
        if first_lba == 0 and last_lba == 0:
            continue
        if first_lba > last_lba or last_lba > int(header["last_lba"]):
            raise GptError(
                f"invalid LBA range for GPT entry {index}: "
                f"{first_lba}..{last_lba}"
            )

        raw_name = entry[56:128]
        try:
            name = raw_name.decode("utf-16le").split("\x00", 1)[0]
        except UnicodeDecodeError as error:
            raise GptError(f"invalid UTF-16 partition name at entry {index}") from error
        if not name:
            raise GptError(f"unnamed non-empty GPT entry {index}")
        if not SAFE_PARTITION_NAME.fullmatch(name):
            raise GptError(f"unsafe partition name {name!r}")
        if any(part.name == name for part in partitions):
            raise GptError(f"duplicate partition name {name!r}")

        partitions.append(Partition(name, first_lba, last_lba))

    if not partitions:
        raise GptError("GPT contains no non-empty partitions")
    return partitions


def _check_output_collisions(output_dir: Path, partitions: Iterable[Partition], overwrite: bool) -> None:
    for partition in partitions:
        destination = output_dir / f"{partition.name}.bin"
        temporary = output_dir / f".{partition.name}.bin.part"
        if not overwrite and (destination.exists() or temporary.exists()):
            raise FileExistsError(
                f"refusing to overwrite existing dump: {destination}; "
                "use --overwrite to replace it"
            )


def dump_partition(device, output_dir: Path, partition: Partition, overwrite: bool) -> None:
    """Dump one partition using block reads and an atomic host-side rename."""

    destination = output_dir / f"{partition.name}.bin"
    temporary = output_dir / f".{partition.name}.bin.part"
    mode = "wb" if overwrite else "xb"
    print(
        f"Dumping {partition.name}: {partition.sectors} sectors "
        f"({partition.bytes} bytes) -> {destination}",
        flush=True,
    )

    blocks_read = 0
    with open(temporary, mode) as output:
        for block in range(partition.sectors):
            data = device.emmc_read(partition.first_lba + block)
            if len(data) != SECTOR_SIZE:
                raise RuntimeError(
                    f"short read at {partition.name} LBA "
                    f"{partition.first_lba + block}: {len(data)} bytes"
                )
            output.write(data)
            blocks_read += 1
            if blocks_read % WATCHDOG_INTERVAL_BLOCKS == 0:
                device.kick_watchdog()
            if blocks_read == partition.sectors or blocks_read % 1024 == 0:
                print(
                    f"  {blocks_read}/{partition.sectors} sectors",
                    end="\r",
                    flush=True,
                )
        output.flush()
        os.fsync(output.fileno())

    if temporary.stat().st_size != partition.bytes:
        raise RuntimeError(
            f"size mismatch for {partition.name}: "
            f"{temporary.stat().st_size} != {partition.bytes}"
        )
    os.replace(temporary, destination)
    print(f"  {partition.name}: complete" + " " * 20, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read every non-empty user-area GPT partition into partition_name.bin files"
    )
    parser.add_argument(
        "output_directory",
        nargs="?",
        type=Path,
        default=Path("dump"),
        help="directory for partition_name.bin files (default: ./dump)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing host-side dump files",
    )
    args = parser.parse_args(argv)

    output_dir: Path = args.output_directory.expanduser()
    if not output_dir.is_absolute():
        output_dir = (Path.cwd() / output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    module_dir = Path(__file__).resolve().parent
    required_payloads = (
        module_dir.parent / "brom-payload" / "stage1" / "stage1.bin",
        module_dir.parent / "brom-payload" / "stage2" / "stage2.bin",
    )
    missing = [str(path) for path in required_payloads if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "BROM payload is not built; missing: " + ", ".join(missing)
        )

    log("Starting read-only partition dumper")
    original_cwd = Path.cwd()
    try:
        # load_payload.py retains the upstream relative paths.  Keep that
        # implementation untouched and make invocation independent of cwd.
        os.chdir(module_dir)
        device = Device().find()
        load_payload(device)
    finally:
        os.chdir(original_cwd)

    # The freshly initialized eMMC interface starts in its default user area.
    # Do not call emmc_switch(0) here: the payload implements that operation as
    # MMC_SWITCH writing EXT_CSD.PARTITION_CONFIG.  A dump must not issue any
    # eMMC switch or data-write command; if LBA 1 is not a user-area GPT, fail
    # closed instead of changing the device's partition-selection state.
    partitions = parse_gpt(device)
    _check_output_collisions(output_dir, partitions, args.overwrite)

    print(f"Found {len(partitions)} non-empty GPT partitions:", flush=True)
    for partition in partitions:
        print(
            f"  {partition.name}: LBA {partition.first_lba}..{partition.last_lba} "
            f"({partition.sectors} sectors)",
            flush=True,
        )

    for partition in partitions:
        dump_partition(device, output_dir, partition, args.overwrite)

    print(f"Completed {len(partitions)} partition dumps in {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted; no device write or reboot was requested.", file=sys.stderr)
        raise SystemExit(130)
