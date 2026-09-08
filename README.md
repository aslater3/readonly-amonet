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
- reads the 4 MiB eMMC hardware boot areas as `boot0.bin` and `boot1.bin`;
- writes the resulting files only on the host;
- periodically kicks the volatile watchdog while reading; and
- exits after the final partition and returns the active eMMC access area to
  user.

The run directory contains these sendable artifacts:

- `dump.log` — complete stdout/stderr from the dumper;
- `amonet.log` — timestamped Amonet/payload log messages;
- `logs.tar.gz` — one compressed log bundle containing both log files; and
- `dump.tar` — an uncompressed tar archive containing every partition file
  completed so far, including `boot0.bin` and `boot1.bin`.

`dump.tar` is refreshed after every completed partition. If the run is
interrupted or a later partition fails, the archive still contains the earlier
completed files and must be sent together with `logs.tar.gz`.

After payload loading, `dump.py` does not request an eMMC data write, RPMB
write, fastboot flag, or reboot. It does perform the explicitly requested
read-only capture of BOOT0/BOOT1 by selecting those eMMC access areas and
returns to the user area after each one. The stage-2 payload still contains
dormant write/reboot command handlers inherited from the upstream payload, but
this program never sends those command values.

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

Stop any daemon that claims CDC-class serial interfaces before a run:

```bash
sudo systemctl stop ModemManager brltty 2>/dev/null || true
```

These drivers bind CDC interfaces regardless of VID:PID and can starve the
device's USB transmit path (`usbdl_flush timeout` loops in preloader logs are
the symptom), leaving nothing for the dumper's libusb handle to exchange.


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

### eMMC-short boots halt BEFORE USB init (verified 2026-09)

Shorting the eMMC clock line prevents boot0 loading, and the BROM UART shows
staged retries (`F3: 4000 00E0`, `F2: 3000 00A6`, progress `00→03`) ending in
`System halt!`. However, on this MT8167 BROM build that halt happens **before
the BROM initializes its USB download device**: `lsusb`/`dmesg` show no
`0e8d:0003`, and no host driver ever sees the device. A shorted halt is
therefore **not a usable BROM-mode entry** on its own.

Observed recovery behavior: removing the short lets the halted BROM continue
when power is cycled or the short is cleared, proceeding to load boot0
normally (`Jump to BL` on UART). This confirms the short genuinely blocks
boot0 and that the halt is recoverable — it just never reaches usbdl init.

### Button-based entry (when the button is reachable)

The proven `0e8d:0003` enumeration path on this device family is the
button/power-on method: hold the action button before and during power-on so
the BROM completes USB download initialization and waits for usbdl traffic
(`0e8d:0003` present, no timing pressure). If the button is not reachable,
BROM entry currently has no confirmed method on this unit; do not rely on
shorted halts.

### Preloader bridge entry when the button is unavailable (`--via-preloader`)

The preloader tool window (`0e8d:2000`, "Tool connection is unlocked" on
UART) enumerates on every normal power-on. Its real protocol was decoded
from a byte-level trace on this hardware: it is **not** the MediaTek
complement handshake. The tool sends a 5-byte preamble (`5e 0a ca d7 83`)
then ASCII `READY` every ~20 ms; amonet's `handshake2` for the sibling
Echo devices waits for `Y` and writes the mode command
`FACTFACT`, which asks the preloader to reboot into the factory fastboot
stage. The bridge therefore:

1. waits for `0e8d:2000` and detaches `cdc_acm` immediately (before the
   kernel eats the preamble);
2. reads until a `READY` token, performs the MediaTek complement
   handshake (`a0 0a 50 05` — UART confirms entry into the command
   phase via `usb_listen sync`), then sends `FACTFACT`;
