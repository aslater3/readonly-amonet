# MT8516 Cupcake read-only partition dump

This guide uses the dedicated `modules/dump.py` entrypoint to read the
non-empty GPT partitions from an MT8516 device and save them on the host as
`<partition-name>.bin` files.

The partition list is read from the device's live GPT. The number of output
files is therefore device-dependent; it is commonly around 15, but the dumper
does not assume a fixed count.

## Scope and safety

`dump.py` is separate from the Cupcake installer path. Do **not** use
`modules/main.py`, `gpt-fix.sh`, or the fastboot scripts for this operation.

The dumper:

- loads the existing BROM stage-1 and stage-2 payloads into volatile device
  memory;
- reads GPT header/table sectors from the eMMC user area;
- reads each non-empty GPT partition one 512-byte sector at a time;
- writes the resulting files only on the host;
- periodically kicks the volatile watchdog while reading; and
- exits after the final partition.

The run directory contains these sendable artifacts:

- `dump.log` — complete stdout/stderr from the dumper;
- `amonet.log` — timestamped Amonet/payload log messages;
- `logs.tar.gz` — one compressed log bundle containing both log files; and
- `dump.tar` — an uncompressed tar archive containing every partition file
  completed so far.

`dump.tar` is refreshed after every completed partition. If the run is
interrupted or a later partition fails, the archive still contains the earlier
completed files and must be sent together with `logs.tar.gz`.

After payload loading, `dump.py` does not request an eMMC data write, RPMB
write, eMMC partition switch, fastboot flag, or reboot. The stage-2 payload
still contains dormant write/reboot command handlers inherited from the
upstream payload, but this program never sends those command values.

The dumped data may contain device-specific identity, calibration, keys, or
user data. Store it privately and do not publish the output directory without
reviewing its contents.

## Host prerequisites

Install the host tools needed by the repository:

```bash
sudo apt-get install build-essential gcc-arm-none-eabi python3-venv
```

Create an environment outside the repository if desired, then install the
Python requirements:

```bash
cd /path/to/readonly-amonet
python3 -m venv /tmp/readonly-amonet-venv
/tmp/readonly-amonet-venv/bin/python -m pip install -r requirements.txt
```

The dumper needs USB access to the MediaTek BROM device (`0e8d:0003`). Make
sure the active user can access the device through the host's udev rules, or
run the command with the required host permissions. Do not connect multiple
MediaTek targets at the same time.

## Build the BROM payloads

From the repository root:

```bash
make -C brom-payload clean all
```

The command must produce both required payload files:

```text
brom-payload/stage1/stage1.bin
brom-payload/stage2/stage2.bin
```

Verify them before connecting hardware:

```bash
test -s brom-payload/stage1/stage1.bin
test -s brom-payload/stage2/stage2.bin
```

`dump.py` checks for both files before it waits for a device. A missing payload
is a host-side setup error and does not start device access.

## Enter BROM and run the dumper

Keep the USB cable connected. Enter the target's MTK BROM mode using the
board-specific service/test-point procedure. The host should see `0e8d:0003`.

From the repository root, run:

```bash
python3 modules/dump.py /absolute/path/to/mt8516-stock-dump
```

For a local `dump/` directory, omit the argument:

```bash
python3 modules/dump.py
```

The payload loader displays its normal short-removal prompt. Follow the
prompt, remove the short when requested, and press Enter. The dumper then:

1. performs the BROM handshake;
2. loads stage 1 and stage 2;
3. reads and validates the primary GPT at LBA 1;
4. prints every non-empty user-area partition name and LBA range;
5. reads the user-area partitions as `<partition-name>.bin`;
6. refreshes `dump.tar` after each completed file and creates `logs.tar.gz`
   when the run ends.

RPMB and the eMMC hardware boot areas are not included. They require separate,
explicitly reviewed capture paths and are not ordinary user-area GPT
partitions.

Example output shape:

```text
Found N non-empty GPT partitions:
  <partition-name>: LBA <first>..<last> (<sectors> sectors)
...
Dumping <partition-name>: <sectors> sectors (<bytes> bytes) -> /path/<partition-name>.bin
  <partition-name>: complete
...
Completed N partition dumps in /path/mt8516-stock-dump
Partition archive: /path/mt8516-stock-dump/dump.tar
Log archive: /path/mt8516-stock-dump/logs.tar.gz
```

The final names come from the target GPT. For example, if the GPT contains a
partition named `system_a`, the output is:

```text
system_a.bin
```

The dumper refuses to overwrite an existing output file by default. Use a new
empty directory for each capture. If replacing an existing host-side capture
is intentional, pass:

```bash
python3 modules/dump.py --overwrite /absolute/path/to/mt8516-stock-dump
```

`--overwrite` only affects files on the host; it does not enable device writes.

## Output verification

After the command exits, inspect the generated artifacts:

```bash
find /absolute/path/to/mt8516-stock-dump -maxdepth 1 -type f -printf '%f\n' | sort
find /absolute/path/to/mt8516-stock-dump -maxdepth 1 -name '.*.part' -print
tar -tf /absolute/path/to/mt8516-stock-dump/dump.tar
tar -tzf /absolute/path/to/mt8516-stock-dump/logs.tar.gz
sha256sum /absolute/path/to/mt8516-stock-dump/*.bin > /absolute/path/to/mt8516-stock-dump/SHA256SUMS
```

The expected archive members are the completed `*.bin` files only. The log
archive contains `dump.log` and `amonet.log`. The program atomically renames
each completed temporary file to its final `<partition-name>.bin` name. If the
process is interrupted, any remaining `.part` file is an incomplete host-side
capture and must not be treated as a valid partition dump.

`dump.tar` is updated after each successful partition, so it is the file to
send when a run produced any completed dump. `logs.tar.gz` is the single file
to send for the complete textual run record; send both archives when asking for
help with a partial or failed capture.

## What is not included

This procedure dumps the non-empty GPT partitions from the eMMC **user area**.
It does not dump:

- eMMC BOOT0;
- eMMC BOOT1, where Amazon identity/IDME data may reside;
- RPMB; or
- unused/unallocated space outside GPT partitions.

Those areas require separate, explicitly reviewed capture paths and are not
included in `dump.tar`.

## If it stops or fails

- **Payload files missing:** run `make -C brom-payload clean all` again and
  confirm both `.bin` files exist.
- **No device found:** confirm BROM mode, the `0e8d:0003` USB identity, cable,
  udev permissions, and that no other process owns the device.
- **GPT validation failure:** stop and preserve the error. Do not run
  `gpt-fix.sh` as a recovery attempt; that script writes GPT data.
- **A partition leaves a `.part` file:** treat that partition as incomplete,
  preserve `dump.tar` and `logs.tar.gz`, and send both archives. Rerun into a
  new output directory after the device has been safely reset.
- **The command exits without rebooting:** this is expected. The dumper sends
  no reboot command. Return the device to its normal state using the target's
  separately reviewed reset/recovery procedure.
- **A log archive is reported:** send `logs.tar.gz` as the single compressed
  textual log bundle. If any partition completed, also send `dump.tar`.

## Host-only checks

The repository includes fake-device tests for GPT parsing, exact partition
output, atomic renaming, and the absence of device write/reboot calls:

```bash
uv run --with pyusb==1.0.2 --with pytest \
  python -m pytest -q tests/test_dump.py
```

These tests do not connect to or modify a device.
