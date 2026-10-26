# Deployment rehearsal report — Minisa learning-cycle release (2026-10-21)

**Verdict: NO-GO for deployment today.** The rehearsal itself succeeded: every
mechanical step that does not require root, an independent destination or an
approved window was executed against disposable copies of the *production-shaped*
database and passed. The blockers are unchanged from
`RECOVERY_READINESS_REPORT_20261021.md` and are physical/administrative, not
software defects. Two runbook corrections must be applied before a window is
scheduled (findings **F1** and **F2**).

- Rehearsal root (all disposable artifacts, retained):
  `/tmp/opencode/rehearsal-20261021T184629Z`
- Report date: 2026-10-21 (UTC timestamps below are from the run itself)
- Production impact: **none**. Verified by before/after snapshots (§4).

---

## 1. Scope and safety boundary

| Rule | How it was honoured | Evidence |
| --- | --- | --- |
| No deploy / no copy into the active release | Nothing was written to `/home/sbeppi/minisa-releases/*` or `/srv/minisa/app`. The candidate core was copied into a temp dir only. | §7.2 staging check |
| No migration against the live database | Every migration ran on `db/*.db` copies made with SQLite's backup API | §3, §5 |
| No service restart / stop / reload / reconfigure | `systemctl` was never used to change service state; only `systemctl show` (read) | §4 |
| No modification of `/etc`, `/srv/minisa`, the live DB, the staged release | Only read access; the live DB was opened exclusively through `file:…?mode=ro` URIs | §4 |
| No privilege escalation, no permission changes | Every command ran as `sbeppi`; `sudo` was never invoked | §9.2 |
| No deletion or overwriting of existing data | Only new files under the rehearsal root; one temporary probe dir was created and removed | §11 |
| No weakened tests, no faked results | Every result below is a recorded exit code / digest; two rehearsal deviations are explicitly flagged (F4, and the `--same-owner` drop) | §6, §8 |
| A same-disk backup is never called independent recovery | The capture utility's own guard was exercised and it refused all three local destinations | §7.4 |

Rehearsal helper scripts live only in the rehearsal root (`snapshot.py`,
`rehearse_migration.py`, `rehearse_failure_paths.py`, `rehearse_dream_cycle.py`,
`rehearse_restore_and_rollback.py`, `rehearse_activation.py`,
`rehearse_capture_machinery.py`). **The candidate workspace source tree was not
modified by the rehearsal** — the only file added to the candidate workspace is
this report.

---

## 2. Baseline

### 2.1 Manifest and staged release

| Item | Value |
| --- | --- |
| Manifest file | `/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021.manifest.json` |
| Manifest SHA-256 (file) | `410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405` |
| Manifest SHA-256 (as recorded inside the manifest) | `410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405` — identical |
| Drop-in SHA-256 | `9e647d83c90946cf894c6f66a3db48323078019f046f38fdfe5bf93d35170879` |
| `ops/verify_candidate_manifest.py` | `verified: true`, `runtime_file_count: 20`, `production_deployed: false` |
| Manifest rebuilt to a temp file | not re-run in this rehearsal (read-only requirement); the earlier byte-identical rebuild remains valid |

### 2.2 Candidate workspace vs staged release (read-only comparison)

All 80 tracked (non-`.pyc`) files of the staged release are **byte-identical** to
their counterparts in the candidate workspace: **0 differing files, 0 staged-only
files**. The 46 candidate-only files are documentation, `ops/` utilities and
`tests/` — none of them are part of the release payload, so the staged release
is a faithful copy of the candidate's runtime files.

The staged release additionally contains **54 `.pyc` files** in 10 `__pycache__`
directories (byproducts of the earlier staged-artifact smoke test). They are not
in the manifest, are not part of the release contract, and were left untouched
(F6).

### 2.3 Production database identification

