# Next actions — Minisa learning-cycle deployment handoff

**Status: NO-GO.** Nothing in this document authorises a production change. It
lists the gates that must be closed, in order, by an administrator who has an
independent recovery destination and an authenticated root session.

**Scope of the change once approved** (all of it, or none of it):
copy two core files into `/srv/minisa/app/core/`, apply migration 5 to
`/srv/minisa/data/minisa.db`, install one systemd drop-in, restart two
services.

**Source of truth** (read before the window):
`RECOVERY_RUNBOOK.md` (operator procedure, §1–§11),
`RECOVERY_READINESS_REPORT_20261021.md`,
`DEPLOYMENT_REHEARSAL_REPORT_20261021.md`,
`ROLLBACK_RUNBOOK_FIX_REPORT_20261021.md`,
`LEARNING_RELEASE_CANDIDATE_20261021.md`,
`ops/preflight_learning_cycle_release.py`, `ops/capture_minisa_recovery.py`,
`ops/verify_recovery_database.py`.

---

## 1. Baseline — last reported values, not newly verified

The values below are **the last reported readings** (runbook §11, readiness
report §6, fix report §6.3, as of 2026-10-21). This handoff re-verified
nothing; re-read every one of them at the window.

| Item | Last reported value |
| --- | --- |
| Live database | `/srv/minisa/data/minisa.db`, SHA-256 `8d7c11be4c55e2956473c68ec8f1c4bff48b12d0ed6e56b7aec69e450c4508a0`, size 692224, mtime `2026-10-19T06:22:31Z` (reported as 07:22:31 local) |
| Live schema version | **4** |
| Live `global_revision` | **195** |
| Live applied migrations | **1–4** (`stage1_persistence_foundation`, `cognitive_state_store`, `persistent_workspace`, `phase5_hardening`) |
| Live database integrity | `ok`, 17 tables, no `memory_records`, `learned_procedures` 0 rows |
| Candidate manifest | `/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021.manifest.json`, SHA-256 `410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405` |
| Staged release | `/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021`, 20 tracked files, `production_deployed: false` |
| Drop-in candidate | `deploy/minisa-learning-cycle-timeout.conf.candidate`, SHA-256 `9e647d83c90946cf894c6f66a3db48323078019f046f38fdfe5bf93d35170879` |
| Services | `minisa.service` PID 1324, `minisa-core.service` PID 1007, both active, never restarted |
| Production state | untouched: live DB, `/srv/minisa/app/core/{db.py,migrations.py}` (`479bb191…`, `425aaa16…`), systemd units, drop-in directory unchanged; `20-dream-recovery.conf` absent |
| Candidate test suite | 210 tests, `OK (skipped=6)` under `/usr/bin/python3` and `/srv/minisa/.venv/bin/python3` |

---

## 2. Gate order

| # | Gate | Runbook / script | Owner |
| --- | --- | --- | --- |
| 1 | Independent recovery destination | §2, `ops/capture_minisa_recovery.py` | administrator + storage owner |
| 2 | Administrator session | §2, §3 | administrator |
| 3 | Read-only preflight + real service configuration | §3, `ops/preflight_learning_cycle_release.py` | operator |
| 4 | Full recovery point captured and independently verified | §4, `ops/capture_minisa_recovery.py`, `ops/verify_recovery_database.py` | operator + administrator |
| 5 | Explicit review and approval of the changes | §9, §10 | approver |
| 6 | Execute the maintenance procedure (incl. code-first rollback) | §5, §6, §7 | operator |
| 7 | Verify health, schema, migration history, learning-cycle behaviour | §6, §7.3 | operator |

Do not start gate *n+1* until gate *n* is closed and its evidence is recorded.

---

## 3. Gate 1 — Independent recovery destination

**Where:** `RECOVERY_RUNBOOK.md` §2; `ops/capture_minisa_recovery.py`
(`check_destination`).

**Commands** (administrator, read-only):

