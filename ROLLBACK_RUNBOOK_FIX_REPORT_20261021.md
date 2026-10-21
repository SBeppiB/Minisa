# Rollback runbook fix report — F1 and F2 closed

**Date:** 2026-10-21
**Scope:** the candidate workspace only —
`/home/sbeppi/minisa-working-assistant-20261018/candidate`
**Trigger:** `DEPLOYMENT_REHEARSAL_REPORT_20261021.md` findings **F1** (high) and
**F2** (medium).
**Deployment status after this work:** **still NO-GO.** See §8.

---

## 1. What was wrong

| ID | Finding (from the rehearsal report) |
| --- | --- |
| F1 | The L2 rollback in `RECOVERY_RUNBOOK.md` §7.3 restored *only* the database. With the candidate core files still installed, `minisa-core.service` re-applies migration 5 on startup (`core/app.py` startup event → `database.initialize()` → applies every pending migration). The candidate runtime release also refuses a restored schema-4 database (`RuntimeError: Workspace requires reviewed Core migration before startup`, `runtime/server/application.py:112-114`). The runbook had no ordered schema-4 recovery procedure. |
| F2 | The `sqlite3` command-line program is **not installed** on this host (`command -v sqlite3` → empty). Four verification commands in the runbook (§4 item 4, §5 verification block, §6 post-deployment verification, §8 step 6) would have failed with "command not found" during the window. |

---

## 2. Files changed (candidate workspace only)

| File | Change |
| --- | --- |
| `ops/verify_recovery_database.py` | **New.** Stdlib-only, read-only verification helper that replaces every `sqlite3` CLI invocation. |
| `RECOVERY_RUNBOOK.md` | §7 rewritten (§7.0 … §7.5); the four `sqlite3` invocations replaced; §8 aligned with the same ordering; §9, §10 and §11 extended. 473 → 698 lines. |
| `tests/test_rollback_runbook.py` | **New.** 22 tests: runbook document invariants, helper behaviour under both interpreters, and the re-migration hazard. |
| `DEPLOYMENT_REHEARSAL_REPORT_20261021.md` | F1 and F2 rows annotated as closed (no findings changed). |

**Not touched:** the live production release, `/srv/minisa`, `/etc`, the staged
release `/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021`,
its manifest, and the live database. Verification in §6.

---

## 3. F1 — the ordered schema-4 recovery procedure

`RECOVERY_RUNBOOK.md` §7 now states the procedure explicitly and in order.

### 3.1 The rule (§7.0)

> **The order is fixed: code first, data second.**

Restoring the database while the candidate core files are still installed is
undone by the core service itself. The predecessor core files
(`core/db.py.pre-v5-<stamp>`, `core/migrations.py.pre-v5-<stamp>`, taken in §5
step 0) are code, not data: they make the core service accept schema 4 again,
but they cannot restore a schema-4 database.

### 3.2 The two independent rollbacks (§7.0)

| | Code rollback | Data rollback (L2) |
| --- | --- | --- |
| What is reverted | the runtime release pointer and the core-service code | the database file |
| Procedure | §7.2 | §7.3 |
| Requires | root, both services stopped | root, both services stopped, a verified capture |

### 3.3 §7.2 — step 1 of L2 (code)

1. `systemctl stop minisa.service minisa-core.service`.
2. Remove `/etc/systemd/system/minisa.service.d/20-dream-recovery.conf` and
   `daemon-reload`. The drop-in **is** the release pointer: the unit's own
   `WorkingDirectory` is the base release and the drop-in overrides it.
3. Restore the predecessor core files into `/srv/minisa/app/core/`.
4. Prove the installed core tree supports schema 4:
   `ops/verify_recovery_database.py --core-tree /srv/minisa/app --core-only`
   → `supports schema version 4`. If it still reports 5, **stop** — the database
   must not be restored.

L1b (hybrid release, candidate `core/` + base `runtime/`) keeps the migrated
database; it now names the preflight command that a hybrid must pass with its
own manifest before activation.

### 3.4 §7.3 — step 2 of L2 (data), seven ordered steps

