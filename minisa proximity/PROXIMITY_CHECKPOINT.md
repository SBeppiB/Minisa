# BLE proximity checkpoint — 2026-10-08

## Current deployment correction — remote server + Windows USB kit

The user clarified that the canonical Minisa repository runs on a remote server.
The two ESP32-S3 boards attach to the user's separate Windows PC. **The server
cannot directly access those USB devices.** Earlier server-side PnP/COM results
do not diagnose the PC's cables, drivers, physical connection or accessibility.
The historical hardware-access conclusions below are superseded by this topology.
No further real COM enumeration or device opening was performed in this pass;
transport/discovery tests used mocks only.

Deliverables:

- `dist/minisa-windows-validation/`: standalone Windows directory.
- `dist/minisa-windows-validation.zip`: transfer this single ZIP and extract it.
- `dist/minisa-windows-validation.zip.sha256`: optional transfer checksum.
- The complete kit includes the **unchanged production algorithm**, actual v3
  service/validator/discovery code, both configs, SQLite schema, 51 runtime tests,
  shared portable flasher, both 4 MiB merged images plus firmware sources/hash
  manifests, provenance, integrity checker, PowerShell scripts and instructions.
- Pinned offline wheels cover standard Windows x64 CPython 3.10-3.14. No Arduino
  installation, server environment, network access for dependencies, camera/Core
  code, credentials or historical databases are required on the Windows PC.

Canonical additions: `windows_validation/` holds README, requirements, setup/run
PowerShell templates and integrity checker. `beacon/package_windows.py` assembles
an explicit allowlist of exact production files and verified artifacts; it never
includes runtime data or copies an entire repository. `prepare_windows_wheels.py`
recreates the offline dependency directory. `test_package_windows.py` has eight
tests for portable paths/interpreter choice, exact-copy ZIP contents, overwrite
refusal, invalid firmware, tampered/missing files and unsafe manifest paths.

Shared `beacon/flash_firmware.py` now supports both canonical and flat kit layouts.
In the kit it runs `sys.executable -m esptool`, pinned to 5.3.1; the canonical path
retains its installed-executable fallback. Source/image SHA checks, explicitly
enumerated port, typed role/port confirmation and ESP32-S3 enforcement remain.
No algorithm/tuning changes. Validator's checklist path is now module-relative;
runtime tests also resolve the flat portable layout. Full firmware was reused,
not unnecessarily rebuilt. The server remains canonical; regenerate snapshots
rather than creating a separate algorithm implementation on Windows.

Validation:

- Canonical suite: **78 tests** = 19 v2 + 51 v3 + 8 packaging tests.
- Extracted-kit suite: all **51 tests pass** in Python 3.11.15 based on
  `C:\Espressif\tools\python\python.exe`; all 51 also pass in a clean Python 3.13
  environment. These are fake-transport software checks, not physical tests.
- Both environments successfully install bundled dependencies offline, pass
  `pip check`, run esptool 5.3.1, and verify packaged files/firmware manifests.
- ZIP extraction/integrity tested; production service bytes match canonical source.
- Initial online install in the Espressif-derived venv encountered a certificate
  verification failure. Bundled wheels resolve this without disabling TLS checks.
  Setup disables pip's optional online version check as well.
- PowerShell scripts parse successfully, but this session's execution policy
  disallows `.ps1` execution. Equivalent direct commands were tested instead; no
  execution-policy weakening is required. README includes both workflows.
- CPython 3.10/3.12/3.14 wheel compatibility was resolved/downloaded; their runtime
  interpreters were not available for executing tests. 32-bit/ARM/free-threaded
  Python are outside the bundled wheel targets.

Exact next actions **on the Windows PC**, from the extracted directory:

