# Minisa Windows BLE validation kit

Run this kit on the Windows PC physically connected to the two BLE ESP32-S3
boards. The remote Minisa server remains the canonical source repository; it
cannot access the PC's USB ports. No USB forwarding, server environment, Arduino
installation, or Minisa Core/camera code is needed on Windows.

This kit is a generated snapshot of the actual production v3 service and guided
validator, with prebuilt firmware. It does not implement a second RSSI algorithm.
Transfer the **entire directory**, or transfer and extract
`minisa-windows-validation.zip`. Keep its relative directories intact. Python
3.10-3.14 (standard 64-bit x86 CPython) with pip/venv/sqlite3 is required; Python
3.11 and 3.13 are tested. All pinned dependencies are included in `wheels/`, so
`pip install -r requirements.txt` installs **offline**, without contacting PyPI or
requiring a compiler. Windows ARM/32-bit/free-threaded Python builds are outside
this wheel bundle. Windows' normal
USB CDC driver supports the native USB configuration; a board not enumerating on
this PC needs local cable/connector/driver investigation, not a server COM scan.

## Setup on Windows

Open PowerShell in the extracted `minisa-windows-validation` directory. Recommended
setup creates a local `.venv`, avoiding changes to an Espressif Python installation:

```powershell
# With the Espressif Python installation:
.\setup.ps1 -Python 'C:\Espressif\tools\python\python.exe'
# Or with a normal Python installation:
# .\setup.ps1 -Python python
$python = '.\.venv\Scripts\python.exe'
```

Equivalent direct commands (also useful if PowerShell scripts are restricted):

```powershell
$basePython = 'C:\Espressif\tools\python\python.exe' # Or: 'python'
& $basePython -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip --disable-pip-version-check install -r requirements.txt
& $python verify_package.py
```

If you already have a suitable isolated Python environment, the minimal setup is
`python -m pip install -r requirements.txt`. Use that same interpreter for every
following command. The longer commands above also disable pip's optional online
version check; package installation itself uses only bundled wheels. Esptool runs
as a module of the selected interpreter; no hardcoded Arduino15 paths
are used in this bundle.

## Discover and identify the boards

```powershell
& $python validate_hardware.py --discover
# Equivalent wrapper: .\run.ps1 -Action Discover
```

Keep the camera board separate. Connect one BLE board at a time with a known
data-capable USB cable and its native USB connector. Re-run discovery after each
connection; record the COM port, serial number/hardware ID and physical label.
Label the intended scanner and beacon. VID/PID identifies a device family, not
its role. Never reuse historical COM numbers without checking. Discovery is
read-only and never selects or flashes a port.

## Flashing is a separate, explicit step

Included firmware settings: ESP32-S3, >=4 MiB flash, QIO 80 MHz bootloader,
DIO image header, hardware USB CDC enabled, PSRAM disabled. Confirm these settings
suit both boards before uploading. Each merged image replaces the selected board's
firmware/data. Close serial monitors and the validator before flashing.

```powershell
$scannerPort = Read-Host 'Positively identified scanner COM port'
$beaconPort = Read-Host 'Positively identified beacon COM port'
& $python flash_firmware.py --role scanner --port $scannerPort
if ($LASTEXITCODE -ne 0) { throw 'Scanner flash failed or was cancelled' }
& $python flash_firmware.py --role beacon --port $beaconPort
if ($LASTEXITCODE -ne 0) { throw 'Beacon flash failed or was cancelled' }
```

The helper verifies the firmware/source hashes, requires a currently enumerated
port and exact typed `flash scanner COMx` or `flash beacon COMy` confirmation,
and enforces ESP32-S3 chip type without `--force`. It never flashes automatically.
Upload output is saved as `firmware/<role>/flash-<UTC>.json`. These results must
be reviewed; shipped binaries are fully linked and image-checked but have NOT
yet been physically booted or radio-tested by this project.

If bootloader entry is required, use the board's BOOT/RESET procedure and discover
the port again. After flashing, re-discover: the running COM numbers may differ.

## Run guided physical validation

```powershell
& $python validate_hardware.py --discover
$scannerPort = Read-Host 'Running scanner COM port after flash/reset'
$beaconPort = Read-Host 'Running beacon COM port after flash/reset'
& $python validate_hardware.py --scanner $scannerPort --beacon $beaconPort
```

Or, with your identified port names and normal Python:

```powershell
python validate_hardware.py --scanner COMx --beacon COMy
```

The tool displays raw/median RSSI, fuzzy zone/presence, boundary support and timing,
fresh observation rate/age, stale warnings and timestamped transitions. Enter
completes each guided step; `note TEXT` adds an action/position annotation; `quit`
or Ctrl+C stops and writes a summary. Follow the minimum hold times and repeat
movement routes. It runs the real v3 serial handshake, algorithm, reconnect and
SQLite code. No metres or human-occupancy claims are inferred.

Omit `--beacon` when powering the beacon from a power bank; beacon USB boot status
then remains unverified. For an hour of stationary monitoring:

```powershell
& $python validate_hardware.py --scanner $scannerPort --beacon $beaconPort --monitor --seconds 3600
```

Read **PHYSICAL_VALIDATION.md** for the full PASS/FAIL/INCONCLUSIVE checklist,
boot tests, movement/holds/obstruction/range/reconnect/soak criteria and limits.
If reconnect changes COM number, explicitly select it in a new run. The tool
never guesses a replacement device. A successful capture is not blanket physical
validation. The shipped `config.beacon.json` retains production tuning; its old
port field is overridden by mandatory `--scanner`. Every run uses a fresh DB.

## Results and the canonical server

Results stay on the Windows PC under `validation-runs/<UTC>/`:

- `capture.jsonl`: production telemetry/errors, transitions, action notes and
  resource samples; optional beacon serial status.
- `events.db`: actual v3 SQLite transitions/health events.
- `summary.json`: counts, step assessments, integrity/resource results and gaps.
- `config.json`: exact configuration used.

Capture stops visibly at 512 MiB by default; `--max-log-mib` can increase this.
Return the whole run folders and `firmware/*/flash-*.json` files to the canonical
server for review. Do not merge test databases into the server's production DB.
Also retain `package-manifest.json` so reviewers know the exact code/image hashes.
For a convenient results ZIP, after stopping the validator:

```powershell
$results = @('.\validation-runs', '.\package-manifest.json')
$results += @(Get-ChildItem .\firmware\*\flash-*.json -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
Compress-Archive -Path $results -DestinationPath ("minisa-results-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.zip')
```

Keep code changes on the server; regenerate the kit for a new version instead of
maintaining a fork on Windows. No credentials, historical databases, camera files,
or unrelated Minisa services are included. `verify_package.py` detects accidental
transfer corruption; hashes do not provide cryptographic publisher authentication.

## Optional software checks

```powershell
& $python verify_package.py
& $python -m unittest discover -v
& $python -m esptool version
```

Tests use fake serial devices and do not probe or flash real hardware. Firmware
sources and provenance are under `firmware/`; rebuilding stays on the canonical
server using `beacon/build_firmware.py`. Full compiler/core/SDK installations and
ELF/map build intermediates are deliberately unnecessary for this Windows kit.
