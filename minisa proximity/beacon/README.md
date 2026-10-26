# Minisa ESP32-S3 test beacon

**Deployment topology:** this repository is canonical on the remote server;
the Windows PC owns the USB boards. Transfer `dist/minisa-windows-validation.zip`
to Windows and use its README, offline setup, `flash_firmware.py`, and
`validate_hardware.py`. Do not run COM discovery on the server. The instructions
below describing a local checkout are retained as implementation/build reference;
the standalone Windows kit is the current hardware workflow.

Latest independent audit: [2026-10-08 checkpoint](../PROXIMITY_CHECKPOINT.md).
All 70 current tests pass (19 v2, 51 v3). Both sketches now fully compile/link
through `python beacon/build_firmware.py`; verified merged images are under
`.build/full/scanner` and `.build/full/beacon`. Arduino CLI discovery still has an
access failure, but full linking no longer depends on it. Upload one positively
identified board with `python beacon/flash_firmware.py --role scanner --port COMx`
or `--role beacon --port COMy`. The helper verifies image/source hashes and asks
for the selected role/port before upload. No connected serial devices were found;
historical Espressif entries are reported disconnected by Windows PnP Utility.
The beacon radio path remains physically unvalidated. Use the
[single-terminal physical-validation workflow](../v3/PHYSICAL_VALIDATION.md).

## Two boards, two jobs

| Board | Sketch | Job |
| --- | --- | --- |
| ESP32-S3 #1 (existing scanner) | `v2/esp32_presence/esp32_presence.ino` | Receive BLE and send RSSI to the computer over USB |
| ESP32-S3 #2 (test target) | `beacon/esp32_test_beacon/esp32_test_beacon.ino` | Advertise continuously; power by USB or power bank |

Both v2 and v3 use the same scanner sketch. Flash the updated scanner once as
well as the new beacon. Neither Python proximity algorithm nor its thresholds,
confirmation timing, database schema, or serial observation format has changed.
No camera files, firmware, or hardware are involved.

The beacon identifies itself by service UUID
`c7a10001-5a1e-4b1e-9a11-4d494e495341`; its human-readable name is `Minisa-01`.
The scanner matches the UUID, not the name or a changing BLE address. Only run
one beacon with this UUID in the experiment: two copies would mix their RSSI.
This is an experimental identity, not authentication.

Legacy advertising uses a fixed 100 ms interval (160 BLE units), fixed 0 dBm
transmit power, and all three primary advertising channels. BLE still adds its
normal random advertising delay; exact arrival spacing is not promised.
The 29-byte primary payload holds the full UUID and full name. It has no scan
response, connection, pairing, GATT server, Wi-Fi, or application sleep cycle.
The beacon starts without waiting for a serial terminal and retries stopped
advertising once per second. `BEACON:OK:Minisa-01:100ms:0dBm` on its USB terminal
means advertising is running; `BEACON:ERROR:*` means it is not ready.