| Item | Value |
| --- | --- |
| Live database path | `/srv/minisa/data/minisa.db` |
| Identified from | `/srv/minisa/config/minisa.toml` → `workspace_database = "/srv/minisa/data/minisa.db"`; `/srv/minisa/app/core/app.py:12` → `DB_PATH = os.environ.get("MINISA_DB_PATH", "/srv/minisa/data/minisa.db")` |
| Journal mode | `wal` (sidecars `minisa.db-wal` 4 157 112 B, `minisa.db-shm` 32 768 B) |
| Schema version | 4 (`installation.schema_version`), `schema_migrations` rows 1–4 |
| Global revision | 195 (`core_state.global_revision`) |
| Integrity | `PRAGMA integrity_check` → `ok` |
| Tables / rows | 17 tables: `audit_events` 430, `backups` 5, `cognitive_interrupts` 1, `cognitive_metrics` 1, `cognitive_migrations` 1, `cognitive_records` 153, `core_meta` 0, `core_state` 1, `dream_results` 4, `dream_state` 1, `idempotency_records` 41, `installation` 1, `jobs` 49, `learned_procedures` 0, `schema_migrations` 4, `workspace_evidence` 37, `workspace_units` 36 |
| Core service code | `/srv/minisa/app/core/db.py`, `/srv/minisa/app/core/migrations.py` — unchanged by the rehearsal |
| Services | `minisa.service` active (PID 1324, started 2026-10-21 07:01:22 BST), `minisa-core.service` active (PID 1007, started 07:01:17 BST) — both original start times, i.e. no restart occurred |

No secret or credential values were read or recorded. The only configuration
inspected was path-valued keys (`root`, `workspace_database`, `DB_PATH`).

---

## 3. Isolated rehearsal workspace and the disposable copy

```
/tmp/opencode/rehearsal-20261021T184629Z/
├── db/       disposable database copies
├── logs/     machine-readable results of every step below
├── restore/  capture package, restore target, staging tree, release-boot roots
├── capture-rehearsal/  capture-utility rehearsal package
├── core-copy/  core-service code-copy rehearsal (§7.2)
├── hybrid/   hybrid release tree (L1b)
└── workspace/  production-shaped workspace (read-only copy of the file trees)
```

### 3.1 Copy method

The live database is in WAL mode and the service is running, so a naive file
copy can capture a torn state. Two independent, verified methods were used:

1. **SQLite online backup API** (primary):
   `sqlite3.connect('file:/srv/minisa/data/minisa.db?mode=ro', uri=True)` →
   `dst.backup(src)` → `db/minisa-live-copy.db` (733 184 B).
2. **The capture utility's own backup routine** (cross-check):
   `ops/capture_minisa_recovery.py::sqlite_backup()` → `capture-rehearsal/.../sqlite/minisa.db`
   (uses `src.backup(out, pages=256, sleep=0.05)`, then runs its own
   `PRAGMA integrity_check` → `["ok"]`).

### 3.2 Copy verification

| Check | Result |
| --- | --- |
| Copy integrity | `ok` |
| Per-table row counts | identical to live for all 17 tables |
| Per-table content SHA-256 (all rows, canonicalised) | identical to live for all 17 tables |
| Schema objects | 27 identical (types, names, SQL) |
| `schema_migrations` history | identical (v1–v4 with the production checksums) |
| `installation` row | identical (`installation_id e79d24ca-…`, schema_version 4, software_version `1.0.0-stage1`) |

The production file tree (`CORE`, `EXPERIENCE`, `MEMORY`, `PERSONALITY`,
`REFLECTION`, `SELF_MODEL`, `STATE`, `USER`) was copied read-only into
`workspace/` so that the dream rehearsal could run against realistic file
state; the production originals were never opened for writing.

---

## 4. Before/after production verification

The whole rehearsal took ~11 minutes. Live database compared before and after:

| Property | Before (18:46:29Z) | After (19:57:41Z) | Verdict |
| --- | --- | --- | --- |
| File SHA-256 | `8d7c11be4c55e295…` | `8d7c11be4c55e295…` | **identical (byte-for-byte)** |
| File size | 692 224 | 692 224 | identical |
| File mtime | 2026-10-19T06:22:31.168563Z | 2026-10-19T06:22:31.168563Z | identical |
| `-wal` / `-shm` size | 4 157 112 / 32 768 | 4 157 112 / 32 768 | identical |
| `PRAGMA integrity_check` | ok | ok | identical |
| Schema version | 4 | 4 | unchanged |
| Global revision | 195 | 195 | unchanged |
| Applied migrations | 1,2,3,4 | 1,2,3,4 | unchanged |
| Per-table content digests | — | — | **no table changed** |
| Schema objects | 27 | 27 | unchanged |
| `minisa.service` | active, PID 1324 | active, PID 1324, same start timestamp | not restarted |
| `minisa-core.service` | active, PID 1007 | active, PID 1007, same start timestamp | not restarted |
| Staged release manifest | `410d865e…` | `410d865e…` | unchanged |
| Staged release file count | 80 tracked + 54 `.pyc` | 80 tracked + 54 `.pyc` | unchanged |