| Step | Action |
| --- | --- |
| 1 | Confirm both services are stopped and the drop-in is gone (`systemctl show … -p ActiveState -p WorkingDirectory`). |
| 2 | Confirm the predecessor core code is in place (`--core-tree /srv/minisa/app --core-only` → schema 4). |
| 3 | Restore the captured image: `sha256sum -c SHA256SUMS`, `cat CAPTURE_VERIFIED`, remove the `-wal`/`-shm` sidecars, `install -o sbeppi -g sbeppi -m 0644 sqlite/minisa.db /srv/minisa/data/minisa.db`. |
| 4 | Verify **before starting anything** (`ops/verify_recovery_database.py --db … --core-tree … --capture-dir … --expect-schema-version 4 --expect-migrations 1,2,3,4 --expect-integrity ok --expect-table learned_procedures --expect-no-table memory_records`). |
| 5 | Confirm that starting is safe: installed core supports schema 4 **and** the active runtime release accepts schema 4 (the base release does, because it carries its own schema-4 `core/`). |
| 6 | Start `minisa-core.service` → `curl -fsS http://127.0.0.1:8765/ready` must show `migration_compatible: true` and `supported_schema_version: 4`; then start `minisa.service` → `curl -fsS http://100.108.164.57:8765/health`; then `systemctl show …`. |
| 7 | Record the evidence (capture id, verification output, service states, restored `global_revision`, timestamps). |

§7.5 lists six rollback invariants (pointer never left on the candidate release
at schema 4; installed core supports exactly the restored schema version; the
restored database is byte-identical to the captured image; no service starts
before verification; code rollback precedes data rollback; predecessor copies
are not a substitute for the capture). They are asserted by the new tests.

§7.4 gained a sixth limitation (restoring the database while the candidate core
files are installed is not a rollback), §9 a thirteenth stop condition, §10 a
thirteenth sign-off row, and §8 an ordering note plus a `daemon-reload` after
the unit files are restored from the archive.

---

## 4. F2 — the `sqlite3` CLI dependency

### 4.1 The helper

`ops/verify_recovery_database.py` — Python standard library only (`sqlite3`,
`hashlib`, `json`, `argparse`, `subprocess`, `tempfile`), no new packages,
read-only (`file:…?mode=ro` + `PRAGMA query_only=ON`).

```
usage: verify_recovery_database.py --db PATH [--core-tree PATH] [--core-only]
         [--expect-schema-version N] [--expect-migrations 1,2,3,4]
         [--expect-integrity ok] [--expect-global-revision N]
         [--expect-table NAME] [--expect-no-table NAME]
         [--expect-row-count TABLE=N] [--capture-dir PATH] [--json]
```

| Exit code | Meaning |
| --- | --- |
| 0 | every requested check passed (`VERDICT: PASSED`) |
| 1 | at least one check failed; the failing checks are named (an incomplete or mismatched capture is a *failed check*, not an error) |
| 2 | the script could not run (usage error, unreadable database, broken code tree) |

Checks: `database_readable`, `integrity_check`, `schema_version`,
`migration_history`, `migration_lineage` (names and checksums of applied
migrations against the inspected core tree), `migration_compatible` (database
schema vs the schema version the installed core tree supports — the check that
decides whether a service may start), `global_revision`, `table_present:*`,
`table_absent:*`, `row_count:*`, `capture_complete`, `capture_manifest`,
`capture_status`, `capture_marker` (`CAPTURE_VERIFIED` vs the manifest digest),
`capture_checksums` (every `SHA256SUMS` entry), `capture_database_match` (the
database is byte-identical to the captured image), `read_only_report`.

`--core-tree` is interrogated in a **separate** interpreter with an empty
`PYTHONPATH` and a neutral working directory, so it can never import the
candidate workspace's `core` package by accident.

### 4.2 Runbook replacements

| Location | Was | Now |
| --- | --- | --- |
| §4 item 4 | `sqlite3 …/sqlite/minisa.db "PRAGMA integrity_check; SELECT MAX(version) …"` | `verify_recovery_database.py --db …/sqlite/minisa.db --expect-schema-version 4 --expect-migrations 1,2,3,4 --expect-integrity ok` |
| §5 verification block | `sqlite3 … "PRAGMA integrity_check; SELECT version,name …"` (6 queries) | `verify_recovery_database.py --db … --core-tree /srv/minisa/app --expect-schema-version 5 --expect-migrations 1,2,3,4,5 --expect-integrity ok --expect-global-revision 195 --expect-table memory_records --expect-table learned_procedures --expect-row-count memory_records=0 --expect-row-count learned_procedures=<pre-migration count>` |
| §6 post-deployment verification | `sqlite3 /srv/minisa/data/minisa.db "SELECT schema_version FROM installation;"` | `verify_recovery_database.py --db … --core-tree /srv/minisa/app --expect-schema-version 5 --expect-migrations 1,2,3,4,5` |
| §8 step 6 | `sqlite3 … "PRAGMA integrity_check; SELECT MAX(version) …"` | `verify_recovery_database.py --db … --core-tree /srv/minisa/app --capture-dir … --expect-schema-version 4 --expect-migrations 1,2,3,4 --expect-integrity ok --expect-table learned_procedures --expect-no-table memory_records` |