API details were checked against installed NimBLE-Arduino 2.5.1 and its
[advertising reference](https://h2zero.github.io/NimBLE-Arduino/class_nim_b_l_e_advertising.html).

## Flash both boards

Use Espressif **ESP32 Arduino 3.3.12** and **NimBLE-Arduino 2.5.1**. These versions
are installed on this machine. The sketches use no board-specific GPIOs or PSRAM.
The example below selects **ESP32S3 Dev Module**, native USB **Hardware CDC and
JTAG**, **USB CDC On Boot enabled**, **PSRAM disabled**, and a conservative **4 MB
flash layout**. Use it for an ESP32-S3 with at least 4 MB flash and its native USB
connector. A larger physical flash is fine with this layout. The repository does
not identify #2's exact module; check its marking/board specification before
upload. If your board requires a vendor-specific flash mode or uses a USB-UART
bridge, use that board's settings and connector instead. Do not select ESP32,
ESP32-C3, or infer memory requirements from unrelated firmware.

From the repository root in PowerShell:

```powershell
$cli = 'C:\Users\endle\AppData\Local\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe'
$python = 'C:\Users\endle\AppData\Local\Programs\Python\Python313\python.exe'
$libraries = 'C:\Users\endle\OneDrive\Documents\Arduino\libraries'
$fqbn = 'esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,PSRAM=disabled,FlashSize=4M'
& $python -m serial.tools.list_ports -v
& $cli board list
```

Identify each board by unplugging/replugging it individually. Close Python and
Serial Monitor before uploading. COM10 is the existing configured scanner port,
not evidence that an arbitrary connected board is the scanner. If USB bootloader
mode is needed, hold BOOT, tap RESET, release BOOT, and list ports again. The port
may change after upload/reset. Only select the two BLE boards.

```powershell
# Enter the ports you identified; these prompts avoid guessing which board to overwrite.
$beaconPort = Read-Host 'COM port for ESP32-S3 #2 (beacon)'
$scannerPort = Read-Host 'COM port for ESP32-S3 #1 (scanner)'

& $cli compile --fqbn $fqbn --libraries $libraries --build-path .build/beacon beacon/esp32_test_beacon
if ($LASTEXITCODE -ne 0) { throw 'Beacon compilation failed' }
& $cli upload --fqbn $fqbn --port $beaconPort --input-dir .build/beacon beacon/esp32_test_beacon
if ($LASTEXITCODE -ne 0) { throw 'Beacon upload failed' }

& $cli compile --fqbn $fqbn --libraries $libraries --build-path .build/scanner v2/esp32_presence
if ($LASTEXITCODE -ne 0) { throw 'Scanner compilation failed' }
& $cli upload --fqbn $fqbn --port $scannerPort --input-dir .build/scanner v2/esp32_presence
if ($LASTEXITCODE -ne 0) { throw 'Scanner upload failed' }
```

Alternatively, open each sketch in Arduino IDE, select the settings above and
the correct port, then Verify and Upload. No source edits are needed to choose
the target. The beacon can subsequently run from a power bank; check that the
bank does not automatically turn off at low current.

## Start a live test

1. Power #2 and keep #1 connected to the computer. Recheck #1's running COM port.
2. Set `port` in `v3/config.beacon.json` to #1's port. The supplied config already
   sets `target` to `beacon`, `person` to `Minisa-Test-01`, and `database` to
   `minisa_beacon_senses.db`. That database is created beside the config, separate
   from phone data. Use the analogous v2 config when comparing algorithms.
3. Close all other readers of #1's serial port and run:

```powershell
& $python -m pip install -r v3/requirements.txt
& $python v3/presence_service.py --config v3/config.beacon.json --debug
```

Expect `Scanner target confirmed: beacon`, a sensor-online event, and repeated
JSON records containing `frame: PRESENCE:PRESENT`, `raw_rssi`, `rolling_median`,
and zone/evidence fields. `SCAN:OK` only says the scanner is alive; it does not
prove the beacon is being received. Near the receiver, expect several fresh
RSSI observations per second, at most about 6.7 Hz because reporting is limited
to one latest sample per 150 ms. RF losses can lower that rate. Debug prints
every valid observation; SQLite stores transitions rather than raw samples.

For v2 comparison, stop v3 with Ctrl+C, set the port in `v2/config.beacon.json`,
then run:

```powershell
& $python v2/presence_service.py --config v2/config.beacon.json
```

V2 prints transitions; use v3 `--debug` to inspect individual RSSI readings.
Only one Python process or Serial Monitor may own the scanner port.

For a raw serial check with Python stopped, open #1 at 115200 baud and send
`TARGET:BEACON` with a newline. Expect `TARGET:OK:BEACON`, then `SCAN:OK` and
`PRESENCE:PRESENT:-NN`. The scanner intentionally waits for target selection
after boot. A target-acknowledgement timeout in Python means wrong port,
unresponsive board, or old scanner firmware; it must not be treated as absence.

## Repeatable movement experiments

Keep the scanner fixed, with the same height, orientation, USB cable, and room.
Mark a route and stopping points. Carry #2 with the same antenna orientation and
the same position relative to your body each time; avoid covering its antenna.
Use steady USB/power-bank power and a single beacon throughout the comparison.

1. Stand still for 10 seconds to establish the RSSI spread and initial zone.
2. Walk toward #1, pause 3–5 seconds at each marked point, then walk away along
   the same route. Repeat at least three times with the same pace/orientation.
3. In v3 debug output, compare raw RSSI, rolling median, and confirmed zone
   changes. Sustained approach/retreat should cross the same configured
   boundaries; the existing 1–3 second synthetic movement targets are retained.
4. Power #2 off for more than six seconds: expect `gone` while `SCAN:OK` continues.
   Power it back on: expect a new presence baseline. Brief interruptions should
   not immediately cause departure. Unplugging #1 should report sensor offline,
   followed by reconnection/target selection when its configured port returns.
5. Compare v2 and v3 with identical routes and configs' thresholds. Beacon and
   phone antennas/power differ, so calibrate thresholds from observed RSSI;
   physical distances cannot be copied directly between them. A stable RF
   signal cannot prove lack of movement, and an RF change cannot prove movement.

## Switch back to the phone

Stop Python. Enable the phone's intentional BLE advertiser with the exact name
`Sbeppi's S22+`. Set #1's running port in `v3/config.json`, which has
`"target": "phone"`, and run:

```powershell
& $python v3/presence_service.py --config v3/config.json --debug
# Or, after stopping v3:
& $python v2/presence_service.py --config v2/config.json
```

No firmware reflash is needed. Phone mode retains active scanning for names in
scan responses. Beacon mode uses passive scanning and matches only the UUID.
The scan window/interval remain 60/100 ms, reporting 150 ms, heartbeat 1 second,
and absence timeout 4 seconds, followed by Python's existing GONE confirmation.
To choose a mode in a custom config, use `"target": "phone"` or `"target":
"beacon"`; omitted target defaults to phone. Restart Python after config edits.

Every serial connection sends `TARGET:PHONE` or `TARGET:BEACON`, retries once
per second, and waits up to `link_timeout_seconds` for the exact acknowledgement.
Pre-acknowledgement observations are discarded. Selection clears pending scanner
evidence when the mode changes; repeated identical commands are idempotent.
Python always starts a fresh proximity model after reconnect. Target selection
is volatile, so reset requires another handshake. Updated services require the
updated scanner even in phone mode; older firmware cannot acknowledge selection.

## Validation and current limits

```powershell
& $python -m unittest discover -s v2 -v
& $python -m unittest discover -s v3 -v
& $python beacon/check_firmware.py
```

`check_firmware.py` is an environment-specific fallback: it uses the installed
ESP32-S3 compiler, Arduino 3.3.12 SDK/headers and NimBLE 2.5.1 to compile both
sketch translation units into `.build/*.o`. It does not link, upload, or prove
radio operation. `--packages` and `--library` override its local search paths.

On 2026-10-07 all 47 Python tests passed (19 v2, 28 v3), and both sketches
passed that object compilation. Full Arduino CLI
builds were attempted but blocked by sandbox access to Arduino15 package/tool
initialization. No board has been flashed by this change, and over-air RSSI,
advertising cadence, USB recovery and real walking latency remain to be checked
using the steps above. Python regressions and target-selection tests cover both
algorithms, fragmented acknowledgements, stale/wrong-target data, retries,
timeouts, short writes, disconnection/reconfiguration, config validation, and
sustained beacon approach/retreat/absence traces.
