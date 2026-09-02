#!/usr/bin/env python3
"""Read every non-empty user-area GPT partition through the BROM payload.

This module deliberately does not import ``functions.py``: that module also
contains the installer write/reboot helpers.  After payload loading, this
module only reads user-area eMMC blocks and kicks the watchdog.

Run from this repository's ``modules`` directory:

    python3 dump.py [output-directory]

The output directory contains ``<partition-name>.bin`` files plus run logs and
the ``dump.tar``/``logs.tar.gz`` sendable archives.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import struct
import sys
import tarfile
import traceback
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from device import Device
import brom_diag
from brom_diag import (
    BROM_LOG_NAME,
    HOST_CONTEXT_NAME,
    log_brom_identity,
)
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
DUMP_LOG_NAME = "dump.log"
AMONET_LOG_NAME = "amonet.log"
DUMP_ARCHIVE_NAME = "dump.tar"
LOG_ARCHIVE_NAME = "logs.tar.gz"
BOOT_AREA_SIZE = 4 * 1024 * 1024



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


class _Tee:
    """Write terminal output to the console and a persistent run log."""

    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()

    def isatty(self) -> bool:
        return any(stream.isatty() for stream in self.streams)


def _write_tar_atomic(archive_path: Path, members: Iterable[Path], gzip: bool) -> None:
    """Create a host-side archive atomically from the supplied files."""

    temporary = archive_path.with_name(f".{archive_path.name}.part")
    mode = "w:gz" if gzip else "w"
    with tarfile.open(temporary, mode) as archive:
        for member in sorted(members, key=lambda path: path.name):
            archive.add(member, arcname=member.name, recursive=False)
    os.replace(temporary, archive_path)


def update_dump_archive(output_dir: Path, completed: Iterable[Path]) -> bool:
    """Refresh ``dump.tar`` with completed partition files only."""

    members = [
        path
        for path in completed
        if path.parent == output_dir
        and path.is_file()
        and path.suffix == ".bin"
        and not path.name.startswith(".")
    ]
    if not members:
        return False
    _write_tar_atomic(output_dir / DUMP_ARCHIVE_NAME, members, gzip=False)
    return True


def create_log_archive(output_dir: Path) -> Path:
    """Bundle all dumper log files into one sendable gzip-compressed tar."""

    candidates = [
        output_dir / DUMP_LOG_NAME,
        output_dir / AMONET_LOG_NAME,
        output_dir / BROM_LOG_NAME,
        output_dir / HOST_CONTEXT_NAME,
    ]
    members = [path for path in candidates if path.is_file()]
    if not members:
        raise RuntimeError("no log files were produced")
    archive_path = output_dir / LOG_ARCHIVE_NAME
    _write_tar_atomic(archive_path, members, gzip=True)
    return archive_path


def _check_fixed_output_collisions(output_dir: Path, overwrite: bool) -> None:
    """Refuse to append to or replace prior run metadata accidentally."""

    if overwrite:
        return
    existing = [
        output_dir / name
        for name in (
            DUMP_LOG_NAME,
            AMONET_LOG_NAME,
            BROM_LOG_NAME,
            HOST_CONTEXT_NAME,
            DUMP_ARCHIVE_NAME,
            LOG_ARCHIVE_NAME,
            f".{DUMP_ARCHIVE_NAME}.part",
            f".{LOG_ARCHIVE_NAME}.part",
        )
        if (output_dir / name).exists()
    ]
    if existing:
        raise FileExistsError(
            "output directory contains artifacts from an earlier run: "
            + ", ".join(str(path) for path in existing)
            + "; use a new directory or --overwrite"
        )


def _special_area_partition(name: str) -> Partition:
    """Represent a 4 MiB eMMC hardware boot area as dumpable blocks."""

    return Partition(name, 0, (BOOT_AREA_SIZE // SECTOR_SIZE) - 1)


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


def dump_partition(
    device,
    output_dir: Path,
    partition: Partition,
    overwrite: bool,
    completed: list[Path],
) -> None:
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
    completed.append(destination)
    update_dump_archive(output_dir, completed)
    print(f"  {partition.name}: complete" + " " * 20, flush=True)


def _run_dump(output_dir: Path, overwrite: bool) -> int:
    """Run the device operation inside the already-configured log capture."""

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
    # Do not call emmc_switch(0): the payload implements that operation as an
    # MMC_SWITCH write to EXT_CSD.PARTITION_CONFIG.  If LBA 1 is not a user-area
    # GPT, fail closed instead of changing the device's partition-selection
    # state.
    partitions = parse_gpt(device)
    special_areas = [
        (1, _special_area_partition("boot0")),
        (2, _special_area_partition("boot1")),
    ]
    _check_output_collisions(
        output_dir,
        [*partitions, *(partition for _, partition in special_areas)],
        overwrite,
    )

    print(f"Found {len(partitions)} non-empty GPT partitions:", flush=True)
    for partition in partitions:
        print(
            f"  {partition.name}: LBA {partition.first_lba}..{partition.last_lba} "
            f"({partition.sectors} sectors)",
            flush=True,
        )

    completed: list[Path] = []
    for partition in partitions:
        dump_partition(device, output_dir, partition, overwrite, completed)

    for area_number, partition in special_areas:
        print(
            f"Selecting eMMC {partition.name} (area {area_number}); "
            "this is an allowed EXT_CSD partition-selection operation",
            flush=True,
        )
        device.emmc_switch(area_number)
        try:
            dump_partition(device, output_dir, partition, overwrite, completed)
        finally:
            # Leave the card's active access area at user, without rebooting.
            device.emmc_switch(0)
            print("Returned eMMC access area to user", flush=True)

    print(f"Completed {len(completed)} partition dumps in {output_dir}", flush=True)
    print(f"Partition archive: {output_dir / DUMP_ARCHIVE_NAME}", flush=True)
    return 0


def _run_probe() -> int:
    """Read-only BROM introspection: no payload, no exploit, no writes."""

    log("Starting read-only BROM probe")
    module_dir = Path(__file__).resolve().parent
    original_cwd = Path.cwd()
    try:
        os.chdir(module_dir)
        device = Device().find()
        device.allow_usb_reset = False
        try:
            # Probe mode includes the handshake itself: identity commands only
            # answer correctly once the BROM has left its echo state.
            device.handshake()
            log("Handshake")
            log_brom_identity(device)
        finally:
            device.allow_usb_reset = True
        print(
            "Probe complete; no payload was loaded and no writes were issued.",
            flush=True,
        )
    finally:
        os.chdir(original_cwd)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read every non-empty user-area GPT partition plus BOOT0/BOOT1 "
            "into partition_name.bin files"
        )
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
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="run read-only BROM identity/log probes and exit without dumping",
    )
    args = parser.parse_args(argv)

    output_dir: Path = args.output_directory.expanduser()
    if not output_dir.is_absolute():
        output_dir = (Path.cwd() / output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        _check_fixed_output_collisions(output_dir, args.overwrite)
    except FileExistsError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    dump_log = output_dir / DUMP_LOG_NAME
    amonet_log = output_dir / AMONET_LOG_NAME
    os.environ["AMONET_LOG_FILE"] = str(amonet_log)
    host_context = brom_diag.write_host_context
    result = 1
    try:
        with dump_log.open("w", encoding="utf-8", buffering=1) as log_file:
            tee_stdout = _Tee(sys.stdout, log_file)
            tee_stderr = _Tee(sys.stderr, log_file)
            with contextlib.redirect_stdout(tee_stdout), contextlib.redirect_stderr(tee_stderr):
                print(f"Run log: {dump_log}", flush=True)
                print(f"Output directory: {output_dir}", flush=True)
                try:
                    if args.probe_only:
                        result = _run_probe()
                    else:
                        result = _run_dump(output_dir, args.overwrite)
                except KeyboardInterrupt:
                    traceback.print_exc()
                    print("Interrupted; no reboot was requested by dump.py.", flush=True)
                    result = 130
                except BaseException:
                    traceback.print_exc()
                    result = 1
    finally:
        # Every run -- probe or dump, success or failure -- always produces a
        # full log bundle: both host context and the archive step run here.
        try:
            host_context()
        except Exception as error:
            print(f"ERROR: could not write host context: {error}", file=sys.stderr)
        try:
            log_archive = create_log_archive(output_dir)
            print(f"Log archive: {log_archive}")
        except Exception as error:
            print(f"ERROR: could not create log archive: {error}", file=sys.stderr)
            result = 1
    return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted; no device write or reboot was requested.", file=sys.stderr)
        raise SystemExit(130)