The main database file did not even change mtime, i.e. no checkpoint was forced
and no write transaction occurred against the live file. All inspection
connections were opened with `mode=ro` (plus `PRAGMA query_only=ON` where the
candidate `core.db` API was used).

---

## 5. Migration 5 rehearsal (disposable copy only)

Target: `db/migration5-target.db` (copy of the live database).

### 5.1 Before the migration

- `verify_migrations(conn)` → **4**, `current_schema_version` → 4,
  `db.status()` → `{schema_version: 4, global_revision: 195, installation_id: e79d24ca-…}`.
- The applied migration chain (1–4) matched the candidate chain's checksums
  exactly; the candidate adds v5 `sourced_memory_records`.
- Per-table row counts and content digests for all 16 protected tables were
  recorded (`logs/migration5-apply.json` → `before`).

### 5.2 Applying the migration

Applied with the candidate code through the documented entry point
(`core.migrations.apply_migrations`, the same function
`core.db.Database.initialize()` calls):

```
schema 4 → 5
```

### 5.3 After the migration

| Check | Result |
| --- | --- |
| `verify_migrations` | 5 |
| `installation.schema_version` | 5 |
| `installation.installation_id` | unchanged (`e79d24ca-…`) |
| `global_revision` | **195 → 195 (unchanged)** |
| `PRAGMA integrity_check` | `ok` |
| `PRAGMA foreign_key_check` | no violations |
| Migration history | v1 `stage1_persistence_foundation`, v2 `cognitive_state_store`, v3 `persistent_workspace`, v4 `phase5_hardening` (original timestamps preserved) + v5 `sourced_memory_records` (checksum `a06db7fb269e7e7c…`) |
| `learned_procedures` shape | new column `source_evidence_ids` added; `global_revision`, `created_at`, `last_used_at`, `usage_count`, `signature` present; 0 rows (as before) |
| `memory_records` | created: `id`, `memory_id`, `details_json`, `revision`, `created_at` |
| New indexes | `procedures_revision`, `procedures_title`, `procedures_usage`, `memory_id_unique` (unique), `memory_revision` |

### 5.4 Protected-row comparison (the central question)

| Table | Rows before → after | Content digest | Note |
| --- | --- | --- | --- |
| `audit_events` | 430 → 430 | identical | |
| `backups` | 5 → 5 | identical | |
| `cognitive_interrupts` | 1 → 1 | identical | |
| `cognitive_metrics` | 1 → 1 | identical | |
| `cognitive_migrations` | 1 → 1 | identical | |
| `cognitive_records` | 153 → 153 | identical | |
| `core_meta` | 0 → 0 | identical | |
| `core_state` | 1 → 1 | identical | revision preserved |
| `dream_results` | 4 → 4 | identical | |
| `dream_state` | 1 → 1 | identical | |
| `idempotency_records` | 41 → 41 | identical | |
| `installation` | 1 → 1 | **changed (expected)** | only `schema_version` 4 → 5; `software_version` unchanged (`1.0.0-stage1`) |
| `jobs` | 49 → 49 | identical | |
| `schema_migrations` | 4 → 5 | **changed (expected)** | one appended row for v5 |
| `workspace_evidence` | 37 → 37 | identical | |
| `workspace_units` | 36 → 36 | identical | |
| `learned_procedures` | 0 → 0 | identical (empty) | table *shape* changed, data preserved |
| `memory_records` | absent → 0 | new table | empty until the dream worker writes |

**Conclusion:** every data-bearing protected table is byte-identical before and
after; the only differences are the two intended metadata updates
(`installation.schema_version`, one appended `schema_migrations` row).

### 5.5 Failure handling (separate disposable copy)

Target: `db/failure-copy.db` (independent copy of the same live snapshot).
Injected fault: a pre-existing `memory_records` table — exactly the
half-migrated state that would break the migration near its end, *after* the
`learned_procedures` rebuild has already dropped and renamed the old table.

| Check | Result |
| --- | --- |
| `apply_migrations` raised | yes (`sqlite3.OperationalError`) |
| `schema_migrations` rows with version 5 | 0 — no partial history |
| `verify_migrations` after failure | 4 (unchanged) |
| `installation.schema_version` | 4 (unchanged) |
| `learned_procedures` columns | original v4 shape (`procedure_id, situation, procedure, evidence_json, confidence, revision`) — the rebuild was rolled back |
| `learned_procedures` rows | 1 (unchanged) |
| All protected tables | content digests identical to the pre-failure state |
| `PRAGMA integrity_check` | `ok` |

