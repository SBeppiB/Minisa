# Minisa BLE presence v3

Current audit, verified results, blockers, and resume instructions:
[2026-10-08 checkpoint](../PROXIMITY_CHECKPOINT.md).

USB hardware belongs to the Windows PC, not this canonical remote server.
Transfer `dist/minisa-windows-validation.zip` and follow its README. It contains
production v3, verified images, offline dependencies, safe flashing and the
[physical-validation workflow](PHYSICAL_VALIDATION.md). Run discovery/serial
commands only on that PC. No server USB probing or forwarding is required.
Runtime tests: 19 v2 + 51 v3; package construction adds eight tests.

V3 targets human-perceived zone changes within roughly 1–3 seconds of sustained
RSSI movement at multiple fresh observations per second. RSSI remains qualitative
signal proximity: `very_close`, `nearby`, `far`, `very_far`. It is not a distance
measurement, and orientation, obstructions, and multipath can resemble movement.

V2 and its database are preserved. V3 creates its own `v3/minisa_senses.db` and
uses the shared `../v2/esp32_presence/esp32_presence.ino` firmware, now supporting
configuration-based phone/beacon selection.
No scan, reporting, heartbeat, or absence timing changes are needed.

## Why the algorithm changed

V2 combines an adaptive EMA with 3 dB hysteresis. Its `very_far` → `far`
transition requires filtered RSSI to reach −72 dBm, consistent with the reported
−71.98 dBm transition. A signal stabilizing below that level can wait indefinitely;
increasing alpha cannot fix that barrier. Conversely, V2's fast alpha can accept
a single large anomalous packet. The reported 72-second delay cannot be fully
reconstructed without the raw trace, but neither mechanism suits the desired
behavior.

V3 replaces both with recent evidence:

1. Keep fresh observations from the last 1.5 seconds. Their median is the filtered
   RSSI stored on transitions; there is no accumulating EMA lag.
2. A crossed boundary needs at least three supporting observations, at least 75%
   agreement across the window, and a supporting time span of at least 1 second.
   Approach and retreat use the same rules. This measures sustained directional
   evidence without requiring each noisy packet to be monotonic.
3. The boundary margin is only 0.5 dB. Temporal agreement supplies stability:
   `very_far` can become `far` at sustained −74.5 or stronger, and `far` can become
   `very_far` at sustained −75.5 or weaker. Alternating ±1 dB jitter cannot build
   enough agreement. Persistent movement inside the narrow margin intentionally
   retains the current zone.
4. Strong evidence at least 6 dB beyond a boundary uses a shorter 0.6-second
   confirmation. It still requires three strong samples and 75% agreement;
   one large spike cannot activate it. Old samples may delay either path until
   the recent window agrees.
5. Evaluate each boundary separately and cross all confirmed boundaries in one
   event. There is no per-zone cooldown during a multi-zone move.

Heartbeat frames do not vote. Old evidence expires, so packets on opposite sides
of a long gap cannot confirm movement together. Repeated timestamps do not vote
twice; serial bursts also have to meet elapsed-time confirmation. The wire
protocol has no device timestamps, so timing is based on host monotonic receipt
time, not a reconstruction of advertisement time.

Initial discovery establishes an immediate baseline from the first observation;
spike resistance applies to subsequent transitions. Confirmed absence clears the
window. Existing GONE confirmation, serial reconnection, sensor health events,
framing, and database schema are retained. Absence still takes roughly 5–6 seconds
with the existing firmware; the 1–3 second target concerns RSSI zone movement.

## ESP32-S3 test beacon

See [the beacon setup and live-testing guide](../beacon/README.md) for both
boards, flash commands, RSSI verification, and repeatable walking tests. Run
`python v3/presence_service.py --config v3/config.beacon.json --debug` from the root. The beacon config uses a separate
database and identity. `config.json` retains phone selection. Updated scanner
firmware is required for both; no v3 movement algorithm or timing changes were
needed.

## Run and compare

From the workspace root:

```powershell
py -3 -m pip install -r v3/requirements.txt
py -3 v3/presence_service.py
```

Edit `v3/config.json` for the serial port and tuning. `--config path/to/config.json`
selects another configuration; relative database paths resolve beside that file.
Only one process can read the serial port: stop v3 before running
`py -3 v2/presence_service.py` for comparison. Neither version migrates the other's
database. The stored RSSI is V2's EMA or V3's rolling median, respectively.

If the Windows Store Python alias fails, use the installed interpreter:

```powershell
& 'C:\Users\endle\AppData\Local\Programs\Python\Python313\python.exe' v3/presence_service.py
```

Walk toward and away from the receiver, pause just beyond each boundary, make a
rapid multi-zone move, and stand still near boundaries. Compare transition times
and unwanted reversals in each version's console/database. Use the same phone
orientation and route. Automated tests verify synthetic traces; actual RF behavior
and human motion latency still require this hardware comparison.

## Debug hardware latency

Enable console-only observation tracing with:

```powershell
py -3 v3/presence_service.py --debug
```

Add `--debug` to the installed-interpreter command above if the Python launcher
fails. Debug is off by default. Every valid `PRESENCE:PRESENT` frame produces a
JSON debug record, including duplicates rejected for non-increasing receipt
timestamps. Every incoming valid `SCAN:OK` frame is also logged. Invalid frames
are logged as `INVALID` with bounded hexadecimal bytes when debug is enabled.
Overlong frames are discarded after 80 bytes and counted as `OVERLONG`; their
unbounded payload is not retained. `PRESENCE:GONE` records expose absence
confirmation timing, including reports that do not produce a transition.
These records are never sent to SQLite; existing transition and
sensor-health storage is unchanged.

