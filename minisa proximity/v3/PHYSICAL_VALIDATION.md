# Physical validation acceptance criteria

Declare these criteria before collecting the first physical trace. They are
operational test targets, not guarantees about RF physics. Do not change RSSI
thresholds or tune v3 to make a single route pass. Repeat approach/retreat and
stationary tests at least three times; retain failures and inconclusive trials.
No test is passed by a synthetic replay. No test of a person is implied.

## One-terminal workflow

The canonical repository is on the remote server. USB hardware belongs to the
Windows PC. Transfer/extract `dist/minisa-windows-validation.zip` on that PC and
follow its README/setup first. Do not run discovery or serial validation on the
server. From the extracted kit on Windows:

```powershell
$python = '.\.venv\Scripts\python.exe'
& $python validate_hardware.py --discover
# After positively identifying the two BLE boards, flash each selected role:
& $python flash_firmware.py --role beacon --port COMy
& $python flash_firmware.py --role scanner --port COMx
# Re-enumerate after reset; port numbers may change.
& $python validate_hardware.py --discover
& $python validate_hardware.py --scanner COMx --beacon COMy
```

COMx/COMy are placeholders, never assumed ports. Flashing requires typing the
selected role and port, verifies the image/source hashes, enforces ESP32-S3 chip
type, and saves the upload output. Confirm >=4 MiB flash/native USB/QIO support.
It never flashes a discovered port automatically. The kit contains prebuilt images.
Rebuild only on the canonical server with `python beacon/build_firmware.py` and
regenerate the kit with `python beacon/package_windows.py --output NEW_DIRECTORY`.

`--beacon` is optional (for USB-powered beacon status). When using a power bank,
omit it; the summary marks beacon boot status unverified unless separately tested.
The scanner is verified by the real service's target handshake. Fresh observations
after BEACON acknowledgement are scanner-reported UUID matches under this firmware;
the serial protocol does not transmit the UUID or authenticate the sender. Keep
only one beacon with the UUID powered during the experiment.

The tool defaults to guided steps. Enter marks the current step complete and
starts the next. Follow the stated hold duration before pressing Enter; premature
completion is marked INCOMPLETE. `note <text>` records position/action/context;
`quit` or Ctrl+C stops cleanly. A failed or interrupted run still gets a summary.
Each new run has its own UTC-named folder under `validation-runs/`, containing
`config.json`, `capture.jsonl`, `events.db`, and `summary.json`. Capture includes
all production diagnostic records, transitions, human annotations, resource
samples, optional beacon serial bytes and errors. Raw/median RSSI, state, support
and span, observation age and five-second report rate appear every two seconds.
The first five seconds of the rate estimate are intentionally a warm-up period.

For a longer automated stationary recording:

```powershell
& $python validate_hardware.py --scanner COMx --beacon COMy --monitor --seconds 3600
```

`--soak-seconds 3600` changes the guided soak minimum. Capture defaults to a
512 MiB per-run limit; on reaching it, the run stops visibly rather than silently
discarding old evidence. Raise `--max-log-mib` for a longer run with enough disk.
Database storage remains transition-only. No v3 filtering logic is duplicated.
Normal service use is unchanged. The only production API addition is an optional
stop event so the recorder can close serial/SQLite cleanly, including retry waits.

If scanner USB comes back under a different COM number, stop, re-discover and
restart with the new port. Automatic selection of an unrelated replacement port
is deliberately prohibited. Record this as an incomplete automatic reconnect test.

## Checklist and explicit result rules

| Test | PASS | FAIL / INCONCLUSIVE |
| --- | --- | --- |
| Scanner boot | Three power/reset cycles; each obtains target ACK within 10 seconds and healthy frames afterward | Missing ACK, boot loop, or healthy frames absent; untested cycles are NOT VERIFIED |
| Beacon boot | Three power cycles; each emits the expected BEACON:OK within 10 seconds and is received by scanner | Error/boot loop/no reception; no beacon serial monitor means status remains NOT VERIFIED |
| Target selection / UUID path | Exact BEACON ACK, then at least 10 fresh reports spanning >=2 seconds, only intended UUID beacon powered | Wrong/no ACK or no repeated reports; this is firmware-reported matching, not independent packet sniffing |
| PRESENT | A fresh report records a present baseline in SQLite, with unknown previous zone on a new session | No presence event or stale history reused |
| No silent stall | Dashboard ages reports; >5-second age visibly STALE; missing valid frames produces offline/retry | Old data displayed as fresh, no offline/retry on serial silence |
| GONE | With target genuinely out of range/off, heartbeat continues and gone is stored within 10 seconds of final report | No GONE within limit, or sensor-offline mistaken for gone; power-off is a separate absence test, not a range test |
| Approach / retreat | Three repeat routes in each direction; intended directional evidence visible; when production support/span gates confirm, matching zone transition is recorded | Contradictory repeated behavior requires investigation; no sufficient boundary crossing is INCONCLUSIVE, never forced into PASS |
| Stationary noise | Each >=30-second hold: >=10 reports, no false GONE/offline, no >5-second report gap, <=2 zone transitions per minute (minimum allowance 2) | Exceeds these prespecified operational limits; review full trace and repeat |
| Obstruct / restore | Raw and median response/evidence captured; no service/firmware crash; >=10 fresh observations in 15 seconds after restoration | No recovery/crash; GONE during long total RF blockage is not inherently a defect |
| Leave / return | Real range exit produces GONE; return produces fresh PRESENT baseline and >=10 reports within 15 seconds | Missing departure/return events or stale baseline; serial remains healthy |
| USB reconnect | Disconnect >=6 seconds yields offline without invented GONE; same port returns, target re-ACKs, online and >=3 fresh reports within 10 seconds after enumeration | No retry/recovery; re-enumerated port requires explicit selection and is recorded as a limitation |
| Persistence / logging | SQLite integrity `ok`; transition rows match logged transition sequence; UTC timestamps and annotations retained; capture errors stop visibly | Missing/contradictory rows, corruption, dropped capture or concealed errors |
| Stationary soak | >=10 minutes (prefer >=1 hour): stationary criteria pass; no crashes; sampled working set settles; last half stays within 10 MiB of first-half median; mean process CPU <10% of one core; DB growth accounted for by event rows | Growth beyond bounds or persistent growth requires investigation; unavailable metrics are NOT VERIFIED; finite soak cannot prove absence of leaks |

Step summaries automate counts and basic checks only. They do not certify boot
cycles, exact action timing, RF identity, all recovery timing limits, persistence
sequence equivalence, or resource-growth acceptance. Review the JSONL, SQLite,
and human action notes against this checklist. Movement without a zone crossing
can still be useful evidence; mark the crossing test inconclusive and retain it.
The monitor's zero exit code means observations were captured without a fatal
tool/storage error; it is not a blanket physical-validation PASS.