```powershell
$basePython = 'C:\Espressif\tools\python\python.exe' # Or 'python'
& $basePython -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip --disable-pip-version-check install -r requirements.txt
& $python verify_package.py
& $python validate_hardware.py --discover
# Identify each BLE board by connecting individually; keep camera separate.
$scannerPort = Read-Host 'Positively identified scanner COM port'
$beaconPort = Read-Host 'Positively identified beacon COM port'
& $python flash_firmware.py --role scanner --port $scannerPort
if ($LASTEXITCODE -ne 0) { throw 'Scanner flash failed/cancelled' }
& $python flash_firmware.py --role beacon --port $beaconPort
if ($LASTEXITCODE -ne 0) { throw 'Beacon flash failed/cancelled' }
& $python validate_hardware.py --discover
# Recheck running port numbers after reset, then:
& $python validate_hardware.py --scanner $scannerPort --beacon $beaconPort
```

Where scripts are permitted: `setup.ps1 -Python <interpreter>`, then
`run.ps1 -Action Discover`, `-Action FlashScanner -Scanner COMx`,
`-Action FlashBeacon -Beacon COMy`, or `-Action Validate -Scanner COMx -Beacon COMy`.
The package README and PHYSICAL_VALIDATION.md contain all guided steps and explicit
criteria. Results stay on Windows in `validation-runs/<UTC>/` (JSONL, SQLite,
config, summary); return these directories, package manifest and upload JSON logs
to the server. Never merge test databases into production history.

**Physical status remains NOT VALIDATED.** Neither firmware flashing nor live BLE
experiments on the Windows-attached boards have occurred. The next external action
is transferring/extracting the ZIP on the Windows PC and running local setup and
discovery, not reconnecting USB to the server or granting server device access.

## Earlier hardware-readiness follow-up (historical; topology superseded above)

**70 tests pass: 19 v2 + 51 v3. All original 55 tests are preserved.** No RSSI
threshold, filter, evidence gate or state-machine tuning changed. The prior
576,000-observation soak was not repeated unnecessarily.

**Both firmware projects now fully compile, link, and generate flash images.**
Arduino CLI still fails under this session's filesystem restrictions, including
with isolated data/cache directories. A new direct platform/SDK build route
compiles all 249 Arduino-core/NimBLE translation units plus each sketch, archives
them, links the installed ESP32-S3 SDK, and generates bootloader, partition,
application, and merged images. This is a full linked build, not the earlier
object-only check and not a claim that Arduino CLI now works.

**No boards are currently USB-enumerated as connected. Nothing was flashed. The
beacon path remains NOT physically validated.** The only current external blocker
to starting the prepared hardware workflow is getting the BLE boards enumerated
over USB and identifying their intended roles. Next user action: connect the two
BLE boards through known data-capable USB cables to their native USB connectors,
keeping the camera board separate, and identify scanner versus beacon.

### Hardware investigation

Read-only methods used: pyserial/Windows SetupAPI, `pnputil` connected/disconnected
Ports enumeration, connected USB bus enumeration, device problem enumeration,
serial registry map, historical USB registry entries, Get-PnpDevice/CIM, Arduino
CLI discovery. Saved report: `.build/hardware-access.json`.

- `pnputil /enum-devices /connected /class Ports`: no connected serial devices.
- Disconnected devices include Espressif VID 303A/PID 1001 serial interfaces at
  historical COM9 and COM10, and VID 303A/PID 4001 at historical COM7. Their
  installed driver is Microsoft `usbser.inf`. These numbers do NOT identify the
  two BLE board roles or authorize flashing them. Historical COM8 is VID 1A86.
- Connected USB enumeration works: input devices, root hub, and Intel Bluetooth
  are visible. No connected Espressif device appeared. Thus there is no evidence
  that this environment hides all physical USB devices.
- Driver-problem enumeration found a ROOT/HIDCLASS device with code 28; no
  connected Espressif driver problem was reported. No driver changes were made.
- Serial registry map had no active entries. Historical USB registry keys match
  the disconnected Espressif devices.
- Get-PnpDevice and CIM return access denied. These WMI-backed methods are
  restricted here; PnP Utility supplies useful Device Manager-equivalent evidence.
- Final PnP recheck still found no connected serial devices. No board mapping or
  newly connected hardware was supplied during this pass.