**Conclusion:** migration 5 is atomic on production-shaped data. A failure
part-way through leaves the database exactly as it was — there is no
half-migrated state to clean up.

### 5.6 Non-empty `learned_procedures` (gap in production coverage)

Production has **0** rows in `learned_procedures`, so the rebuild's row-copy
path is never exercised in the real system. Target: `db/nonempty-copy.db`
(3 synthetic v4-shaped rows inserted before the migration).

| Mapping (migration SQL → v5 column) | Verified |
| --- | --- |
| `procedure_id` preserved | yes |
| `situation` → `situation` | yes |
| `procedure` → `procedure` | yes |
| `evidence_json` → `evidence_json` | yes |
| `evidence_json` → `source_evidence_ids` | yes |
| `revision` → `global_revision` | yes |
| `situation` → `title` | yes |
| `procedure_id` → `signature` | yes |
| `confidence` preserved | yes |
| `usage_count` reset to 0 | yes |
| `provenance_json` rewritten to `authority=dream_hypothesis, executable=0, admission=pre_migration_record` | yes |

3 rows in → 3 rows out, `integrity_check ok`, no FK violations.

### 5.7 Full dream cycle on the upgraded schema (production-shaped data)

Run against `db/migration5-target.db` inside the copied production workspace
(37 production evidence rows, 36 production units, real CORE/STATE/PERSONALITY
files). Three synthetic turns were ingested into the *copy* to give the dream
worker admissible evidence.

| Check | Result |
| --- | --- |
| Proposal validation (real validator) | passed |
| Dream tick | ran, returned a result (not deferred, not failed) |
| Accepted changes | 2 (one `memory`, one `procedure`) |
| Review-only changes | 0 |
| Revision | 195 → 199 |
| `memory_records` after the cycle | 1 row — the new table is written correctly at runtime |
| `learned_procedures` after the cycle | 1 row written into the **rebuilt** table: `source_evidence_ids = ["turn_rehearsal_0","turn_rehearsal_1","turn_rehearsal_2"]`, `provenance_json = {"admission":"evidence_supported_not_causal","authority":"dream_hypothesis","executable":false}` |
| `PRAGMA integrity_check` | ok |
| Tables changed by the cycle | `audit_events`, `cognitive_metrics`, `core_state`, `dream_results`, `dream_state`, `idempotency_records`, `jobs`, `learned_procedures`, `memory_records`, `workspace_evidence`, `workspace_units` — exactly the runtime's own writes |
| Tables unchanged by the cycle | `backups`, `cognitive_interrupts`, `cognitive_migrations`, `cognitive_records`, `core_meta`, `installation`, `schema_migrations` |

**Conclusion:** the learning cycle works end-to-end on the migrated
production-shaped schema, and runtime writes fit the v5 column set.

---

## 6. Release validation

Exact commands and actual outcomes (all run from
`/home/sbeppi/minisa-working-assistant-20261018/candidate`):

| Command | Outcome |
| --- | --- |
| `PYTHONWARNINGS=error::ResourceWarning /srv/minisa/.venv/bin/python3 -m unittest discover -s tests` | `Ran 188 tests … OK (skipped=6)` |
| `PYTHONWARNINGS=error::ResourceWarning /usr/bin/python3 -m unittest discover -s tests` | `Ran 188 tests … OK (skipped=6)` |
| `PYTHONWARNINGS=error::ResourceWarning /srv/minisa/.venv/bin/python3 -m unittest tests.test_migration_chain` | `Ran 6 tests … OK` |
| `… -m unittest tests.test_learning_acceptance` | `Ran 3 tests … OK` |
| `… -m unittest tests.test_learning_cycle` | `Ran 7 tests … OK (skipped=1)` |
| `… -m unittest tests.test_deployment_preflight` | `Ran 38 tests … OK` |
| `… -m unittest tests.test_recovery_capture` | `Ran 9 tests … OK` |
| `… -m unittest tests.test_dream_recovery` | `Ran 8 tests … OK` |
| `/srv/minisa/.venv/bin/python3 ops/preflight_learning_cycle_release.py --json <report>` | exit code **2**, `preflight: blocked`, `read_only: true` |

The 6 skips in the full suite are the opt-in live-model tests (they require
`MINISA_LIVE_DREAM_HOST` / `MINISA_LIVE_DREAM_MODEL`); they were not exercised
here because the configured remote host `100.123.208.117:11434` is unreachable
and the local Ollama serves only a non-embedding model.

### 6.1 Preflight result — expected blocks vs. genuine failures

