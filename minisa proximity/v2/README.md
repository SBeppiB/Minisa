# Minisa BLE presence v2

The original `presence_node`, database, and JSONL file are preserved in the parent folder. No original Arduino sketch was available. This version writes its own `v2/minisa_senses.db` automatically; it does not migrate or modify the original database.

## ESP32-S3 test beacon

See [the beacon setup and live-testing guide](../beacon/README.md) for firmware
for both boards, exact flash commands, target selection, and walking tests.
`config.json` selects the phone; `config.beacon.json` selects the test beacon and
a separate database. Both require the updated scanner firmware. Python confirms
the target over USB before accepting presence data.

## Architecture and protocol

Samsung S22+ advertising → ESP32-S3 NimBLE callbacks → USB serial → Python proximity state machine → SQLite transitions. Advertise the exact, case-sensitive name `Sbeppi's S22+`. Names identify this prototype's target but are neither unique nor authenticated; RSSI describes qualitative signal proximity, never exact physical distance.

115200 baud, ASCII, newline terminated:

```
TARGET:BEACON        # host command (or TARGET:PHONE)
TARGET:OK:BEACON     # scanner acknowledgement
PRESENCE:PRESENT:-42
SCAN:OK
PRESENCE:GONE
```

The comments above are explanatory, not wire data. Python selects the configured
target automatically on each connection. A manually opened serial terminal must
send a target command before a freshly booted scanner emits observations.

PRESENT carries a fresh observation, at most once every 150 ms (latest sample wins). SCAN:OK is a one-second scanner heartbeat, not fresh phone evidence. GONE repeats once a second only after four seconds without a matching advertisement while scanning. Python requires two or more GONE frames spanning at least 0.8 seconds; any PRESENT cancels confirmation. Default departure latency is roughly 5–6 seconds after the last observation. Initial absence uses the same confirmation. Tune this for the phone's advertising interval and real packet loss.

After target selection, firmware scans asynchronously and indefinitely, with 60 ms windows every 100 ms, active scan for phone names in scan responses or passive scan for the beacon UUID, duplicate callbacks, and no stored result list. The loop yields every 10 ms. Discovery still depends on the phone's advertisement cadence, radio interference, and scan responses. A stopped scanner is restarted; it emits no healthy heartbeat or departure evidence while stopped.

Python handles fragmented lines, rejects malformed/out-of-range frames, and bounds its input buffer. Five seconds without a valid frame causes reconnection, including when a serial port remains open but silent. USB errors also reconnect after two seconds. Sensor offline/online events are separate from phone departure. Each connection starts with unknown proximity and fresh EMA, so recovery records a new observed baseline with a null previous zone. Historical database state is deliberately not assumed current after restart. Ctrl+C closes serial and SQLite, including during reconnect waits; unexpected errors also close SQLite.

## Run

1. In Arduino IDE install the Espressif ESP32 board package and **NimBLE-Arduino 2.x** (API targeted at 2.5.1). Select your ESP32-S3 board and USB port. For native USB, enable USB CDC on boot as appropriate to the board. Open `esp32_presence/esp32_presence.ino`, compile, and upload. Close Serial Monitor before Python opens the same port.
2. Enable intentional BLE advertising on the phone with the exact target name. Keep it advertising in the background as allowed by Android.
3. From the original prototype folder in PowerShell:

```powershell
py -3 -m pip install -r v2/requirements.txt
py -3 v2/presence_service.py
```

Requires Python 3.10+. Edit `config.json` for COM10 or other settings. `--config path/to/config.json` selects another config; relative database paths resolve beside that config, independent of the working directory. Run only one reader per serial port.

On this machine the default `py -3` points to an inaccessible Store installation. The working interpreter used for tests was `C:\Users\endle\AppData\Local\Programs\Python\Python313\python.exe`. If necessary, replace `py -3` in the commands above with `& 'C:\Users\endle\AppData\Local\Programs\Python\Python313\python.exe'`.

## Proximity and tuning

EMA: `filtered += alpha * (raw - filtered)`. Default alpha 0.4 retains stability without a five-sample lag. When raw differs from filtered by at least 12 dB, alpha becomes 0.85 for that observation, allowing immediate multi-zone moves on strong approach or retreat. A single large RF outlier can still cause a move; lowering fast_alpha or raising strong_change_db trades responsiveness for stability.

| Parameter | Default | Effect |
|---|---:|---|
| alpha | 0.4 | Normal EMA response; higher is faster/noisier |
| fast_alpha | 0.85 | Response to large changes; must be ≥ alpha |
| strong_change_db | 12 | Raw-to-EMA gap activating fast response |
| thresholds | -45, -60, -75 | Initial boundaries for very_close, nearby, far, very_far |
| hysteresis_db | 3 | Margin on each side of a boundary |
| gone_confirm_seconds | 0.8 | Minimum span of repeated firmware absence reports |
| link_timeout_seconds | 5 | Valid-frame silence before reconnect |
| reconnect_seconds | 2 | Retry delay |

For the -60 dBm boundary, nearby becomes far below -63; far becomes nearby at -57 or stronger. Hysteresis applies to the EMA, with multi-zone jumps allowed. EMA alpha is per emitted observation, so its time response depends on advertisement cadence. Calibrate thresholds in the actual room, with the phone in typical pockets/orientations. Gone means sustained lack of advertisements, not proof a person physically left.

JSON `target` selects phone or beacon without source edits. Firmware constants define the phone name, beacon UUID, scan duty, 150 ms reporting, one-second heartbeat, and four-second absence timeout. Increase scan window for faster discovery at higher radio usage, or reduce it to save radio time. Keep heartbeat shorter than Python's link timeout. The current duty is nominal scan scheduling, not a measured power figure.

## Storage and tests

`schema.sql` is applied automatically. Parameterized inserts store UTC timestamps, sensor/person identity, event type, state, zone, previous zone, and filtered RSSI for present transitions. Gone and sensor health have null RSSI. Only initial observations, presence/zone changes, and sensor health transitions are stored; raw samples and heartbeats are not. Null previous zone means unknown baseline. SQLite commits each event, with timestamp, type, and identity/time indexes. `Proximity`, `EventStore`, and serial handling are separate so Minisa Core can reuse the algorithm without serial or database dependencies.

```powershell
py -3 -m unittest discover -s v2 -v
```

Local tests cover EMA, threshold jitter, fast approach, departure cancellation/confirmation, protocol framing, SQLite, silence detection, reconnect retries, and shutdown. Hardware validation is still required: walk toward/away from the receiver, stop advertising for over six seconds, briefly interrupt advertising, unplug/replug USB, and verify transition rows. Both updated sketches passed ESP32-S3 object compilation; full linking/upload and actual BLE latency/power remain unverified. See the beacon guide for build limitations.

NimBLE references: [scan API](https://h2zero.github.io/NimBLE-Arduino/class_nim_b_l_e_scan.html), [2.x migration guide](https://h2zero.github.io/NimBLE-Arduino/md_1_8x__to2_8x__migration__guide.html).