| Requested classification | Finding |
| --- | --- |
| Physically absent | Not provable remotely; Windows reports no live enumeration |
| Present, driver missing | No such ESP32 evidence; historical CDC driver is installed |
| Present, inaccessible | Not demonstrated; no present ESP32 device to open |
| Visible to Windows, hidden here | No evidence from working PnP enumeration; WMI access itself is denied |
| Unknown | Cable/data capability, connector, power, physical presence and board role remain unknown |

The precise observed obstacle is **no active ESP32 USB enumeration**, not a proven
missing driver or inaccessible COM handle. Physical cable/power/connector causes
cannot be distinguished without connecting a board. Do not state a more specific
root cause than this evidence supports.

### Arduino15 diagnosis and successful full-build route

Installed and readable: ESP32 Arduino 3.3.12, NimBLE-Arduino 2.5.1, Xtensa compiler
2601, S3 SDK libraries 3.3.12, esptool 5.3.1. Arduino CLI is 1.5.1. The IDE YAML
contains no custom directories; CLI config dump was empty (defaults). Arduino15
ACLs were readable and showed user ownership/full control; no ACL edits were made.
The packages directory itself is an ordinary directory, not a symlink.

CLI's `following symlink ... packages: Access is denied` occurs even for a copied
ordinary directory inside the writable workspace. The original default setup also
failed temporary extraction outside the writable root. An isolated `.build` copy
of the core, S3 SDK/compiler, esptool, builtin tools and NimBLE allowed builtin tool
installation to succeed, but CLI package discovery still failed. This isolates
the remaining CLI problem to its directory-resolution/access path in this
restricted environment; the exact failing Windows API was not traced. It does
not mean the ESP32 package is absent or corrupted.

Safe resolution: `beacon/build_firmware.py` directly executes the installed
platform's compiler/SDK/link/image-generation recipes using argument arrays.
It writes only `.build/full`, preserves the installation, and uses fixed S3
hardware CDC, no PSRAM, QIO 80 MHz bootloader, DIO image header, default 4 MiB layout.
Source requires no sketch prototype generation. The linker checks the memory
layout; generated application size is checked against the default app partition.
Manifests are invalidated before a rebuild, and regenerated only on success.

| Artifact | Location | Application size |
| --- | --- | ---: |
| Scanner merged image | `.build/full/scanner/scanner.merged.bin` | 551,312 bytes |
| Beacon merged image | `.build/full/beacon/beacon.merged.bin` | 548,096 bytes |

Each merged image is 4,194,304 bytes. Each role directory also contains ELF/map,
bootloader, partition table, application binary and a SHA-256 manifest.
`.build/full/build.log` records the full compile/link commands and diagnostics.
Esptool inspection of both applications and both bootloaders reported valid
checksums/hashes. Source/image manifests and merged binary offsets were also
verified for both roles; detailed inspection is in `.build/full/image-checks.txt`.
Application headers report ESP32-S3, 4 MB/80 MHz/DIO. Runtime boot/reception remains untested. Confirm actual board
flash/native USB compatibility before upload. The isolated CLI experiment uses
roughly 2 GiB under `.build`; no installed files were deleted or changed.

### Tooling, tests and exact next commands

New files:

- `v3/validate_hardware.py`: one terminal, guided steps, Enter completion, human
  notes, live raw/median/state/evidence/rate/age, stale warnings, UTC transitions,
  fresh per-run SQLite and JSONL, optional beacon status monitoring, resource
  samples and compact JSON summary. Runs the actual production `run()` function;
  no duplicated proximity algorithm. Capture has a visible stop-at-size limit.
- `v3/hardware_access.py`: read-only candidate metadata and multiple Windows
  detection methods, retaining errors rather than treating denial as absence.
- `beacon/build_firmware.py`: full reproducible build fallback described above.
- `beacon/flash_firmware.py`: one explicitly selected role/port per invocation,
  current port enumeration, source/image hash checks, typed role confirmation,
  ESP32-S3 enforced by esptool, upload result saved. Never scans-and-flashes.
