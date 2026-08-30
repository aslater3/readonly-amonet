from __future__ import annotations

import ast
import hashlib
import importlib.util
import struct
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "modules"
DUMP_PATH = MODULES / "dump.py"
sys.path.insert(0, str(MODULES))
spec = importlib.util.spec_from_file_location("read_only_dump", DUMP_PATH)
assert spec and spec.loader
DUMP = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = DUMP
spec.loader.exec_module(DUMP)


class FakeDevice:
    def __init__(self, sectors: dict[int, bytes]):
        self.sectors = sectors
        self.reads: list[int] = []
        self.kicks = 0
        self.writes = 0
        self.reboots = 0

    def emmc_read(self, lba: int) -> bytes:
        self.reads.append(lba)
        return self.sectors[lba]

    def kick_watchdog(self) -> None:
        self.kicks += 1

    def emmc_write(self, *_args) -> None:
        self.writes += 1

    def reboot(self) -> None:
        self.reboots += 1


def make_gpt(partitions: list[tuple[str, int, int]], entry_count: int = 8) -> dict[int, bytes]:
    entry_size = 128
    table = bytearray(entry_count * entry_size)
    for index, (name, first, last) in enumerate(partitions):
        entry = bytearray(entry_size)
        entry[:16] = bytes.fromhex("00112233445566778899aabbccddeeff")
        struct.pack_into("<QQ", entry, 32, first, last)
        encoded_name = name.encode("utf-16le")[:72]
        entry[56 : 56 + len(encoded_name)] = encoded_name
        table[index * entry_size : (index + 1) * entry_size] = entry

    table_crc = DUMP.zlib.crc32(table) & 0xFFFFFFFF
    table_sectors = (len(table) + DUMP.SECTOR_SIZE - 1) // DUMP.SECTOR_SIZE
    header = bytearray(DUMP.SECTOR_SIZE)
    header[:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<Q", header, 24, 1)
    struct.pack_into("<Q", header, 32, 1000)
    struct.pack_into("<Q", header, 40, 34)
    struct.pack_into("<Q", header, 48, 900)
    header[56:72] = bytes.fromhex("102030405060708090a0b0c0d0e0f000")
    struct.pack_into("<Q", header, 72, 2)
    struct.pack_into("<I", header, 80, entry_count)
    struct.pack_into("<I", header, 84, entry_size)
    struct.pack_into("<I", header, 88, table_crc)
    header_crc_data = bytearray(header[:92])
    struct.pack_into("<I", header_crc_data, 16, 0)
    struct.pack_into("<I", header, 16, DUMP.zlib.crc32(header_crc_data) & 0xFFFFFFFF)

    sectors = {
        1: bytes(header),
        2: bytes(table[:512]),
        3: bytes(table[512:1024]),
    }
    return sectors


def test_parse_gpt_uses_declared_table_and_skips_empty_entries() -> None:
    fake = FakeDevice(make_gpt([("boot_a", 100, 101), ("system_a", 200, 203)]))
    partitions = DUMP.parse_gpt(fake)
    assert [(p.name, p.first_lba, p.last_lba) for p in partitions] == [
        ("boot_a", 100, 101),
        ("system_a", 200, 203),
    ]


def test_dump_partition_is_exact_and_atomic(tmp_path: Path) -> None:
    partition = DUMP.Partition("boot_a", 100, 102)
    payload = {100: b"A" * 512, 101: b"B" * 512, 102: b"C" * 512}
    fake = FakeDevice(payload)
    DUMP.dump_partition(fake, tmp_path, partition, overwrite=False)
    result = (tmp_path / "boot_a.bin").read_bytes()
    assert result == payload[100] + payload[101] + payload[102]
    assert not (tmp_path / ".boot_a.bin.part").exists()
    assert fake.writes == 0
    assert fake.reboots == 0


def test_dumper_source_contains_no_persistent_device_write_or_reboot() -> None:
    tree = ast.parse(DUMP_PATH.read_text())
    forbidden = {
        "emmc_write",
        "emmc_switch",
        "rpmb_write",
        "reboot",
        "flash_data",
        "flash_binary",
    }
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
    }
    assert not (calls & forbidden)
    assert "emmc_read" in calls
    assert "kick_watchdog" in calls


def test_partition_name_output_is_sha256_stable(tmp_path: Path) -> None:
    partition = DUMP.Partition("misc", 7, 7)
    payload = b"M" * 512
    fake = FakeDevice({7: payload})
    DUMP.dump_partition(fake, tmp_path, partition, overwrite=False)
    assert hashlib.sha256((tmp_path / "misc.bin").read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()
