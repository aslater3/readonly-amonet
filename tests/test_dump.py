from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import struct
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "modules"
DUMP_PATH = MODULES / "dump.py"
sys.path.insert(0, str(MODULES))

LOGGER_SPEC = importlib.util.spec_from_file_location("brom_diag", MODULES / "brom_diag.py")
assert LOGGER_SPEC and LOGGER_SPEC.loader
BROM_DIAG = importlib.util.module_from_spec(LOGGER_SPEC)
sys.modules[LOGGER_SPEC.name] = BROM_DIAG
LOGGER_SPEC.loader.exec_module(BROM_DIAG)

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
    DUMP.dump_partition(fake, tmp_path, partition, overwrite=False, completed=[])
    result = (tmp_path / "boot_a.bin").read_bytes()
    assert result == payload[100] + payload[101] + payload[102]
    assert not (tmp_path / ".boot_a.bin.part").exists()
    assert fake.writes == 0
    assert fake.reboots == 0


def test_dumper_source_contains_no_persistent_device_write_or_reboot() -> None:
    tree = ast.parse(DUMP_PATH.read_text())
    forbidden = {
        "emmc_write",
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
    assert "emmc_switch" in calls
    assert "kick_watchdog" in calls


def test_partition_name_output_is_sha256_stable(tmp_path: Path) -> None:
    partition = DUMP.Partition("misc", 7, 7)
    payload = b"M" * 512
    fake = FakeDevice({7: payload})
    DUMP.dump_partition(fake, tmp_path, partition, overwrite=False, completed=[])
    assert hashlib.sha256((tmp_path / "misc.bin").read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()


def test_dump_archive_contains_completed_partitions_only(tmp_path: Path) -> None:
    complete = tmp_path / "boot_a.bin"
    complete.write_bytes(b"boot")
    partial = tmp_path / ".system_a.bin.part"
    partial.write_bytes(b"partial")
    assert DUMP.update_dump_archive(tmp_path, [complete, partial])
    with tarfile.open(tmp_path / "dump.tar", "r") as archive:
        assert archive.getnames() == ["boot_a.bin"]


def test_log_archive_contains_all_run_logs(tmp_path: Path) -> None:
    (tmp_path / "dump.log").write_text("stdout/stderr\n", encoding="utf-8")
    (tmp_path / "amonet.log").write_text("payload log\n", encoding="utf-8")
    archive_path = DUMP.create_log_archive(tmp_path)
    with tarfile.open(archive_path, "r:gz") as archive:
        assert archive.getnames() == ["amonet.log", "dump.log"]


def test_special_boot_area_dump_uses_fixed_four_mib_geometry() -> None:
    partition = DUMP._special_area_partition("boot0")
    assert partition.first_lba == 0
    assert partition.sectors == 8192
    assert partition.bytes == 4 * 1024 * 1024


def test_logger_honors_run_log_environment_path(tmp_path: Path) -> None:
    logger_path = MODULES / "logger.py"
    logger_spec = importlib.util.spec_from_file_location("test_logger", logger_path)
    assert logger_spec and logger_spec.loader
    logger = importlib.util.module_from_spec(logger_spec)
    sys.modules[logger_spec.name] = logger
    logger_spec.loader.exec_module(logger)
    target = tmp_path / "amonet.log"
    previous = os.environ.get("AMONET_LOG_FILE")
    os.environ["AMONET_LOG_FILE"] = str(target)
    try:
        logger.log("test log line")
    finally:
        if previous is None:
            os.environ.pop("AMONET_LOG_FILE", None)
        else:
            os.environ["AMONET_LOG_FILE"] = previous
    assert "test log line" in target.read_text(encoding="utf-8")


def test_brom_status_1d1a_is_decoded_as_cache_issue() -> None:
    message = BROM_DIAG.describe_status(bytes.fromhex("1d1a"))
    assert "KAMAKIRI2_CACHE_ISSUE" in message
    assert "re-enter" in message


def test_upstream_status_error_is_hex_decoded_not_ascii_encoded() -> None:
    message = BROM_DIAG.describe_status_error(RuntimeError("status is 1d1a"))
    assert "status bytes 1d1a" in message
    assert "LE 0x1a1d" in message
    assert "KAMAKIRI2_CACHE_ISSUE" in message


def test_unknown_brom_status_is_reported_in_both_endiannesses() -> None:
    message = BROM_DIAG.describe_status(bytes.fromhex("efbe"))
    assert "LE 0xbeef" in message
    assert "BE 0xefbe" in message


def test_stage1_restores_usb_before_any_payload_io_and_has_no_uart_dependency() -> None:
    source = (ROOT / "brom-payload" / "stage1.c").read_text(encoding="utf-8")
    assert "0x11005000" not in source
    assert "low_uart_put" not in source
    restore_at = source.index("*(volatile uint32_t *)(usbdl_ptr[0] + 8)")
    response_at = source.index("send_usb_response(1, 0, 1)")
    sync_at = source.index("send_dword(0xA1A2A3A4)")
    assert restore_at < response_at < sync_at