`grep -n "sqlite3 " RECOVERY_RUNBOOK.md` → **no matches**. The runbook now
states at §4 item 4 that the `sqlite3` program is not installed and that no step
may use it.

---

## 5. Tests

`tests/test_rollback_runbook.py` — 22 tests in three groups.

*Document invariants* (9): no `sqlite3` executable invocation anywhere in the
runbook; the helper is referenced at least four times and exists; §7 contains
§7.2/§7.3/§7.5; the drop-in removal, the predecessor-core restore and the
database install appear in that order; the verification precedes the first
`systemctl start`; the re-migration hazard is documented; §7.3 verifies with
`--capture-dir` and `--expect-no-table memory_records`; §7.5 states the six
invariants; §8 verifies before starting; the pre-existing sections survive.

*Helper behaviour* (11, run under both interpreters): passes on a healthy
schema-4 database; names the failing check on a schema mismatch, on a present
table that must be absent, and on a wrong row count; detects an incompatible
core tree; exit 2 for a missing database; **never writes** (the database bytes
are compared before and after); JSON report; `--core-only`; detects a capture
that does not match the database; detects an incomplete capture.

*Re-migration hazard* (2): a genuine schema-4 database is built by applying
migrations 1–4 from the candidate chain. The **predecessor** core tree
(`/home/sbeppi/minisa-releases/response-context-20261017T010152Z`, schema 4)
leaves it at schema 4 with no `memory_records`; the **candidate** core tree
(schema 5) re-applies migration 5 and creates `memory_records`.

```
$ PYTHONWARNINGS=error::ResourceWarning /usr/bin/python3 -m unittest discover -s tests
Ran 210 tests in 4.627s
OK (skipped=6)

$ PYTHONWARNINGS=error::ResourceWarning /srv/minisa/.venv/bin/python3 -m unittest discover -s tests
Ran 210 tests in 4.680s
OK (skipped=6)
```

188 tests before this work, 210 now (+22); both interpreters green, no
warnings.

---

## 6. Evidence

### 6.1 The documented rollback, executed on disposable data

The §7.2/§7.3 commands were run against the rehearsal capture package
(`/tmp/opencode/rehearsal-20261021T184629Z/capture-rehearsal/…`, the verified
capture of the pre-migration database). Nothing was installed over production.

```
$ ops/verify_recovery_database.py --core-tree /srv/minisa/app --core-only
PASS  core_tree: /srv/minisa/app supports schema version 4
VERDICT: PASSED

$ install -o sbeppi -g sbeppi -m 0644 …/sqlite/minisa.db /tmp/…/data/minisa.db
-rw-r--r--. 1 sbeppi sbeppi 733184 /tmp/…/data/minisa.db

$ ops/verify_recovery_database.py --db /tmp/…/data/minisa.db --core-tree /srv/minisa/app \
    --capture-dir …/minisa-recovery-rehearsal --expect-schema-version 4 \
    --expect-migrations 1,2,3,4 --expect-integrity ok \
    --expect-table learned_procedures --expect-no-table memory_records
PASS  integrity_check: integrity_check = ['ok'] (expected ['ok'])
PASS  schema_version: installation.schema_version = 4 (expected 4)
PASS  migration_history: applied migrations = [1, 2, 3, 4] (expected [1, 2, 3, 4])
PASS  migration_lineage: applied migration names and checksums match the inspected core tree
PASS  migration_compatible: database schema 4 vs core tree supported version 4 (services may start only when these are equal)
PASS  table_present:learned_procedures: present
PASS  table_absent:memory_records: absent
PASS  read_only_report: global_revision = 195, tables = 17
PASS  capture_status: capture_status = 'verified' (expected 'verified')
PASS  capture_marker: CAPTURE_VERIFIED matches the manifest digest
PASS  capture_checksums: every SHA256SUMS entry matches
PASS  capture_database_match: the database is byte-identical to the captured image
VERDICT: PASSED
```

### 6.2 The hazard, reproduced through the real core-service startup

The restored capture image was served by the **real** core application
(`core.app:app`, FastAPI startup event, `MINISA_DB_PATH` pointed at the
disposable copy, port 8799/8798 — the live service on 8765 was not touched).

Predecessor core tree (`/srv/minisa/app`, schema 4):

```
GET /ready  → {"status":"ready","service":"minisa-core","db_reachable":true,
                "schema_version":4,"global_revision":195,
                "supported_schema_version":4,"migration_compatible":true}
GET /health → {"status":"ok","service":"minisa-core","schema_version":4, …}
schema after startup: 4
```

Candidate core tree (schema 5) against the same restored image — the F1
mechanism, reproduced end to end:

```
GET /ready  → {"status":"ready", …,"schema_version":5,
                "supported_schema_version":5,"migration_compatible":true}
schema after startup: 5
memory_records present: 1
```

The rollback only holds if the predecessor core code is in place first — which
is why §7.2 precedes §7.3 and why `migration_compatible` is a mandatory check.

### 6.3 Production and the staged release are untouched

| Check | Expected | Observed |
| --- | --- | --- |
| Live database SHA-256 | `8d7c11be4c55e2956473c68ec8f1c4bff48b12d0ed6e56b7aec69e450c4508a0` | identical |
| Live database size / mtime | 692224 / 2026-10-19 07:22:31 | identical |
| `minisa.service` | PID 1324, start 2026-10-21 07:01:22 BST | identical |
| `minisa-core.service` | PID 1007, start 2026-10-21 07:01:17 BST | identical |
| Staged manifest SHA-256 | `410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405` | identical |
| Staged tracked files vs manifest | 20/20 match | 20/20 match |
| Candidate vs staged (tracked) | identical | 20/20 identical, 0 differing |
| Staged tree vs candidate tree | no content differences | 0 differences (only candidate-only docs/ops/tests) |
| Untracked `.pyc` in the staged release | 54, untouched | 54, untouched |

---

## 7. Deliberate behaviour changes inside the runbook

1. **Restored database mode `0600` → `0644`** (§7.3 step 3 and §8 step 5). The
   live database is `sbeppi:sbeppi 0644`; the restore now reproduces that owner
   and mode. The capture's own copy stays `0600` inside the capture package.
2. **`sudo systemctl daemon-reload` added to §8 step 7** — the archive restores
   the unit files, so the daemon must reload them before the services start.
3. **§8 step 6 now checks `--expect-no-table memory_records`** — the absence of
   that table is the proof that migration 5 was undone rather than re-applied.
4. **§6 post-deployment verification** now uses the helper instead of a single
   `SELECT`, so the deployment path checks the same invariants as the rollback
   path.

---

## 8. What this does not change — deployment is still NO-GO

Closing F1 and F2 fixes the *procedure*; it removes no blocker. Still
outstanding, unchanged from the rehearsal report:

1. **No independent recovery destination.** The capture guard refuses every
   local path; a same-device path is not a backup.
2. **No root/administrator session.** The unit file is unreadable by this
   account, and the drop-in install, the core-code copy, `systemctl stop/start`
   and `capture_minisa_recovery.py --execute-maintenance` all need root.
3. **No verified full-system recovery capture.** Only a database capture was
   rehearsed; the full archive has never been taken on an independent device.
4. **No approved maintenance window and no deployment approval.**
5. **The L2 rollback has never been executed against production**, only against
   disposable copies (this work) and the rehearsal's isolated exercises.

Root-only steps in the new §7.2/§7.3 remain unexercised on the real system:
`systemctl stop/start`, the drop-in removal, `daemon-reload`, the copy of the
predecessor core files into `/srv/minisa/app/core/`, and the install of the
captured database over `/srv/minisa/data/minisa.db`. They are written to match
the observed unit and file layout (`systemctl show` output, `/srv/minisa/app`
layout, `minisa.toml`), and the verification helper proves the *state* they
must produce — but they must be dry-run against a copy and signed off by the
administrator before the window.

Open assumptions that a root session must confirm: the exact `WorkingDirectory`
after the drop-in removal is the base release (confirmed by `systemctl show` on
the live unit, unreadable unit file), and the presence of
`/srv/minisa/app/core/db.py.pre-v5-<stamp>` and
`core/migrations.py.pre-v5-<stamp>` — which §5 step 0 creates during the
window. If §5 step 0 was not run, L2 has no predecessor core code and the
rollback cannot be completed.

---

## 9. Reproduce

```bash
cd /home/sbeppi/minisa-working-assistant-20261018/candidate

# the new tests
PYTHONWARNINGS=error::ResourceWarning /usr/bin/python3 -m unittest tests.test_rollback_runbook -v

# the full suite, both interpreters
PYTHONWARNINGS=error::ResourceWarning /usr/bin/python3 -m unittest discover -s tests
PYTHONWARNINGS=error::ResourceWarning /srv/minisa/.venv/bin/python3 -m unittest discover -s tests

# the helper's own interface
python3 ops/verify_recovery_database.py --help
python3 ops/verify_recovery_database.py --core-tree /srv/minisa/app --core-only
```

Read-only preflight of the release (still reports blocked — see §8):

```bash
python3 ops/preflight_learning_cycle_release.py \
  --recovery-capture /mnt/minisa-recovery/minisa-recovery-<capture-id>
```