| Check | Status | Reading |
| --- | --- | --- |
| `manifest` | passed | 20 changed files, supported schema 5, `production_deployed=false` |
| `release_content` | passed | 20 tracked files match the manifest; 54 untracked bytecode files present (F6) |
| `migration_lineage` | passed | production migrations 1–4 match the candidate chain; candidate adds 5 |
| `live_database` | passed | schema 4, revision 195, integrity ok, opened read-only |
| `core_service_files` | passed | live core differs from the candidate for `db.py`/`migrations.py`, as expected before the copy step |
| `service_configuration` | **failed** | **expected block** — `/etc/systemd/system/minisa.service` is unreadable by this account; requires a root session |
| `independent_destination` | **failed** | **expected block** — no mounted filesystem is independent of `fedora-root` / `fedora-server` / `nvme0n1` |
| `recovery_point` | **failed** | **expected block** — no verified capture supplied |
| `rollback_prerequisites` | passed | base release intact, live core hashes recorded, rollback model recorded |
| `tests` | passed | `OK (skipped=6)` |

**There are no genuine failures.** All three failures are the same
physical/administrative blockers already reported in
`RECOVERY_READINESS_REPORT_20261021.md`; the preflight correctly refuses to
report "ready" while they exist.

---

## 7. Rollback and activation: exercised vs. merely reviewed

### 7.1 Exercised (with disposable data and isolated release trees)

| Runbook step | How it was rehearsed | Result |
| --- | --- | --- |
| §4 capture machinery — online backup | `ops/capture_minisa_recovery.py::sqlite_backup()` on the live DB | integrity `ok`, schema 4, 0.01 s, source metadata copied |
| §4 capture machinery — archive | `tar` with the utility's flags over `/srv/minisa` + the active release | 4 133 files, 93.9 MB (exclusions: root-only paths, see F3) |
| §4 capture machinery — restore rehearsal | `ops/capture_minisa_recovery.py::restore_rehearsal()` | restored DB: integrity `ok`, schema 4, **SHA-256 identical to the captured backup**; file-ownership comparison reported 98 root-owned paths (F4) |
| §4 capture machinery — finalisation | `finalize_capture()` | `SHA256SUMS` (4 entries) written; `sha256sum -c SHA256SUMS` → all `OK`; `CAPTURE_VERIFIED` = `manifest_sha256=ed80fa0d…`; `CAPTURE_INCOMPLETE` removed |
| §4 destination-independence guard | `ops/capture_minisa_recovery.py <dir>` (read-only preflight mode) | refuses `/home/sbeppi/...` → *"backup destination shares a physical block device with production"*; refuses `/srv` → *"refusing root or Minisa data filesystem"*; refuses tmpfs → *"unverified destination filesystem/source"*; all exit 2, no writes |
| §5 step 1 — core-service code copy | candidate `core/db.py` + `core/migrations.py` copied to a temp app dir (mode 0644), then the documented `Database(...).initialize()` command | `schema_version = 5`, `integrity_check ok` |
| §5 verification queries | run against the migrated copy | integrity ok, migrations 1–5, `installation.schema_version` 5, revision unchanged, `memory_records` present and empty |
| §6/§7 L0 — fix-forward | the **candidate release booted against the migrated schema-5 database** in an isolated root | started successfully, `schema_version_seen: 5` |
| §7.1 L1b — hybrid release (candidate `core/` + base `runtime/`) | hybrid tree built in the rehearsal root; booted against the migrated schema-5 database | **started successfully**, `schema_version_seen: 5` |
| §7.1 L2 — data rollback via the capture | §8 restore procedure executed against a disposable target (see 7.2) | restored DB identical to the pre-migration capture |
| §7.2 — code-only rollback target | base release booted against the restored schema-4 database | started successfully, `schema_version_seen: 4` |

### 7.2 The restore procedure (§8) against disposable data

A capture package with the documented layout was built in the rehearsal root
(`restore/capture/`): `manifest.json`, `SHA256SUMS` (4 entries), `file-state.tar`,
`file-manifest.json`, `sqlite/minisa.db`, `CAPTURE_VERIFIED`. The restore target
(`restore/target/`) represented the live system *after* the migration and some
runtime activity (it held the migrated, dream-cycled database).

Executed steps and results:

| Step | Command (paths substituted) | Result |
| --- | --- | --- |
| 2 — verify the capture | `sha256sum -c SHA256SUMS` in the capture dir | exit 0; `file-state.tar: OK`, `sqlite/minisa.db: OK`, `file-manifest.json: OK`, `manifest.json: OK` |
| 2 — marker | `cat CAPTURE_VERIFIED` | `manifest_sha256=…` matches the manifest digest exactly |
| 3 — stage the file state | `tar --extract --file file-state.tar --directory <stage> --numeric-owner` | staged tree created (never extracted over `/`) |
| 4 — move into place | `rsync -a --delete <stage>/target/ <target>/` | applied to the disposable target only |
| 5 — replace the database | `rm -f minisa.db-wal minisa.db-shm` + `install -m 0600 sqlite/minisa.db minisa.db` | sidecars removed, DB installed mode 0600 |
| 6 — verify before starting anything | integrity + schema + revision queries | `integrity ok`, schema version **4**, revision **195**, 17 tables, `memory_records` absent, `learned_procedures` back to the v4 shape |
| 6 — comparison with the capture | per-table digests of restored vs captured | **all 17 tables identical, zero differences** |

The restore therefore returns the database to the exact pre-migration state —
the only path back to schema 4.

### 7.3 Rollback findings that change the runbook

- **F1 (high) — an L2 rollback as written in §7.3 is incomplete.** The runbook
  says "restore the verified pre-migration database from the capture", and §7.2
  says "keep the candidate `core/` files in place". Rehearsal results:
  1. With the candidate `core/` files still installed, `core.db.Database.initialize()`
     on the restored schema-4 database **re-applies migration 5 automatically**
     (observed: `schema_version = 5`). The core service's own startup path would
     re-migrate the freshly restored database, silently undoing the rollback.
  2. The candidate runtime release **refuses** the restored schema-4 database:
     `RuntimeError: Workspace requires reviewed Core migration before startup`
     (the schema gate at `runtime/server/application.py:112-114`).
  3. The base release boots the restored database correctly (`started: true`,
     `schema_version_seen: 4`).

  **Resolution (2026-10-21):** the three steps below are now written out as the ordered L2 procedure in `RECOVERY_RUNBOOK.md` §7.0–§7.5, and the hazard is covered by `tests/test_rollback_runbook.py`; see `ROLLBACK_RUNBOOK_FIX_REPORT_20261021.md`.

  **Therefore L2 requires three coordinated steps, in this order:** restore the
  predecessor core files (`core/db.py.pre-v5-<stamp>`,
  `core/migrations.py.pre-v5-<stamp>`), revert the release pointer to the base
  release (removing the drop-in), and restore the pre-migration database — then
  start the services. §7.3 must state this explicitly; it currently implies the
  database restore alone is sufficient.
- **L1b is real and works.** The hybrid release (candidate `core/` + base
  `runtime/`) started cleanly against the migrated schema-5 database. The hybrid
  correctly refuses a schema-4 database, so it is a *post-migration-only* target.
  Note the hybrid still needs its own manifest and verification before it could
  ever be pointed at by a unit file.

### 7.4 Steps that cannot be rehearsed here (and their exact prerequisites)

| Step | Why it cannot be rehearsed | Exact prerequisite | Safe next action |
| --- | --- | --- | --- |
| §4 capture to an independent destination | No independent filesystem exists; the guard refuses all local targets (§7.1) | An administrator attaches a physically independent device (or verifies a remote filesystem) and mounts it at e.g. `/mnt/minisa-recovery` | Re-run `ops/capture_minisa_recovery.py /mnt/minisa-recovery` in **read-only preflight mode** first (no `--execute-maintenance`), confirm it reports `preflight: passed`, then schedule the window |
| §4 capture with services stopped | `--execute-maintenance` stops `minisa.service` and `minisa-core.service` and requires root | Root (`sudo`) in an approved window | Same as above; the capture restores both services to their original state |
| §5 copy into `/srv/minisa/app/core` | Requires root and would modify the live application directory | Root + window + approval | Keep the rehearsal evidence; the copy semantics were verified in a temp dir (§7.1) |
| §6 drop-in install + `daemon-reload` | Requires root; `/etc/systemd/system/minisa.service` is unreadable by this account | Root + window | The drop-in content was verified (points at the staged release, `TimeoutStopSec=50`); `systemd-analyze verify` on the production unit also needs root |
| §8 restore over production | Requires root, a stopped service and a real capture | Root + window + verified capture | The restore mechanics are validated (§7.2); only the production execution remains |
| Live-model dream test (opt-in) | The configured Ollama host `100.123.208.117:11434` is unreachable; local Ollama has no embedding-capable model | Network access to the dream host and `MINISA_LIVE_DREAM_HOST`/`_MODEL` set | Run the live-model subset in the window as an extra check; it is not required for the migration itself |