3. classifies the re-enumeration:
   * `0e8d:0003` → BROM download mode; the dump continues immediately;
   * an `0e8d` device whose interfaces match the fastboot signature
     (`ff/42/03`) → factory fastboot stage; the dumper stops here
     (read-only scope) and asks you to report the state —
     fastboot use is a separate explicitly authorised workflow;
   * a non-fastboot `0e8d` gadget such as `0e8d:2008` ("AEOOT") →
     the device simply **booted its OS normally**; the mode request did
     not take effect and the verdict says so honestly;
   * `0e8d:2000` returns unchanged → command ignored: the bridge falls
     back on the same power-up to the volatile register arm (mtkclient
     `reset_to_brom`): blind `0xD4` WRITE32 frames — watchdog disable,
     misc unlock, watchdog-resettable, relock, usbdl flag (`0x444C`
     magic | timeout | enable | BROM-bit clear) at `misc_lock-0x20`,
     then TOPRGU SWRST — verified by the enumeration verdict, retrying
     `misc_lock` candidates `0x10001838`, `0x1000141C`, `0x1001a100`;
   * device disappears and nothing MTK returns → it booted normally.

**Framing finding (hardware-confirmed).** On this unit the tool counts
received bytes against a full frame: the UART printed
`USB_HANDSHAKE: should be 8 bytes less than 512 bytes` — exactly the
8 bytes of a raw `FACTFACT`. The Dot-era client writes the bare 8 bytes
and works; this preloader apparently buffers them waiting for the rest
of a 512-byte frame, times out (`usb listen timeout` →
`cannot detect tools!`) and boots normally. The bridge therefore sends
`FACTFACT` raw first, then — after a *booted OS* verdict and a fresh
power-cycle — retries it zero-padded to a full 512-byte frame. If even
a complete frame boots the OS, this preloader does not honour USB mode
requests at all.

**UART tool-sync fallback.** The same UART log shows the preloader
checking a *second* tool channel after USB fails:
`[TOOL] <UART> wait sync time 150ms->5ms` → `receieved data: ()`. It
waits for the MTK sync sequence (`a0 0a 50 05`) on the console port
(`0x11005000`, 921600 8N1) and boots when nothing answers. If that
channel answers with the byte-wise complements (`5f f5 af fa`) the
preloader stays in command mode — a button-free door. Use
`modules/uart_tool_sync.py <console-port>`: it is observe-only unless
you pass `--send-sync`, and it never sends anything beyond the four
sync bytes. Analyse whatever comes back before any follow-up command.

```bash
python3 modules/dump.py --via-preloader dump
```

Everything this path touches is volatile register state or a RAM mode
request — no flash, RPMB, or other persistent write. Override the
starting candidate with `--misc-lock 0x...` if you have evidence for a
different TOPRGU layout. `modules/bridge_diag.py` is the raw packet
observer used to decode this protocol; rerun it if behaviour changes.

From the repository root, run:

```bash
python3 modules/dump.py /absolute/path/to/mt8516-stock-dump
```

For a local `dump/` directory, omit the argument:

```bash
python3 modules/dump.py
```

The payload loader displays its normal short-removal prompt. Follow the
prompt, remove the short when requested, and press Enter. Stage 1 is USB-only:
it restores the BROM USB transmit pointer, completes the pending control
transfer, and sends its synchronization word without relying on the hardware
UART (whose clock/pin configuration is not guaranteed at BROM stage). The
dumper then:

1. performs the BROM handshake;
2. logs the BROM hardware code and target config (secure boot / SLA / DAA);
3. loads stage 1 and stage 2;
4. reads and validates the primary GPT at LBA 1;
5. prints every non-empty user-area partition name and LBA range;
6. reads the user-area partitions as `<partition-name>.bin`;
7. selects and reads the 4 MiB BOOT0 and BOOT1 areas as `boot0.bin` and
   `boot1.bin`; and
8. refreshes `dump.tar` after each completed file and creates `logs.tar.gz`
   when the run ends.

BOOT0/BOOT1 are included because this capture explicitly permits the required
MMC partition-selection operation. RPMB is not included: it uses authenticated
RPMB request/response handling rather than ordinary block reads and requires a
separate, explicitly reviewed capture path.