- `v3/PHYSICAL_VALIDATION.md`: complete workflow and prespecified PASS/FAIL/
  INCONCLUSIVE checklist. Read it before the physical experiment.
- `v3/test_validation.py`: 15 new tests covering production capture + SQLite,
  cooperative shutdown, reconnect wait interruption, stale/offline display,
  annotations/incomplete stages, capture limits, criteria, metadata-only discovery,
  access errors, missing-hardware summary, and four flash safety cases.

Modified: production v3 adds only optional cooperative `stop_event` parameters to
`run`/`read_session`; normal callers retain previous behavior. README files and
this checkpoint document the workflow; `.gitignore` excludes generated run data.
No firmware source, v1/v2 service, tuning/config/schema, camera or cognition edits.

Tests caught a Windows SQLite handle left open by the first draft's summary read;
it was fixed with explicit connection closure and retested. A real 3-second tool
smoke run against deliberately nonexistent COM99 displayed offline/unknown/STALE,
saved one offline row with SQLite integrity `ok`, reported zero observations and
NOT VERIFIED reception, and exited 1. This is a negative tool test, not hardware
validation. Artifacts are under `.build/tool-smoke/`.

Run from the workspace root:

```powershell
$python = 'C:\Users\endle\AppData\Local\Programs\Python\Python313\python.exe'
# First command immediately after boards are connected:
& $python v3/validate_hardware.py --discover
# Existing full images are ready; rebuild only if sources/toolchain change:
& $python beacon/build_firmware.py
# Replace placeholders only after identifying each BLE board:
& $python beacon/flash_firmware.py --role beacon --port COMy
& $python beacon/flash_firmware.py --role scanner --port COMx
& $python v3/validate_hardware.py --discover
& $python v3/validate_hardware.py --scanner COMx --beacon COMy
```

For a power-bank beacon omit `--beacon`. For an unattended hour capture use
`--monitor --seconds 3600`. Default guided soak is 600 seconds; increase with
`--soak-seconds 3600`. Commands, configuration, data limits and manual review
requirements are detailed in `v3/PHYSICAL_VALIDATION.md`. Flash helpers never
choose a board from VID/PID alone. Current reconnect follows the configured port;
a changed COM number requires re-discovery and explicit restart.

### Physical checklist — all still pending

- [ ] Identify boards/roles and flash both; retain upload results.
- [ ] Three scanner and beacon boot cycles, no errors, ACK/status within 10 s.
- [ ] Exact BEACON ACK and >=10 fresh matching observations over >=2 s.
- [ ] PRESENT persistence; timestamped raw/median/evidence stream; stale warning.
- [ ] >=30 s stationary baseline and holds; no false gone/offline or >5 s gap;
      at most two zone changes per minute (minimum allowance two).
- [ ] Three approach/retreat routes with pauses; directional evidence/crossings.
      No sufficient RF crossing means INCONCLUSIVE, not forced PASS.
- [ ] Obstruction/restoration: no crash, fresh observations recover.
- [ ] Actual range exit: gone within 10 s after last report, healthy scanner;
      return: present and >=10 fresh reports within 15 s.
- [ ] Scanner disconnect >=6 s then reconnect: offline, re-ACK, online, fresh data.
- [ ] Logs/annotations match persisted transitions; SQLite integrity passes.
- [ ] >=10 min (prefer >=1 h) stationary soak; review CPU, memory, storage growth,
      stalls and oscillation against the limits in the checklist document.

The summary never automatically certifies all physical criteria. No received data
can prove person occupancy, authenticated identity, or exact physical distance.

## Previous pass status (historical; superseded above)

Software audit and focused v3 hardening completed. Baseline independently verified:
19 v2 tests + 28 v3 tests passed before edits. Final: **19 v2 + 36 v3 = 55 passing**.
Both firmware sketches compile to object files using installed ESP32 Arduino
3.3.12 and NimBLE-Arduino 2.5.1. Full Arduino builds were attempted for each sketch
and failed during toolchain initialization because access was denied. No firmware
was flashed. **The beacon radio path is NOT physically validated.**