---

## 8. Findings

| ID | Severity | Finding | Evidence / action |
| --- | --- | --- | --- |
| **F1** | high | L2 rollback as documented restores only the database; with the candidate core files in place the core service re-applies migration 5 on startup, and the candidate runtime refuses the restored schema-4 database | `logs/restore-and-rollback-rehearsal.json` → `candidate_core_initialize_on_restored_db.stdout == "5"`, `candidate_runtime_against_restored_db.error == "RuntimeError: Workspace requires reviewed Core migration before startup"`. **Amend runbook §7.2/§7.3 before the window.** — *Closed 2026-10-21: `RECOVERY_RUNBOOK.md` §7.0–§7.5 now specify the ordered code-then-data procedure; see `ROLLBACK_RUNBOOK_FIX_REPORT_20261021.md`.* |
| **F2** | medium | The `sqlite3` CLI is **not installed** on this host; the runbook's verification commands (§4 item 4, §5 verification block, §8 step 6) would fail with "command not found" | `command -v sqlite3` → empty. Substitute `python3 -c "import sqlite3; ..."` in the runbook — *Closed 2026-10-21: `ops/verify_recovery_database.py` replaces every `sqlite3` invocation in the runbook; see `ROLLBACK_RUNBOOK_FIX_REPORT_20261021.md`.* |
| **F3** | medium | The capture utility is root-only in more places than the runbook states: it archives `/etc/systemd/system/minisa.service` (unreadable), must archive the root-owned `/srv/minisa/backups/20261003T010357Z-r4b`, and its restore rehearsal uses `tar --same-owner` | `tar` errors captured during the rehearsal. The runbook already requires `sudo`, but the operator should expect the archive to fail without root rather than assuming a permissions problem is transient |
| **F4** | low | The restore rehearsal reports "content mismatch in 98 archived paths" without root — exactly the 98 root-owned paths, because `tar --same-owner` cannot restore ownership unprivileged. The **restored database itself verified correctly** (integrity ok, schema 4, SHA-256 identical to the capture) | `logs/capture-machinery-rehearsal.json` → `restore_rehearsal.root_owned_paths_in_archive == 98` equals the mismatch count. Not a content defect; do not run the capture without root |
| **F5** | medium (coverage gap, now closed) | Production has 0 rows in `learned_procedures`, so the migration's row-copy path was never exercised by production data | Rehearsed with 3 synthetic rows; all documented mappings verified (§5.6) |
| **F6** | low | The staged release carries 54 untracked `.pyc` files in 10 `__pycache__` directories | Not part of the manifest; harmless, but an operator may prefer to clean them before the window (cleanup command is in the readiness report) |
| **F7** | positive | Migration 5 is atomic on production-shaped data: an injected mid-migration conflict rolls the whole rebuild back | §5.5 |
| **F8** | positive | The full dream cycle runs on the upgraded production-shaped schema and writes both the new `memory_records` table and the rebuilt `learned_procedures` table | §5.7 |
| **F9** | positive | The candidate release, the base release and the L1b hybrid all boot correctly against their respective database versions in isolation | §7.1 |

### Open questions for the operator

1. Which independent destination will be used, and who verifies it is physically
   independent (the tool refuses anything sharing the production block device —
   so a second partition on the same NVMe will be rejected, correctly)?
2. Who executes the window with `sudo`, and is there a second person to verify
   the capture's `SHA256SUMS` and `CAPTURE_VERIFIED` marker?
3. Should the runbook's §7.3 be amended (F1) before the window, or should the
   amendment be deferred to the rollback moment? Recommended: amend now — the
   rollback path is the one path that cannot be rehearsed live.
4. Is `sqlite3` intended to be installed for the operator, or should the
   runbook use the Python equivalent (F2)?

---

## 9. Limitations of this rehearsal

1. **No production write of any kind was performed**, so the live system's
   behaviour under the *actual* deployment sequence (service stop, copy,
   migrate, start, first dream) was not observed end-to-end. The rehearsal
   approximated it by booting the release trees against copies of the live
   database and workspace.
2. **The capture rehearsal archive is incomplete by construction** (unit files
   and one root-owned directory excluded) and its restore rehearsal could not
   restore ownership for 98 root-owned paths (F3, F4).
3. **The dream rehearsal used three synthetic turns** for admissible evidence;
   the model was the scripted test double, not the live dream model. The live
   model path remains untested here (the configured host is unreachable).