Example output shape:

```text
Found N non-empty GPT partitions:
  <partition-name>: LBA <first>..<last> (<sectors> sectors)
...
Dumping <partition-name>: <sectors> sectors (<bytes> bytes) -> /path/<partition-name>.bin
  <partition-name>: complete
...
Selecting eMMC boot0 (area 1); this is an allowed EXT_CSD partition-selection operation
Dumping boot0: 8192 sectors (4194304 bytes) -> /path/boot0.bin
  boot0: complete
Returned eMMC access area to user
Selecting eMMC boot1 (area 2); this is an allowed EXT_CSD partition-selection operation
Dumping boot1: 8192 sectors (4194304 bytes) -> /path/boot1.bin
  boot1: complete
Returned eMMC access area to user
Completed N+2 partition dumps in /path/mt8516-stock-dump
Partition archive: /path/mt8516-stock-dump/dump.tar
Log archive: /path/mt8516-stock-dump/logs.tar.gz
```

### Read-only probe mode

If a dump fails before the payload loads, or you want diagnostics without any
exploit attempt, run the probe first:

```bash
uv run --with pyusb==1.0.2 python modules/dump.py --probe-only /absolute/path/to/mt8516-stock-dump
```

The probe connects, logs the BROM identity, and exits. It performs no payload
load and no eMMC operation of any kind. Each run (probe or dump) now records:

- BROM hardware code, hardware sub-code, and hardware/software versions;
- target config (secure boot / SLA / DAA);
- MEID and SoC ID when the BROM answers;
- the BROM's internal UART debug log, saved as `brom-log.txt`;
- host-side context (Python, pyusb/libusb, lsusb view of the device), saved as
  `host-context.txt`; and
- all of the above inside `logs.tar.gz`.

Send `logs.tar.gz` from a probe run when a dump keeps failing before the
payload stage; the BROM log frequently shows the underlying error.

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

This procedure dumps the non-empty GPT partitions from the eMMC **user
area**, plus the two eMMC hardware boot areas. It does not dump:

- RPMB; or
- unused/unallocated space outside GPT partitions.

`boot0.bin` and `boot1.bin` are each read as 4 MiB (8192 512-byte sectors),
which is the MT8516 boot-area geometry used by this branch. RPMB is a separate
authenticated storage area and requires a different, explicitly reviewed
capture path; it is not included in `dump.tar`.

## If it stops or fails

- **Payload files missing:** run `make -C brom-payload clean all` again and
  confirm both `.bin` files exist.
- **No device found:** confirm BROM mode, the `0e8d:0003` USB identity, cable,
  udev permissions, and that no other process owns the device.
- **GPT validation failure:** stop and preserve the error. Do not run
  `gpt-fix.sh` as a recovery attempt; that script writes GPT data.
- **`status ... (KAMAKIRI2_CACHE_ISSUE)` during payload loading:** the BROM
  rejected the exploit command for this attempt. Power the device off, re-enter
  BROM mode, wait for `0e8d:0003`, and run the dumper again; retries after a
  fresh BROM entry are expected. Include `logs.tar.gz` from the failed run if
  it keeps happening.
- **`[Errno 32] Pipe error` right after `Load payload`:** expected. The
  injection intentionally STALLs the BROM's control endpoint when execution is
  diverted to the payload. It is not itself a failure; the next line
  (`Waiting for stage 1 to come online...`) decides the outcome. If that read
  returns `b''`, immediately run `--probe-only` **without power-cycling**:
  - re-enumerates as `0e8d:0003` with a fresh BROM banner -> the core reset;
    focus on the watchdog value toggle (`0x22000064` vs `0x22000000`).
  - no device / USB timeout -> the core wedged (hard fault, no reset).
  - handshake answers immediately -> the payload never took control and the
    BROM main loop is still running.
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