Pyserial enumeration twice returned `no ports found`. Arduino CLI also reported
no boards, but its initialization failed, so that result is not independent
hardware confirmation. Windows CIM serial enumeration returned access denied.
This means no boards were accessible/detected in this session; it does not prove
that none were physically plugged in. User was asked to connect/identify the two
BLE boards and assist with movement. No board mapping was received during work.

## Repository audit

No repository-local AGENTS.md was found. This directory is not a Git repository;
`git status` failed. Filesystem changes are listed below instead of a Git diff.

| Location | Contents and findings |
| --- | --- |
| `v1/presence_node` | Original Python script: five-sample mean, fixed zones, older text protocol, serial retries. Original DB and JSONL retained. |
| `v2/` | README, adaptive EMA service, phone/beacon JSON configs, schema, pinned pyserial 3.5, 19 tests, shared scanner firmware. |
| `v3/` | README, rolling-median service, phone/beacon configs, schema, requirements, algorithm/debug/transport/target tests, existing DB. |
| `beacon/` | README with two-board workflow, non-connectable advertiser sketch, installed-toolchain object compilation helper. |
| `.build/` | Generated object/build artifacts; not evidence of a successful full link or flash. |
| root/v1/v3 databases | Historical artifacts preserved; not used as live evidence or migrated. |
| camera files and `camera_firmware/` | Recognized as excluded scope; not modified or tested. |

Read all BLE READMEs, both service implementations, v1 script, both sketches,
all BLE tests, configs, schemas, requirements and firmware-check helper. No
separate proximity checkpoint existed; prior validation was in the READMEs.

## Existing design verified

- Beacon: service UUID `c7a10001-5a1e-4b1e-9a11-4d494e495341`, name `Minisa-01`,
  configured 100 ms interval (160 BLE units), 0 dBm advertisement power, no pairing
  or connection, no scan response. Identity constants match the scanner source.
  Real cadence/power and UUID reception have not been measured over the air.
- Scanner: phone name matching with active scanning; beacon UUID matching with
  passive scanning. Scan window/interval 60/100 ms; repeated callbacks; latest
  observation at most once per 150 ms. No growing stored scan result list.
- Protocol at 115200 baud: host `TARGET:PHONE` or `TARGET:BEACON`, exact
  `TARGET:OK:<MODE>` acknowledgement, then `PRESENCE:PRESENT:<RSSI>`, `SCAN:OK`,
  `PRESENCE:GONE`; ASCII lines terminated by newline. No device timestamps.
- Target acknowledgement is required on every connection; wrong/pre-ack frames
  cannot establish presence. Selection is volatile. Stable UUID/name is an
  unauthenticated experimental target label, not proof of a person's identity.
- V2: EMA 0.4 / fast 0.85, 12 dB fast-change trigger, 3 dB hysteresis.
- V3: 1.5-second rolling median; minimum three samples, 75% support and 1-second
  supporting span per crossed boundary; 6 dB strong evidence permits 0.6 seconds;
  0.5 dB hysteresis; multi-zone crossings allowed. No RSSI-to-metres conversion.
- Presence is immediate on the first observation. Gone requires repeated firmware
  absence reports spanning 0.8 seconds, after firmware's four-second timeout.
  Heartbeats do not count as target evidence or prove absence.
- Transport reconnects after errors or five seconds without valid frames, with
  two-second retry delay. Online/offline is separate from target presence.
  Each session creates a fresh model; restart never reuses historical proximity.
- SQLite stores UTC transition/health events with parameterized inserts and three
  indexes. Raw RSSI is debug telemetry, not a per-observation DB write. SQLite
  failures propagate. There is no DB retention policy or live status API.
- Approach/retreat is represented by confirmed zone crossings, not a standalone
  trend channel. No calibrated confidence probability exists. Evidence counts and
  confirmation timing are available in debug records. No physical data justified
  a model change or tuning during this audit.

## Changes and before/after evidence