Records include UTC `timestamp`, host `monotonic_seconds`, `raw_rssi`,
`rolling_median`, `previous_zone`, `current_zone` (after evaluation), window size,
and whether the observation was accepted. The initial observation establishes a
baseline and has no transition candidates. Receipt times are host times; the
protocol cannot reveal when the ESP32 actually measured the RSSI.

V3 has no persistent candidate-zone state or candidate stopwatch. The `candidates`
array describes each boundary in the direction reachable from the previous zone,
including boundaries for multi-zone moves. Each entry gives the `candidate_zone`,
supporting/required samples, whether this packet counted as evidence, median gate,
and confirmation result. Required samples are the larger of `min_observations`
and the rounded-up agreement fraction of the entire window. A confirmed farther
boundary can only be crossed if the intervening boundaries also confirm.

`pending_seconds` is the age of the oldest supporting sample still in the window
(zero with no support). It can shrink when evidence expires; it is not a timer
since physical movement began. `support_span_seconds` is the actual elapsed span
between the first and last supporting samples, compared to `confirm_seconds`.
Strong support has its own count, span, per-packet evidence flag, and shorter
confirmation requirement, using the same required sample count and median gate.
An accepted opposing packet still affects the median and agreement denominator.

Compare raw RSSI with the median, then check the median gate, support counts, and
both confirmation spans to locate the delay. Logging adds console I/O overhead;
use this mode for diagnosis and omit `--debug` for normal operation.

Use `--port COMx` to override the configured port after detecting the actual
scanner. Save diagnostics with `--log-file beacon-run.log --debug`. The parent
directory must exist. Logs rotate at 10 MiB with five backups (about 60 MiB total);
archive an experiment before its oldest records rotate away. Logs include startup
configuration, target acknowledgement, transitions, and debug records. They are
timestamp-prefixed log lines with JSON diagnostic payloads, not a pure JSONL file.
SQLite still stores only transitions and has no automatic retention policy.

V3 reads the target acknowledgement one byte at a time so it cannot consume the
following observation or partial frame. During normal operation, all frames in a
single serial read share one receipt timestamp: slow logging/database writes
cannot manufacture sustained evidence within that read. Extra observations in
that batch are logged but rejected as duplicate timestamps. Device measurement
timestamps remain unavailable; separate buffered reads can still contain old data.

`python v3/soak_check.py --hours 24` runs an accelerated deterministic synthetic
stability check with bounded history and temporary SQLite storage. It is not a
24-hour hardware test.

## Integration boundary

The legacy `person` column is a configured target label, not authenticated identity
or evidence that its owner occupies a room. `present` means a matching BLE target
was observed; `gone` means confirmed missing advertisements. Sensor offline makes
target occupancy unknown, not gone. Consumers must use sensor health and event
timestamps and must not treat the latest historical presence row as current after
a service restart or stop. SQLite is an event history, not a live status endpoint.

Zone transitions express coarse stronger/weaker signal evidence. There is no
separate continuous approach/retreat field or calibrated confidence probability.
Debug sample counts, agreement and confirmation spans expose the actual evidence.
Thresholds are fuzzy signal-strength regions, not metres or portable room labels.
Movement within a region may produce no transition. Minisa Core must combine this
channel with other sensory evidence before making statements about people.

## Tuning parameters

| Parameter | Default | Meaning |
|---|---:|---|
| `thresholds` | −45, −60, −75 | Descending qualitative zone boundaries |
| `hysteresis_db` | 0.5 | Small margin on either side of each boundary |
| `window_seconds` | 1.5 | Maximum age of voting samples |
| `min_observations` | 3 | Minimum supporting samples, including the strong path |
| `agreement` | 0.75 | Fraction of all window samples supporting a crossing |
| `confirm_seconds` | 1.0 | Minimum span of supporting observations |
| `strong_change_db` | 6 | Penetration beyond a boundary for strong evidence |
| `strong_confirm_seconds` | 0.6 | Minimum span of strong supporting observations |
| `gone_confirm_seconds` | 0.8 | Existing repeated-GONE confirmation |
| `link_timeout_seconds` | 5 | Valid-frame silence before reconnect |
| `reconnect_seconds` | 2 | Serial retry interval |

Keep `0 < strong_confirm_seconds <= confirm_seconds < window_seconds`,
`0.5 < agreement <= 1`, and `min_observations >= 3`. A longer window or higher
agreement reduces chatter but delays movement. Confirmation is elapsed time,
not just packet count; sample count still limits behavior at sparse rates.
Custom tuning or observations slower than the tested rates may exceed the target
or provide insufficient evidence. Do not restore a large margin merely to suppress
jitter: that restores the sustained-signal barrier observed in V2. A sustained RF
change and physical movement cannot be distinguished perfectly with RSSI alone.

## Validation

```powershell
py -3 -m unittest discover -s v3 -v
py -3 -m unittest discover -s v2 -v
```

Tests cover fast approach/retreat steps and ramps, every boundary in both
directions, alternating boundary jitter, isolated large outliers, multi-zone
jumps, stale evidence, serial bursts, configurable confirmation, departure,
transport, and SQLite. Latency assertions bound both elapsed time and observation
count at 2, 4, and approximately 6.7 Hz.

Measured synthetic step results after a settled baseline (time from the last
baseline observation, with the first changed sample one interval later):

| Fresh observation rate | Changed observations to transition | Elapsed time |
|---|---:|---:|
| 2 Hz | 3 | 1.50 s |
| 4 Hz | 6 | 1.50 s |
| 6.67 Hz | 8 | 1.20 s |

These results apply to tested −85↔−65, −90↔−30, and −77↔−73 dBm steps.
The last pair demonstrates crossing without waiting for the old ±3 dB limits.
They are simulated receipt-time results, not hardware measurements or a guarantee
for ambiguous/noisy traces.