4. **The `systemd` unit/drop-in mechanics were not exercised** — only their
   content was verified. `systemctl` state changes need root.
5. **Both capture packages under the rehearsal root are fixtures, not recovery
   points.** `capture-rehearsal/minisa-recovery-rehearsal/manifest.json` records
   `independent_from_live_block_devices: "rehearsal-fixture (NOT an independent destination)"`
   and `rehearsal_fixture: true`; `restore/capture/` was built by the rehearsal
   script with a plain local destination string. Both live on tmpfs on the
   production device and both must never be used to restore anything. Only the
   capture produced by `ops/capture_minisa_recovery.py` on a genuinely
   independent destination is a recovery point.
6. Two rehearsals deviate from the documented procedure for lack of privilege
   only: `tar --same-owner` was dropped (F4) and the unit-file archiving was
   skipped (F3). Both deviations are recorded in the machine-readable logs.

---

## 10. Recommendation

**NO-GO for deployment now.** The software path is rehearsed and clean; the
remaining blockers are physical/administrative and unchanged:

1. **Independent recovery destination** — none exists; the capture utility
   refuses every local path. Prerequisite: an administrator-attached,
   physically independent device mounted at `/mnt/minisa-recovery` (or a verified
   remote filesystem with `--confirm-independent-target`).
2. **Root session** — the capture, the core-file copy, the drop-in install, the
   service stop/start and the unit-file inspection all require root.
3. **Verified capture** — produced by the utility on the independent
   destination, with `sha256sum -c SHA256SUMS` passing, `CAPTURE_VERIFIED`
   matching the manifest digest, `CAPTURE_INCOMPLETE` absent, the captured
   database reporting `integrity ok` and schema 4, and the restore rehearsal
   passing.
4. **Approved maintenance window** — migration 5 is one-way; the window must
   cover capture → migration → activation → verification, with the capture
   verified *before* the migration runs.
5. **Runbook amendments F1 and F2** — recommended before the window; F1 affects
   the rollback path, which cannot be rehearsed live.

**Conditional GO for the procedure as rehearsed** once 1–5 are satisfied: the
migration, the runtime writes, the L1b hybrid rollback, the base-release
rollback, and the database restore have each been exercised successfully on
production-shaped disposable data.

---

## 11. Artifact inventory (retained, not cleaned up)

```
/tmp/opencode/rehearsal-20261021T184629Z/            210 MB, tmpfs, retained
├── snapshot.py                     read-only snapshot helper (metadata, digests)
├── rehearse_migration.py           §5.2–5.4 migration + protected-row comparison
├── rehearse_failure_paths.py       §5.5 failure path, §5.6 non-empty procedures
├── rehearse_dream_cycle.py         §5.7 full dream cycle on the upgraded copy
├── rehearse_restore_and_rollback.py§7.1–7.3 restore procedure + rollback ladder
├── rehearse_activation.py          §7.1 L0 activation, core copy, drop-in, systemd probe
├── rehearse_capture_machinery.py   §7.1 capture machinery + independence guard
├── db/
│   ├── minisa-live-copy.db         consistent backup-API copy of the live DB (schema 4)
│   ├── migration5-target.db        copy migrated to schema 5 (+ dream cycle applied)
│   ├── failure-copy.db             copy with the injected mid-migration fault
│   └── nonempty-copy.db            copy with 3 synthetic learned_procedures rows
├── logs/                            every result above as JSON/TXT (see below)
├── restore/                         capture package, restore target, staging tree, boot roots
├── capture-rehearsal/               capture-utility rehearsal package (NOT a recovery point)
├── core-copy/                       core-service code-copy rehearsal tree
├── hybrid/release/                  hybrid release tree (candidate core + base runtime)
└── workspace/                       read-only copy of the production file trees
```

Key logs: `live-before.json`, `live-after.json`, `copy-after-backup.json`,
`migration5-apply.json`, `migration5-failure-paths.json`,
`dream-cycle-on-upgraded-copy.json`, `restore-and-rollback-rehearsal.json`,
`activation-rehearsal.json`, `capture-machinery-rehearsal.json`,
`preflight-final.json`, `test-suite.txt`, `test-modules.txt`,
`manifest-verify.json`.

The rehearsal root is on tmpfs and will not survive a reboot; copy it elsewhere
if the evidence is needed later. Nothing in it is required for the deployment
itself.

---

*Rehearsal performed 2026-10-21 against the candidate learning-cycle release.
Production was not modified; the live database file is byte-identical before and
after (§4).*