Only v3 software and BLE documentation changed; v1/v2 runtime, firmware, configs,
schemas, original databases, eyes/camera and cognition systems were preserved.

1. `v3/presence_service.py`: target handshake now reads through the ACK newline
   without consuming subsequent bytes. Regression reproduced loss of both a full
   first observation and a partial line before the fix; both now remain available.
2. Same file: one receipt timestamp per normal serial read. A regression with
   simulated slow processing previously turned a buffered burst into a false
   very_far-to-very_close transition; it now leaves the original zone unchanged.
   Co-batched observations remain visible in debug but do not vote twice.
3. Same file: debug now records GONE confirmation evidence, invalid frame bytes
   as bounded hex, and overlong-frame counts. Invalid input still cannot refresh
   sensor health. No unbounded malformed payload is retained.
4. Same file: optional `--port` override and `--log-file` rotating capture. Logs
   include startup configuration. Rotation: 10 MiB/file plus five backups;
   archive experiments before rollover. Default database behavior is unchanged.
5. `v3/test_hardening.py`: eight added tests covering full/partial data after ACK,
   burst timing, absence/invalid diagnostics, bounded overlong diagnostics,
   malformed-traffic timeout, rolling median expiry, restart baseline/history,
   and visible SQLite write failures.
6. `v3/test_presence.py`: updated existing debug expectations to include malformed
   frames; retained assertions that debug does not change database contents.
7. `v3/soak_check.py`: reproducible accelerated synthetic stability check with a
   fixed seed, near-boundary jitter, isolated spikes, temporary SQLite, bounded
   history checks and timing/memory metrics.
8. `v3/README.md`, `beacon/README.md`, this checkpoint: current behavior, results,
   integration boundary, commands, and unresolved physical work.

V2 deliberately remains the historical comparison implementation. It still has
the older bulk-read ACK behavior; use v3 for the hardened operational path.

## Verification results

Final automated tests: v2 19/19, v3 36/36. Intentional disconnect warnings were
visible during tests. Newly added regression tests first demonstrated the ACK and
batch-timing bugs; all pass after the changes. CLI help also checked successfully.
Read-only checks of the existing root, v1 and v3 databases all returned SQLite
integrity `ok`, with 8, 8 and 10 historical rows respectively. The original JSONL
presence record was also inspected. No historical data was modified.

Accelerated synthetic run, 24 simulated hours at 0.15-second intervals:

| Metric | Observed |
| --- | ---: |
| Observations | 576,000 |
| Stored events | 1 initial baseline |
| Zone | `far`, no oscillation on this trace |
| Maximum retained window | 11 samples |
| Database | 24,576 bytes |
| SQLite integrity | `ok` |
| Traced Python memory, current / peak | 9,624 / 18,648 bytes |
| CPU / elapsed execution | 8.05 / 8.28 seconds |

This is a specific synthetic trace, not a general leak proof or hardware soak.
It excludes actual serial I/O, RF, firmware memory, USB recovery, logging overhead
and real-time process CPU usage. Database growth during frequent real transitions
remains unmeasured.

Firmware object checks passed for both unchanged sketches. Arduino CLI 1.5.1 full
compile attempts failed with `following symlink ... Arduino15\packages: Access
is denied`, denied temporary extraction directories for builtin tools, and then
`Platform 'esp32:esp32' not found`. The headers/compiler are installed and were
usable by the fallback; the last error is not proof the package is absent. Current
session policy disallows elevation. No attempt was made to bypass that restriction.

## Exact run configuration and commands

Run from this workspace root in PowerShell. Use the installed Python, since the
Store launcher was previously problematic. Dependencies were already available.

```powershell
$python = 'C:\Users\endle\AppData\Local\Programs\Python\Python313\python.exe'
$cli = 'C:\Users\endle\AppData\Local\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe'
$libraries = 'C:\Users\endle\OneDrive\Documents\Arduino\libraries'
$fqbn = 'esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,PSRAM=disabled,FlashSize=4M'
& $python -m pip install -r v3/requirements.txt
& $python -m unittest discover -s v2 -v
& $python -m unittest discover -s v3 -v
& $python v3/soak_check.py --hours 24
& $python beacon/check_firmware.py
& $python -m serial.tools.list_ports -v
```