```bash
findmnt -o TARGET,SOURCE,FSTYPE
lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINT,MODEL
lsblk -srno NAME "$(findmnt -n -o SOURCE -T /)"      # ancestry of /
lsblk -srno NAME "$(findmnt -n -o SOURCE -T /srv)"    # ancestry of /srv

sudo python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/capture_minisa_recovery.py \
  /mnt/minisa-recovery
```

The last command is the utility's read-only preflight mode (no
`--execute-maintenance`). For a remote filesystem add
`--confirm-independent-target`, which the utility demands for network targets
after the administrator has verified the storage and its failure domain.

**Evidence of success:** the utility prints `"preflight": "passed"` and names
the destination in `"destination"` with
`"independent_from_live_block_devices": true`; it reports
`"required_free_bytes"` and `"available_bytes"`; the destination's block-device
ancestry does not intersect `nvme0n1`.

**Stop immediately if:** the utility refuses the target
("backup destination shares a physical block device with production",
"refusing root or Minisa data filesystem as backup destination",
"unverified destination filesystem/source", "network target requires
--confirm-independent-target"), or `"preflight": "blocked"`, or the ancestry
intersects production's. The destination must be an existing directory.

### 3.1 What does **not** satisfy this gate

Same-disk material is not an independent recovery point, whatever it is called:

- another directory, a separate logical volume, an LVM snapshot, or any other
  path on `nvme0n1` (including `/home`, `/srv`, `/var`, `/var/tmp`);
- the 2026-10-18 database-only capture
  `/home/sbeppi/minisa-recovery-backups/dream-recovery-db-20261018T123156Z`
  (same device, database component only);
- the five `/srv/minisa/backups/stage1-*` registry backups (same device,
  schema 1–2);
- **every disposable rehearsal artifact** — the rehearsal root
  `/tmp/opencode/rehearsal-20261021T184629Z`, its `db/*.db` copies, its
  `capture-rehearsal/` package, and the dry-run copies made on 2026-10-21. They
  prove the *mechanism* works; they are lost with the device and are not a
  recovery point;
- a passed restore rehearsal. A successful rehearsal proves the restore
  procedure, never the independence of the destination.

---

## 4. Gate 2 — Administrator session

**Where:** `RECOVERY_RUNBOOK.md` §2 ("Who is needed") and §3.

**Commands:**

```bash
sudo systemctl show minisa.service minisa-core.service \
  -p ActiveState -p SubState -p WorkingDirectory -p TimeoutStopUSec --no-pager
sudo systemctl show --property=UnitFileState --value minisa.service
sudo systemctl show --property=UnitFileState --value minisa-core.service
```

**Evidence of success:** `sudo` completes without changing any authentication
setting; the effective unit properties are readable; `minisa.service`
`WorkingDirectory` is still the base release
`/home/sbeppi/minisa-releases/response-context-20261017T010152Z`; the drop-in
path is free:

```bash
test ! -e /etc/systemd/system/minisa.service.d/20-dream-recovery.conf
```

**Stop immediately if:** the administrator's authentication cannot be completed
(never disable authentication, never change sudoers, never pass a password
through chat), or any service is `failed` or transitioning at the start of the
window — the capture utility refuses to run in that case. Whether the unit
file itself was read is proven by the preflight's `service_configuration` check
in gate 3, not by `systemctl show`.

---

## 5. Gate 3 — Read-only preflight and the real service configuration

**Where:** `RECOVERY_RUNBOOK.md` §3; `ops/preflight_learning_cycle_release.py`.

**Command:**

```bash
sudo python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/preflight_learning_cycle_release.py
```

Do **not** pass `--skip-tests`: the preflight then fails the `tests` check on
purpose.

**Evidence of success:** the JSON report shows `"preflight": "blocked"` with
`"read_only": true` and exactly one blocking check, `recovery_point` (no
capture exists yet at this point). Every other check must pass:

| Check | Must report |
| --- | --- |
| `manifest` | 20 changed files, supported schema 5, `production_deployed: false` |
| `release_content` | 20 tracked files match the manifest |
| `migration_lineage` | production migrations 1–4 match the candidate chain; the candidate adds migration 5 only |
| `live_database` | schema 4, revision 195, integrity `ok`, opened read-only |
| `core_service_files` | live core code differs from the candidate as expected before the copy step |
| `service_configuration` | both units' effective properties readable, `WorkingDirectory` still the base release, drop-in path free — **this check is the administrator's proof that the unit file was read** |
| `independent_destination` | a mounted filesystem whose block-device ancestry does not intersect production's |
| `recovery_point` | **expected to be the one blocking check at this gate** — no capture exists yet; it must pass after gate 4 |
| `rollback_prerequisites` | base release intact, live core hashes recorded, rollback model recorded |
| `tests` | candidate suite green (`OK (skipped=6)`) |

**Stop immediately if:** any check other than `recovery_point` is blocking, or
a check merely *could not be verified* — a missing file, an unreadable unit
file, an unreadable configuration is a failure, not a pass. The preflight exits
`0` only when nothing is blocking, `2` when blocked, `3` when it could not run.

---

## 6. Gate 4 — Capture and independently verify the full recovery point

**Where:** `RECOVERY_RUNBOOK.md` §4; `ops/capture_minisa_recovery.py`;
`ops/verify_recovery_database.py`.

**Commands** (inside the approved window, root; the utility stops both
services, captures, rehearses the restore and restores the original service
states):

```bash
sudo python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/capture_minisa_recovery.py \
  /mnt/minisa-recovery --execute-maintenance
```

Then verify the capture **independently** (not only by trusting the utility's
own report):

```bash
cd /mnt/minisa-recovery/minisa-recovery-<capture-id>
sha256sum -c SHA256SUMS
cat CAPTURE_VERIFIED

python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/verify_recovery_database.py \
  --db /mnt/minisa-recovery/minisa-recovery-<capture-id>/sqlite/minisa.db \
  --expect-schema-version 4 --expect-migrations 1,2,3,4 --expect-integrity ok
```

Then re-run the full preflight with the capture:

```bash
sudo python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/preflight_learning_cycle_release.py \
  --recovery-capture /mnt/minisa-recovery/minisa-recovery-<capture-id>
```

**Evidence of success:** the capture utility prints `"capture": "verified"` with
`backup_directory`, `manifest_sha256`, `archive_sha256`, `database_sha256` and
`restore_rehearsal`; `sha256sum -c SHA256SUMS` reports every entry `OK`;
`CAPTURE_VERIFIED` matches the manifest digest; the helper prints
`VERDICT: PASSED` with `integrity_check ok`, `installation.schema_version = 4`
and applied migrations `1, 2, 3, 4`; the capture directory contains
`manifest.json`, `SHA256SUMS`, `file-state.tar`, `file-manifest.json`,
`sqlite/minisa.db`, `CAPTURE_VERIFIED` and `restore-rehearsal/`; and the
preflight now reports `"preflight": "passed"` with an empty `blocking_checks`
list and exits `0`.

**Stop immediately if:** the utility reports `capture failed safely: …`; the
capture directory contains `CAPTURE_INCOMPLETE`; any `SHA256SUMS` entry fails;
the captured database is not schema 4 or its integrity check is not `ok`; the
restore rehearsal did not pass; or the preflight is still blocked.

---

## 7. Gate 5 — Review and explicit approval

**Where:** `RECOVERY_RUNBOOK.md` §9 (stop conditions) and §10 (sign-off
checklist).

The approver must see, in writing, before anything is executed:

- the gate 3 and gate 4 preflight reports (`"preflight": "passed"`, exit `0`);
- the capture identity (`backup_directory`, `manifest_sha256`,
  `database_sha256`) and the independently verified checksums;
- the exact production changes: copy `core/db.py` and `core/migrations.py`
  into `/srv/minisa/app/core/`, apply migration 5, install
  `/etc/systemd/system/minisa.service.d/20-dream-recovery.conf`, restart
  `minisa-core.service` and `minisa.service`;
- the rollback plan and its known cost: **migration 5 is one-way**; an L2
  rollback restores the captured pre-migration database and loses everything
  written after the migration (runbook §7.3);
- the §10 sign-off checklist, item by item, including the new item "L2 rollback
  path (§7.2 code rollback, then §7.3 data rollback) rehearsed on a disposable
  copy, in that order".

**Evidence of success:** every §10 checklist row is signed, and the approval
names the window, the operator, the capture id and the approver.

**Stop immediately if:** any §9 stop condition is true, any §10 row is
unsigned, or the approval does not name the capture that will be used for
rollback.

---

## 8. Gate 6 — Execute the maintenance procedure

**Where:** `RECOVERY_RUNBOOK.md` §5 (migration), §6 (activation), §7 (rollback).

Run in this order, with both services stopped, and check every verification line
before continuing.

**§5 — migration 5** (core code copy, then the migration, then verification):

```bash
# 0. keep the predecessor core code recoverable (precondition for L2 rollback)
sudo cp -a /srv/minisa/app/core/db.py        /srv/minisa/app/core/db.py.pre-v5-<stamp>
sudo cp -a /srv/minisa/app/core/migrations.py /srv/minisa/app/core/migrations.py.pre-v5-<stamp>

# 1. install the candidate core files
sudo install -o sbeppi -g sbeppi -m 0644 \
  /home/sbeppi/minisa-working-assistant-20261018/candidate/core/db.py \
  /srv/minisa/app/core/db.py
sudo install -o sbeppi -g sbeppi -m 0644 \
  /home/sbeppi/minisa-working-assistant-20261018/candidate/core/migrations.py \
  /srv/minisa/app/core/migrations.py

# 2. apply the migration explicitly
cd /srv/minisa/app && /srv/minisa/app/.venv/bin/python3 -c "
from core.db import Database
db = Database('/srv/minisa/data/minisa.db')
print('schema_version =', db.initialize())
"
```

**§5 verification** (all must hold): `schema_version = 5` printed, then

```bash
python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/verify_recovery_database.py \
  --db /srv/minisa/data/minisa.db \
  --core-tree /srv/minisa/app \
  --expect-schema-version 5 \
  --expect-migrations 1,2,3,4,5 \
  --expect-integrity ok \
  --expect-global-revision 195 \
  --expect-table memory_records \
  --expect-table learned_procedures \
  --expect-row-count memory_records=0 \
  --expect-row-count learned_procedures=<pre-migration row count>
```

Expected: `VERDICT: PASSED`, including `migration_compatible` (database 5 ==
installed core 5) and `migration_lineage` (applied names and checksums match
the installed core tree). Use the `learned_procedures` count read in the
pre-migration verification as `<pre-migration row count>` (0 at the last
reported baseline).

**§6 — runtime activation:**

```bash
test ! -e /etc/systemd/system/minisa.service.d/20-dream-recovery.conf

sudo install -D -o root -g root -m 0644 \
  /home/sbeppi/minisa-working-assistant-20261018/candidate/deploy/minisa-learning-cycle-timeout.conf.candidate \
  /etc/systemd/system/minisa.service.d/20-dream-recovery.conf
sudo systemctl daemon-reload
sudo systemctl start minisa-core.service
sudo systemctl start minisa.service
```

**Rollback if anything below fails — code first, data second (§7.2 → §7.3).**
Never restore the database before the core code has been reverted: with the
candidate core files installed, the core service re-applies migration 5 to a
restored schema-4 database at startup.

```bash
# §7.2 code rollback
sudo systemctl stop minisa.service minisa-core.service
sudo rm -f /etc/systemd/system/minisa.service.d/20-dream-recovery.conf
sudo systemctl daemon-reload
sudo cp -a /srv/minisa/app/core/db.py.pre-v5-<stamp>        /srv/minisa/app/core/db.py
sudo cp -a /srv/minisa/app/core/migrations.py.pre-v5-<stamp> /srv/minisa/app/core/migrations.py
python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/verify_recovery_database.py \
  --core-tree /srv/minisa/app --core-only          # must report: supports schema version 4

# §7.3 data rollback
cd /mnt/minisa-recovery/minisa-recovery-<capture-id>
sha256sum -c SHA256SUMS
cat CAPTURE_VERIFIED
sudo rm -f /srv/minisa/data/minisa.db-wal /srv/minisa/data/minisa.db-shm
sudo install -o sbeppi -g sbeppi -m 0644 sqlite/minisa.db /srv/minisa/data/minisa.db
python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/verify_recovery_database.py \
  --db /srv/minisa/data/minisa.db \
  --core-tree /srv/minisa/app \
  --capture-dir /mnt/minisa-recovery/minisa-recovery-<capture-id> \
  --expect-schema-version 4 --expect-migrations 1,2,3,4 --expect-integrity ok \
  --expect-table learned_procedures --expect-no-table memory_records
sudo systemctl start minisa-core.service
curl -fsS http://127.0.0.1:8765/ready
sudo systemctl start minisa.service
curl -fsS http://100.108.164.57:8765/health
```

**Evidence of success:** `schema_version = 5`; helper `VERDICT: PASSED`; core
`/ready` shows `migration_compatible: true` and `supported_schema_version: 5`;
runtime `/health` reports `status: ok`; `systemctl show minisa.service` reports
`WorkingDirectory=/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021`
and `TimeoutStopUSec=50s`.

**Stop immediately if:** `initialize()` raises; a migration name or checksum is
not in the candidate chain; `integrity_check` is not `ok`; `global_revision`
changed; the `learned_procedures` row count changed; the core service does not
become ready with `migration_compatible: true` at schema 5; or the runtime does
not become active on the candidate release.

---

## 9. Gate 7 — Verify health, schema, migration history, learning-cycle behaviour

**Where:** `RECOVERY_RUNBOOK.md` §6 (post-deployment verification) and §7.3
(step 6); `runtime/server/http.py` (runtime endpoints); `core/app.py`
(core-service endpoints).

**Commands:**

```bash
systemctl show minisa.service minisa-core.service \
  -p ActiveState -p SubState -p WorkingDirectory -p TimeoutStopUSec --no-pager

curl -fsS http://127.0.0.1:8765/health        # core service
curl -fsS http://127.0.0.1:8765/ready         # core service
curl -fsS http://100.108.164.57:8765/health   # runtime
curl -fsS http://100.108.164.57:8765/v1/dream-status   # runtime
curl -fsS http://100.108.164.57:8765/v1/cognitive-health  # runtime

python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/verify_recovery_database.py \
  --db /srv/minisa/data/minisa.db \
  --core-tree /srv/minisa/app \
  --expect-schema-version 5 --expect-migrations 1,2,3,4,5 --expect-integrity ok
```

**Evidence of success:** both services `active`; the runtime `WorkingDirectory`
is the candidate release; core `/health` and `/ready` answer with
`"status": "ok"` / `"status": "ready"`, `"schema_version": 5`,
`"supported_schema_version": 5`, `"migration_compatible": true`; the runtime
`/health` answers `"status": "ok"`; `/v1/dream-status` and
`/v1/cognitive-health` answer 200 with JSON; the helper prints
`VERDICT: PASSED` with applied migrations `1, 2, 3, 4, 5` (migration 5 named
`sourced_memory_records`) and `migration_compatible`.

**Learning-cycle behaviour:** observe it, do not provoke it. Runbook §6 is
explicit: **do not send test chat prompts, do not invoke a dream, and do not
reset the recovery circuit** — let ordinary scheduling run. The learning-cycle
evidence is one naturally scheduled dream result that appears in the durable
store with its timestamp, source and result revisions, evidence count and
matching audit rows (`LEARNING_RELEASE_CANDIDATE_20261021.md` §4, step 5), and
a `memory_records` row appearing through the normal cycle rather than through
the migration. Optional post-window measurement, using the existing harness and
no code change (it writes only its own report file, by default
`/tmp/minisa-retrieval-report.json`):
`python3 ops/measure_retrieval_quality.py`.

**Stop immediately if:** any health endpoint is not OK; `/ready` does not report
`migration_compatible: true`; the schema version is not 5 or the applied
migrations are not 1–5; `global_revision` moves for any reason other than the
normal learning cycle; or a protected file's hash differs from the capture's
`file-manifest.json`.

---

## 10. Command provenance

Every command above is taken from the repository's own documentation or scripts.

| Command / artefact | Source |
| --- | --- |
| `findmnt`, `lsblk` ancestry checks | `RECOVERY_RUNBOOK.md` §2 |
| `ops/capture_minisa_recovery.py <dir>` (`--execute-maintenance`, `--confirm-independent-target`) | runbook §3–§4; `ops/capture_minisa_recovery.py` CLI |
| `ops/preflight_learning_cycle_release.py` (`--recovery-capture`, `--skip-tests`) | runbook §3; `ops/preflight_learning_cycle_release.py` CLI |
| `ops/verify_recovery_database.py` (all flags used above) | runbook §4, §5, §6, §7.3, §8; `ops/verify_recovery_database.py` CLI |
| `sha256sum -c SHA256SUMS`, `cat CAPTURE_VERIFIED` | runbook §4, §8 |
| `systemctl show … -p ActiveState -p SubState -p WorkingDirectory -p TimeoutStopUSec` | runbook §6, §7.3 |
| `systemctl show --property=UnitFileState --value <unit>` | `ops/capture_minisa_recovery.py` |
| `systemctl start/stop minisa.service minisa-core.service`, `daemon-reload` | runbook §5–§8 |
| `sudo install … core/db.py, core/migrations.py` | runbook §5 step 1 |
| `sudo cp -a … .pre-v5-<stamp>` | runbook §5 step 0, §7.2 |
| `cd /srv/minisa/app && /srv/minisa/.venv/bin/python3 -c "… db.initialize() …"` | runbook §5 step 2 |
| `sudo install -D … 20-dream-recovery.conf` | runbook §6 |
| `curl … http://100.108.164.57:8765/health`, `http://127.0.0.1:8765/ready` | runbook §6, §7.3, §8 |
| `curl … /v1/dream-status`, `/v1/cognitive-health` | `runtime/server/http.py:35-42` |
| `ops/measure_retrieval_quality.py` | `LEARNING_RELEASE_CANDIDATE_20261021.md` §3 |
| `test ! -e /etc/systemd/system/minisa.service.d/20-dream-recovery.conf` | runbook §6 |

The `sqlite3` command-line program is **not installed** on this host and is not
used anywhere in this handoff; `ops/verify_recovery_database.py` replaces it.

---

## 11. Open items for the administrator

1. **Which independence mechanism** — a physically separate device mounted at
   `/mnt/minisa-recovery`, or a remote filesystem verified by the administrator
   and passed with `--confirm-independent-target`.
2. **`<capture-id>`** is generated by the capture utility; use the
   `backup_directory` value it prints, never a guessed path.
3. **`<stamp>`** in `core/*.pre-v5-<stamp>` is operator-chosen and must be
   recorded in the incident log; without those two files an L2 rollback cannot
   be completed.
4. **`<pre-migration row count>`** for `learned_procedures` must be read before
   the migration (0 at the last reported baseline) and substituted literally.
5. **Root-only steps have never been executed on the real system**: the
   capture with `--execute-maintenance`, the core-file copy, the drop-in
   install, `daemon-reload`, and the two service restarts. Their semantics were
   verified in disposable copies only.
6. **The hybrid release (L1b)** — candidate `core/` + base `runtime/` — has no
   manifest and no preflight of its own; it must not be activated until it does.
7. **The 54 untracked `.pyc` files** in the staged release are optional to
   remove before deployment (readiness report §7); removing them changes no
   tracked file and no manifest digest, but it does modify the staged release,
   which must stay byte-identical until it is approved for deployment.
8. **The dream host** `100.123.208.117:11434` did not answer from this host,
   so the learning-cycle check depends on ordinary scheduling rather than a
   manual trigger.

---

## 12. Sign-off

| Gate | Closed by | Date | Evidence attached |
| --- | --- | --- | --- |
| 1 Independent destination | | | |
| 2 Administrator session | | | |
| 3 Preflight + service configuration | | | |
| 4 Verified recovery point | | | |
| 5 Approval | | | |
| 6 Maintenance executed (or rolled back) | | | |
| 7 Post-deployment verification | | | |

Deployment remains **NO-GO** until every row is closed.