Identify only the BLE boards, by individual unplug/replug if necessary; avoid the
camera board. Check board specifications before using this generic S3 4 MB layout.
Full builds need a normal shell with access to installed Arduino tools:

```powershell
$beaconPort = Read-Host 'Identified beacon COM port'
$scannerPort = Read-Host 'Identified scanner COM port'
& $cli compile --fqbn $fqbn --libraries $libraries --build-path .build/beacon beacon/esp32_test_beacon
if ($LASTEXITCODE -ne 0) { throw 'Beacon build failed' }
& $cli compile --fqbn $fqbn --libraries $libraries --build-path .build/scanner v2/esp32_presence
if ($LASTEXITCODE -ne 0) { throw 'Scanner build failed' }
& $cli upload --fqbn $fqbn --port $beaconPort --input-dir .build/beacon beacon/esp32_test_beacon
if ($LASTEXITCODE -ne 0) { throw 'Beacon upload failed' }
& $cli upload --fqbn $fqbn --port $scannerPort --input-dir .build/scanner v2/esp32_presence
if ($LASTEXITCODE -ne 0) { throw 'Scanner upload failed' }
& $python -m serial.tools.list_ports -v
$scannerPort = Read-Host 'Verified running scanner COM port after reset'
& $python v3/presence_service.py --config v3/config.beacon.json --port $scannerPort --debug --log-file beacon-run.log
```

`v3/config.beacon.json` is complete as shipped: target `beacon`, person label
`Minisa-Test-01`, sensor `esp32-s3-ble`, database `minisa_beacon_senses.db`, baud
115200 and v3 defaults above. The supplied COM10 is historical; `--port` overrides
it without altering the config. Relative DB paths resolve beside the config.
Use one serial reader at a time. Stop with Ctrl+C.

Phone mode uses `v3/config.json`: target `phone`, person label `SBeppi`, database
`minisa_senses.db`, same scanner and v3 settings. Intentionally advertise exact
case-sensitive name `Sbeppi's S22+`, then run:

```powershell
& $python v3/presence_service.py --config v3/config.json --port $scannerPort --debug --log-file phone-run.log
```

## Physical experiments and resume boundary

No flash, live acknowledgement, raw radio observation, movement/obstruction/range
test, physical disconnect/reconnect, or long-run hardware experiment was performed.
None should be marked passed. Follow `beacon/README.md` once boards are accessible:

1. Record actual ports, board identities, flash outcomes, firmware/tool versions.
2. Confirm `Scanner target confirmed: beacon` AND repeated fresh RSSI records.
   Heartbeats or a beacon status line alone do not prove the radio path.
3. Keep scanner fixed; stationary baseline, approach with pauses, retreat with
   pauses, stationary boundary test, obstruction, out-of-range, return. Repeat
   routes at least three times. Human assistance is required to move the beacon.
4. Record experiment action timestamps alongside captured debug logs. Log raw
   RSSI, median, boundary support/spans and transitions. Power-off absence is
   useful but must not be mislabeled as a physical range test.
5. Unplug/replug scanner; confirm offline/online and fresh target handshake.
   Re-enumerated COM numbers currently require updating `--port`/config and restart.
6. Run a real extended soak, archive logs before rotation, measure CPU/process
   memory/DB growth, packet gaps, false gone, oscillation and recovery. Tune only
   reproducible RF problems supported by repeated captures.

RSSI is not distance. Zone names are fuzzy signal regions, not universal room or
metre boundaries. Host receipt timestamps cannot establish device measurement age
or distinguish physical movement from orientation/multipath/obstruction. Separate
buffered reads may still contain delayed data. A persisted `person` label and
`present` event do not establish that a human is beside Minisa. Sensor offline or
service shutdown makes the live state unknown; consumers need health/freshness
checks and future sensory fusion. No public-model or calibration change was made.
