# Recovery runbook — Minisa learning-cycle deployment (2026-10-21 candidate)

Scope: the staged candidate
`/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021`
(manifest `.../minisa-learning-cycle-candidate-20261021.manifest.json`,
manifest SHA-256 `410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405`),
which requires production database **schema version 5** while production is
currently at **schema version 4**.

This runbook is the operator procedure. It does not authorise anything by
itself: every phase below has a gate, and the gates are currently **closed**
(see §1). Do not skip a phase because a later one looks routine.

Everything in §1–§9 was verified read-only on 2026-10-21; the evidence is in
§11.

---

## 1. Current gate status (2026-10-21, read-only inspection)

| Gate | State | Blocking reason |
| --- | --- | --- |
| G1 Manifest + staged release verified | **open** | manifest and all 20 tracked files match; `production_deployed: false` |
| G2 Migration lineage continues production | **open** | live migrations 1–4 match the candidate chain; candidate adds migration 5 only |
| G3 Independent recovery destination | **CLOSED** | the host has exactly one physical disk (`nvme0n1`); every filesystem (`/`, `/home`, `/srv`, `/usr/local/lib/ollama`, `/boot`) descends from it. No USB, NFS, CIFS or other independent target is mounted or configured. |
| G4 Verified full recovery point | **CLOSED** | depends on G3. Existing material is database-only and same-device (see §1.1) |
| G5 Maintenance window with root | **CLOSED** | no administrator-authenticated root session; the unit files are not readable by the service account |
| G6 Tests green | **open** | 188 tests pass in the candidate workspace (§3) |
| G7 Service configuration ready | **CLOSED** | the effective unit file cannot be read by the service account; the drop-in install path is free |

Deployment is **blocked** by G3, G4, G5 and G7. No production change may be
started until all four are closed by an administrator.

### 1.1 Recovery material that does exist (same physical device — not independent)

| Artifact | Location | Device | State |
| --- | --- | --- | --- |
| Database-only capture, 2026-10-18 | `/home/sbeppi/minisa-recovery-backups/dream-recovery-db-20261018T123156Z` | `/` → `nvme0n1` | `SHA256SUMS` match, `integrity_check ok`, schema 4, revision 42, restore rehearsal passed. **Database component only** — no file trees, config, units or release. |
| 5 registry backups | `/srv/minisa/backups/stage1-*` | `/srv` → `nvme0n1` | recorded SHA-256 all match, `integrity_check ok`, but schema version 1–2 (September), `restore_test_status` is `NULL` for all five |
| Older deploy/pre-stage dirs | `/srv/minisa/backups/{20261003T010357Z-r4b,deploy-*,pre-stage1-*}` | `/srv` → `nvme0n1` | unverified |

None of these survives the loss of the NVMe device, and none of them is the
coordinated full-system recovery point the deployment gate requires. They are
useful only for the narrow, already-completed 2026-10-18 investigation.

---

## 2. Roles, prerequisites, and the independent destination

**Who is needed.** An administrator who can authenticate with `sudo` locally on
this host. Never disable authentication, change sudoers, or pass a password
through chat.

**What must be attached.** A physical backup device with enough capacity for the
full capture, mounted at `/mnt/minisa-recovery`, whose failure domain is
independent of `nvme0n1`. A second directory, a separate logical volume, an
LVM snapshot, or another path on the same NVMe is **not** independent. For a
remote filesystem, the administrator must independently verify the storage and
failure domain and pass `--confirm-independent-target`.

**Capacity.** The capture utility reports the required and available bytes in
its read-only preflight. It does not estimate duration from size alone: use
the verified target throughput to set the window, and add the restore
rehearsal plus service restart time.

**Current topology that makes this impossible as-is.**

```
nvme0n1 (WD SN740 SDDQNQD-256G-1014, 238.5G)   <- the only physical device
├── nvme0n1p1  vfat   /boot/efi
├── nvme0n1p2  xfs    /boot
└── nvme0n1p3  LVM2_member
    ├── fedora-root    xfs  /            (also /home, /var)
    └── fedora-server  xfs  /srv         (also /usr/local/lib/ollama)
zram0  swap
```

`/mnt`, `/media` and `/run/media` are empty, `/etc/fstab` declares no external
device, no backup or capture timer exists in systemd, and the only non-local
mount is `sunrpc` (`rpc_pipefs`), which is a kernel pipe and not storage.

**Verify the destination yourself before trusting any tool:**

```bash
findmnt -o TARGET,SOURCE,FSTYPE
lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINT,MODEL
lsblk -srno NAME "$(findmnt -n -o SOURCE -T /)"      # ancestry of /
lsblk -srno NAME "$(findmnt -n -o SOURCE -T /srv)"    # ancestry of /srv
```

If the destination ancestry intersects the production ancestry, there is no
independent destination and the deployment stays blocked.

---

## 3. Phase 0 — read-only preflight (no window needed)

Run the candidate preflight. It only reads; it never stops a service, never
writes to the database, and never modifies the release.

```bash
python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/preflight_learning_cycle_release.py \
  --recovery-capture /mnt/minisa-recovery/minisa-recovery-<capture-id>
```

Exit code `0` means every required prerequisite was positively verified;
`2` means at least one required prerequisite failed **or could not be verified**
(fail closed); `3` means the preflight itself could not run. The JSON report
lists each check with its evidence and the `blocking_checks` list.

| Check ID | What it verifies |
| --- | --- |
| `manifest` | manifest parses, has all required keys, `production_deployed: false`, no duplicate/unsafe paths |
| `release_content` | every one of the 20 tracked files in the staged release matches the manifest byte-for-byte, drop-in hash matches |
| `migration_lineage` | the candidate chain's checksums for 1–4 equal the checksums recorded in the live `schema_migrations` table, the production sequence is contiguous, and exactly one migration (5) is pending |
| `live_database` | the live database opens read-only, `PRAGMA integrity_check` is `ok`, schema version is 4 (pre-migration) or 5 (post-migration) |
| `core_service_files` | the live core-service code differs from the candidate exactly as expected before the copy step |
| `service_configuration` | both units' effective properties, `WorkingDirectory` still on the base release, drop-in path free, no dream-timeout override |
| `independent_destination` | a mounted filesystem whose block-device ancestry does not intersect production's |
| `recovery_point` | the capture directory is verified: `capture_status: verified`, `CAPTURE_VERIFIED` digest matches, no `CAPTURE_INCOMPLETE`, all `SHA256SUMS` entries match, captured database integrity `ok` |
| `rollback_prerequisites` | base release intact, live core hashes recorded, rollback model recorded |
| `tests` | the candidate test suite runs green (nested runs are reported but not blocking) |

Also run the recovery utility's own read-only preflight as the administrator:

```bash
sudo python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/capture_minisa_recovery.py \
  /mnt/minisa-recovery
```

It must report `preflight: passed`, name the independent target, and list the
required and available capacity. A `blocked` result here ends the attempt.

---

## 4. Phase 1 — pre-migration backup (inside the approved window)

Order matters: the backup must be taken **before** migration 5 touches the
database, and it must be verified **before** the migration runs.

```bash
sudo python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/capture_minisa_recovery.py \
  /mnt/minisa-recovery --execute-maintenance
```

The utility stops `minisa.service` and `minisa-core.service`, acquires the
existing Minisa locks without creating or truncating them, takes a SQLite
online backup from a read-only connection, archives `/srv/minisa` (including
`/srv/minisa/app`), the active release, the systemd units and drop-in
directories with ownership/ACL/xattr/SELinux metadata, runs a disposable
restore rehearsal, and restores both services to their original active or
inactive state.

The capture directory contains:

```
minisa-recovery-<capture-id>/
├── manifest.json        capture_status: verified, destination, service states, database metadata
├── SHA256SUMS           4 entries: file-state.tar, sqlite/minisa.db, file-manifest.json, manifest.json
├── file-state.tar       /srv/minisa (without the raw database and its sidecars), the release, the units
├── file-manifest.json   per-path mode/uid/gid/size/sha256
├── sqlite/minisa.db     the SQLite online backup (mode 0600)
├── CAPTURE_VERIFIED     manifest_sha256=<digest of manifest.json>
└── restore-rehearsal/   the disposable rehearsal tree
```

**Do not accept anything less than all of this:**

1. the command reported a verified capture;
2. `CAPTURE_VERIFIED` exists and `CAPTURE_INCOMPLETE` does not;
3. `sha256sum -c SHA256SUMS` passes inside the capture directory;
4. the captured database passes `PRAGMA integrity_check` and reports
   `schema_version` 4 (pre-migration) — recheck it independently with the
   read-only verification helper. The `sqlite3` command-line program is **not
   installed on this host**, so no step in this runbook may use it:

   ```bash
   python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/verify_recovery_database.py \
     --db /mnt/minisa-recovery/minisa-recovery-<capture-id>/sqlite/minisa.db \
     --expect-schema-version 4 --expect-migrations 1,2,3,4 --expect-integrity ok
   ```

   Exit code `0` with `VERDICT: PASSED` is required. A non-zero exit names the
   failing check; exit `2` means the check could not be run at all. The helper
   opens the database read-only and never writes to it.

5. the rehearsal directory exists and the script reported the restore rehearsal
   as passed. The rehearsal must never be restored over production.

A directory without the verified marker is incomplete even if it contains a
manifest, and it must not be used.

**Note.** `ops/capture_minisa_recovery.py` pins `ACTIVE_RELEASE` to the
pre-deployment release. If a capture is ever taken after the candidate release
is active, that constant must be updated first, otherwise the archive will
contain the old release instead of the running one.

---

## 5. Phase 2 — migration 5 (core service code copy + schema upgrade)

Migration 5 (`sourced_memory_records`) is **not** a pure table addition: it
rebuilds `learned_procedures` into a new table with an added
`source_evidence_ids` column, copying every existing row, drops the old table,
renames the new one, adds three indexes, and creates `memory_records`. Rows are
preserved; the table shape changes. There is **no down migration**.

Steps, in this order, with both services stopped:

```bash
# 0. keep the predecessor core code recoverable
sudo cp -a /srv/minisa/app/core/db.py        /srv/minisa/app/core/db.py.pre-v5-<stamp>
sudo cp -a /srv/minisa/app/core/migrations.py /srv/minisa/app/core/migrations.py.pre-v5-<stamp>

# 1. install the candidate core files (candidate root -> core service application dir)
sudo install -o sbeppi -g sbeppi -m 0644 \
  /home/sbeppi/minisa-working-assistant-20261018/candidate/core/db.py \
  /srv/minisa/app/core/db.py
sudo install -o sbeppi -g sbeppi -m 0644 \
  /home/sbeppi/minisa-working-assistant-20261018/candidate/core/migrations.py \
  /srv/minisa/app/core/migrations.py

# 2. apply the migration explicitly (atomic per migration, rolls back on failure)
cd /srv/minisa/app && /srv/minisa/app/.venv/bin/python3 -c "
from core.db import Database
db = Database('/srv/minisa/data/minisa.db')
print('schema_version =', db.initialize())
"
```

`core/db.py:initialize()` is the same entry point the core service uses on
startup (`core/app.py` startup event), so restarting `minisa-core.service`
after the copy would also apply the migration — apply it explicitly first so
the outcome is observed, not inferred.

**Verification (all must hold before continuing):**

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

Expected: `VERDICT: PASSED`; `integrity_check` = `ok`; migrations `1,2,3,4,5`
with the name `sourced_memory_records` on 5 (the helper's `migration_lineage`
check compares the names and checksums recorded in `schema_migrations` with the
chain in the installed core tree); `installation.schema_version` = 5;
`global_revision` unchanged from the pre-migration value (195 at the time of
this runbook — replace the argument with the value recorded before the
migration); `memory_records` present and empty; `learned_procedures` row count
unchanged.

**Stop conditions for this phase:** any error from `initialize()`; a migration
name or checksum that is not in the candidate chain; `integrity_check` not
`ok`; a changed `global_revision`; a changed `learned_procedures` count. On any
of these, roll back with §7 and re-capture.

---

## 6. Phase 3 — runtime release activation

Only after Phase 2 is verified:

```bash
# confirm the drop-in path is still free
test ! -e /etc/systemd/system/minisa.service.d/20-dream-recovery.conf

sudo install -D -o root -g root -m 0644 \
  /home/sbeppi/minisa-working-assistant-20261018/candidate/deploy/minisa-learning-cycle-timeout.conf.candidate \
  /etc/systemd/system/minisa.service.d/20-dream-recovery.conf
sudo systemctl daemon-reload
sudo systemctl start minisa-core.service
sudo systemctl start minisa.service
```

The drop-in points `WorkingDirectory` at
`/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021` and
sets `TimeoutStopSec=50`. Production already carries
`/etc/systemd/system/minisa.service.d/dream-timeout.conf` (`TimeoutStopSec=90`);
the two files coexist and the more specific `20-*.conf` name is applied last.

**Post-deployment verification:**

```bash
systemctl show minisa.service minisa-core.service \
  -p ActiveState -p SubState -p WorkingDirectory -p TimeoutStopUSec --no-pager
curl -fsS http://100.108.164.57:8765/health
curl -fsS http://127.0.0.1:8765/ready        # core service
python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/verify_recovery_database.py \
  --db /srv/minisa/data/minisa.db \
  --core-tree /srv/minisa/app \
  --expect-schema-version 5 \
  --expect-migrations 1,2,3,4,5
```

Expected: `minisa.service` active with `WorkingDirectory` =
the candidate release; core service `ready` with `supported_schema_version: 5`
and `migration_compatible: true`; database schema 5; the helper reports
`VERDICT: PASSED`, including `migration_compatible` (database schema 5 equals
the schema version the installed core tree supports).

Do not send test chat prompts, do not invoke a dream, and do not reset the
recovery circuit. Let ordinary scheduling run.

---

## 7. Rollback

### 7.0 Two independent rollbacks, in a fixed order

| | Code rollback | Data rollback (L2) |
| --- | --- | --- |
| What is reverted | the runtime release pointer and the core-service code | the database file |
| Procedure | §7.2 | §7.3 |
| Requires | root, both services stopped | root, both services stopped, a verified capture |
| Effect on data | none | everything written after the migration is lost |

**The order is fixed: code first, data second.** A database restore performed
while the candidate core files are still installed is silently undone by the
core service itself: `core/app.py` calls `database.initialize()` in its startup
event, and `initialize()` applies every pending migration. Observed in the
2026-10-21 rehearsal — with the candidate `core/migrations.py` installed,
`Database.initialize()` on a restored schema-4 database re-applied migration 5
(`schema_version = 5`); `core/app.py` calls exactly that function at startup,
so starting `minisa-core.service` does the same. A restored database must
therefore never be handed to a core tree that supports schema 5.

### 7.1 Rollback ladder

| Level | Situation | Action | Data effect |
| --- | --- | --- | --- |
| L0 | runtime release fails to start, database is healthy at schema 5 | fix forward: restore the candidate runtime files/drop-in (the candidate runtime is the code that supports schema 5) | none |
| L1 | runtime regression after a successful activation, revert the release pointer | remove the drop-in and point `WorkingDirectory` back at the base release | **the base release carries its own schema-4 `core/`, so it refuses a schema-5 database** (`MinisaApplication.start()` requires `schema_version == SUPPORTED_SCHEMA_VERSION`, and the runtime imports that constant from the release tree's `core/migrations.py`). Reverting the pointer alone leaves the service down; continue with L1b or L2. |
| L1b | runtime regression, and the older runtime behaviour is wanted | build a **hybrid release**: the candidate `core/` (schema 5) plus the base `runtime/`. The runtime's schema gate reads `core.migrations.SUPPORTED_SCHEMA_VERSION` from the same tree, so the hybrid accepts the migrated database. The hybrid needs its own manifest and verification exactly like any release; it is not a drop-in change | none |
| L2 | the migrated database itself must go back | **code rollback first (§7.2), then data rollback (§7.3)**: remove the drop-in, restore the predecessor core files, and only then restore the verified pre-migration database from the capture | everything written after the migration is lost; the capture is the only preserved state |

L2 is the only path back to schema 4, and it is composed of two steps that must
not be swapped: §7.2 (code) and §7.3 (data).

### 7.2 Step 1 of L2 — code rollback (executed with both services down)

The release pointer *is* the drop-in: the unit's own `WorkingDirectory` is the
base release and `20-dream-recovery.conf` overrides it with the candidate
release. Removing the drop-in is therefore the whole pointer revert — there is
no symlink or pointer file to move.

```bash
# stop both services first; a restore over a running service tears the database
sudo systemctl stop minisa.service minisa-core.service

# L1: revert the release pointer to the base release
sudo rm -f /etc/systemd/system/minisa.service.d/20-dream-recovery.conf
sudo systemctl daemon-reload
systemctl show minisa.service -p WorkingDirectory --no-pager   # must be the base release
```

For **L2** (schema 4) the core-service code must be reverted as well, otherwise
the core service re-applies migration 5 to the restored database (§7.0):

```bash
# restore the predecessor core files taken in §5 step 0
ls -l /srv/minisa/app/core/db.py.pre-v5-<stamp> \
      /srv/minisa/app/core/migrations.py.pre-v5-<stamp>
sudo cp -a /srv/minisa/app/core/db.py.pre-v5-<stamp>        /srv/minisa/app/core/db.py
sudo cp -a /srv/minisa/app/core/migrations.py.pre-v5-<stamp> /srv/minisa/app/core/migrations.py

# the restored core tree must support schema 4 before anything is started
python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/verify_recovery_database.py \
  --core-tree /srv/minisa/app --core-only
```

Expected from the last command: `supports schema version 4`. If it still reports
5, the predecessor copies were not restored — stop and do not restore the
database.

For **L1b** (keep the migrated database, use the older runtime behaviour) the
candidate `core/` stays installed and a hybrid release is built and verified
separately. A hybrid that has not passed the preflight with its own manifest
must not be activated:

```bash
python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/preflight_learning_cycle_release.py \
  --manifest /path/to/hybrid/manifest.json --candidate-root /path/to/hybrid
```

### 7.3 Step 2 of L2 — data rollback to schema 4 (the only way back)

The pre-migration database is the only source of a schema-4 state. The
predecessor core copies are **not** a substitute: they are code, not data. The
live core code supports schema 4 and refuses a migrated database, and
`/srv/minisa/app/core.phase5-previous-67b9555fe2714951ac1ce24c2b443a43`
supports schema 2. Migration 5 has no down migration; there is no SQL to undo
it.

Preconditions, all mandatory:

- root access, both services stopped (§7.2);
- a **verified** capture directory (§4) that passed the independent recheck;
- the predecessor core copies `core/db.py.pre-v5-<stamp>` and
  `core/migrations.py.pre-v5-<stamp>` present;
- the base release directory intact.

Perform the steps in this order. No service may start before Step 5 passes.

**Step 1 — confirm the services are stopped and the candidate release inactive.**

```bash
systemctl show minisa.service minisa-core.service -p ActiveState -p SubState --no-pager
systemctl show minisa.service -p WorkingDirectory --no-pager   # base release, drop-in removed
```

**Step 2 — confirm the predecessor core-service code is in place.**

```bash
python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/verify_recovery_database.py \
  --core-tree /srv/minisa/app --core-only
```

Expected: `supports schema version 4`. This is the check that prevents the
re-migration described in §7.0.

**Step 3 — restore the verified pre-migration database from the capture.**

```bash
cd /mnt/minisa-recovery/minisa-recovery-<capture-id>
sha256sum -c SHA256SUMS
cat CAPTURE_VERIFIED
sudo rm -f /srv/minisa/data/minisa.db-wal /srv/minisa/data/minisa.db-shm
# owner and mode must match the live database (sbeppi:sbeppi, 0644)
sudo install -o sbeppi -g sbeppi -m 0644 sqlite/minisa.db /srv/minisa/data/minisa.db
ls -l /srv/minisa/data/minisa.db
```

The sidecars must be removed: a stale WAL next to a restored database would be
replayed into it. The capture's own copy of the database stays `0600` inside the
capture package; the installed file reproduces the production owner and mode.

**Step 4 — verify the restored database before starting anything.**

```bash
python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/verify_recovery_database.py \
  --db /srv/minisa/data/minisa.db \
  --core-tree /srv/minisa/app \
  --capture-dir /mnt/minisa-recovery/minisa-recovery-<capture-id> \
  --expect-schema-version 4 \
  --expect-migrations 1,2,3,4 \
  --expect-integrity ok \
  --expect-table learned_procedures \
  --expect-no-table memory_records
```

Expected: `VERDICT: PASSED`, including

- `capture_database_match` — the restored file is byte-identical to the captured
  image;
- `migration_compatible` — database schema 4 equals the schema version the
  installed core tree supports (4);
- `memory_records` absent — proof that migration 5 was undone, not re-applied.

**Step 5 — confirm that starting is safe.**

Both conditions must hold before the first `systemctl start`:

- the installed core tree supports schema 4 (Step 2, confirmed again by Step 4's
  `migration_compatible`); and
- the active runtime release accepts schema 4 — the base release does, because
  it carries its own schema-4 `core/` tree.

If either is untrue, stop: the services will not start, or they will re-apply
migration 5.

**Step 6 — start the core service, then the runtime service, and verify.**

```bash
sudo systemctl start minisa-core.service
curl -fsS http://127.0.0.1:8765/ready     # must show migration_compatible: true, supported_schema_version: 4
sudo systemctl start minisa.service
curl -fsS http://100.108.164.57:8765/health
systemctl show minisa.service minisa-core.service -p ActiveState -p SubState -p WorkingDirectory --no-pager
```

Expected: the core service is ready with `migration_compatible: true` and
`supported_schema_version: 4`; the runtime `/health` reports `status: ok`; and
`minisa.service` runs from the base release.

**Step 7 — record the evidence.**

Record the capture id, the verification output, the service states, the restored
`global_revision` and the time of every command in the incident record. The
rollback is not complete until it is written down.

### 7.4 Rollback limitations (explicit)

1. Migration 5 is one-way. Reverting requires the captured database.
2. The candidate runtime requires schema 5; the base release requires schema 4
   because it carries its own `core/` tree. At no point may the base release
   be pointed at a schema-5 database. L1b (candidate `core/` + base `runtime/`)
   is the only code-level path that runs against the migrated database with
   the older runtime behaviour.
3. Any turn, experience, failure record or dream result written between the
   migration and the rollback is lost by an L2 rollback.
4. The systemd journal and the remote inference host are outside the capture
   package and cannot be restored by it.
5. Rollback must be executed with both services stopped; restoring over a
   running service would produce a torn database.
6. Restoring the database while the candidate core files are still installed is
   not a rollback: the core service re-applies migration 5 at startup and the
   schema-4 state is lost again.

### 7.5 Rollback invariants

These hold for every rollback and are asserted by `tests/test_rollback_runbook.py`:

1. The release pointer is never left on the candidate release while the database
   is at schema 4: the candidate runtime release is never pointed at a restored
   schema-4 database.
2. The installed core-service code supports exactly the restored schema version
   before any service starts.
3. The restored database is byte-identical to the captured pre-migration image.
4. No service starts before the verification step passes.
5. A code rollback precedes a data rollback; the reverse order re-applies
   migration 5.
6. The predecessor core copies are code, not data: they never substitute for
   the capture.

---

## 8. Restoration procedure (disaster recovery)

Only from a verified capture. Never restore over production without a second
preserved copy of the current state.

```bash
# 1. stop both services
sudo systemctl stop minisa.service minisa-core.service

# 2. verify the capture again, independently
cd /mnt/minisa-recovery/minisa-recovery-<capture-id>
sha256sum -c SHA256SUMS
cat CAPTURE_VERIFIED

# 3. stage the file state (never directly over /)
sudo mkdir -p /var/tmp/minisa-restore
sudo tar --extract --file file-state.tar --directory /var/tmp/minisa-restore \
  --numeric-owner --same-owner --same-permissions --acls --xattrs --xattrs-include=* --selinux

# 4. move the staged tree into place
sudo rsync -aHAX --numeric-ids /var/tmp/minisa-restore/srv/minisa/ /srv/minisa/

# 5. replace the database (the archive deliberately excludes the raw database)
sudo rm -f /srv/minisa/data/minisa.db-wal /srv/minisa/data/minisa.db-shm
# owner and mode must match the live database (sbeppi:sbeppi, 0644)
sudo install -o sbeppi -g sbeppi -m 0644 sqlite/minisa.db /srv/minisa/data/minisa.db

# 6. verify before starting anything
python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/verify_recovery_database.py \
  --db /srv/minisa/data/minisa.db \
  --core-tree /srv/minisa/app \
  --capture-dir /mnt/minisa-recovery/minisa-recovery-<capture-id> \
  --expect-schema-version 4 \
  --expect-migrations 1,2,3,4 \
  --expect-integrity ok \
  --expect-table learned_procedures \
  --expect-no-table memory_records

# 7. start the services and verify (core service first, then the runtime)
sudo systemctl daemon-reload
sudo systemctl start minisa-core.service
curl -fsS http://127.0.0.1:8765/ready     # migration_compatible: true, supported_schema_version: 4
sudo systemctl start minisa.service
curl -fsS http://100.108.164.57:8765/health
```

Step 6 must report `VERDICT: PASSED` — integrity `ok`, pre-migration schema
version (4), `memory_records` absent, and `capture_database_match` proving the
restored file is byte-identical to the captured image — before step 7. Compare
the protected identity/CORE/personality/self-model files with the capture's
`file-manifest.json` hashes.

**Ordering note (same rule as §7.0).** Step 4 restores the archived
`/srv/minisa/app` tree, which contains the pre-migration `core/` code, and step
5 replaces the database afterwards: the code is therefore in place before the
schema-4 data is installed. The reverse order is not safe — a candidate core
tree re-applies migration 5 to a restored schema-4 database at startup. Step 6
re-checks that the restored core tree supports schema 4 before any service
starts, and `sudo systemctl daemon-reload` in step 7 reloads the unit
definitions restored with the archive.

---

## 9. Explicit stop conditions

Stop and escalate — do not improvise — if any of these is true:

1. No genuinely independent destination is attached and verified. A same-device
   path is not a backup.
2. The capture does not report a verified result, or any `SHA256SUMS` entry
   fails, or `CAPTURE_INCOMPLETE` is present.
3. The captured database fails `PRAGMA integrity_check`, or its schema version
   is not 4, or the restore rehearsal did not pass.
4. The preflight reports any blocking check, including a check that merely
   *could not be verified* (missing file, unreadable unit file, unreadable
   configuration).
5. The candidate manifest digest is not `410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405`,
   or any tracked file does not match the manifest.
6. The migration chain does not continue the production lineage: a name or
   checksum in `schema_migrations` differs from the candidate chain.
7. The migration produces an error, a non-`ok` integrity check, a changed
   `global_revision`, or a changed `learned_procedures` row count.
8. The core service does not become ready with `migration_compatible: true`
   at schema 5.
9. The runtime service does not become active on the candidate release, or
   `/health` is not OK.
10. Any service state was `failed` or transitioning at the start of the
    window; the capture utility refuses to run in that case.
11. The host was SIGKILLed or lost power during the window. An in-process
    script cannot handle that; inspect both unit states and the capture
    directory before resuming.
12. The administrator's authentication cannot be completed. Never disable
    authentication or work around it.
13. An L2 rollback was started without the code rollback (§7.2) having been
    completed first, or without the verification in §7.3 Step 4 having passed.
    The core service would re-apply migration 5 to the restored database.

---

## 10. Operator sign-off checklist

| # | Item | Check | Sign-off |
| --- | --- | --- | --- |
| 1 | Independent destination attached, ancestry verified | ☐ | |
| 2 | Root preflight of the capture utility passed with capacity figures | ☐ | |
| 3 | Preflight script (`ops/preflight_learning_cycle_release.py`) exits 0 | ☐ | |
| 4 | Candidate manifest digest matches the value in §9.5 | ☐ | |
| 5 | Verified capture exists; `SHA256SUMS` verified independently | ☐ | |
| 6 | Captured database: integrity `ok`, schema 4, rehearsal passed | ☐ | |
| 7 | Predecessor core files copied to `*.pre-v5-<stamp>` | ☐ | |
| 8 | Migration 5 applied; integrity `ok`; schema 5; revision unchanged | ☐ | |
| 9 | Core service ready, `migration_compatible: true` | ☐ | |
| 10 | Drop-in installed; runtime active on the candidate release | ☐ | |
| 11 | `/health` OK; no dream triggered; recovery circuit untouched | ☐ | |
| 12 | Capture directory preserved and its digest recorded | ☐ | |
| 13 | L2 rollback path (§7.2 code rollback, then §7.3 data rollback) rehearsed on a disposable copy, in that order | ☐ | |

---

## 11. Evidence recorded on 2026-10-21 (read-only)

- `findmnt` / `lsblk`: single physical device `nvme0n1`; `/`, `/home`, `/var`
  on `fedora-root`; `/srv`, `/usr/local/lib/ollama` on `fedora-server`; both
  LVs on `nvme0n1p3`. `/mnt`, `/media`, `/run/media` empty; `fstab` has no
  external device; no backup timer; `sunrpc` is a kernel pipe, not storage.
- Live database opened read-only: `integrity_check ok`, schema 4, 17 tables,
  no `memory_records`, `learned_procedures` 0 rows, `global_revision` 195,
  size 692224 bytes, mtime 2026-10-19 07:22 UTC.
- `schema_migrations` rows 1–4 with checksums `4d5491e0…`, `b08db440…`,
  `b45c9a57…`, `3c28dcc8…`; the candidate chain reproduces all four and adds
  `a06db7fb…` for migration 5.
- `systemctl show`: `minisa.service` active, `WorkingDirectory`
  `/home/sbeppi/minisa-releases/response-context-20261017T010152Z`,
  `TimeoutStopUSec=1min 30s`, drop-ins `10-timeout-abort.conf` and
  `dream-timeout.conf`; `minisa-core.service` active with `WorkingDirectory`
  `/srv/minisa/app`. `/etc/systemd/system/minisa.service` is not readable by
  the service account.
- Config: `/srv/minisa/config/minisa.toml` has no `dream_timeout_seconds`
  and `minisa.env` has no `MINISA_DREAM_TIMEOUT_SECONDS`, so the candidate's
  40-second default applies; the manifest records the same value.
- Live core code hashes: `core/db.py` `479bb191…`, `core/migrations.py`
  `425aaa16…`; candidate: `499c22a7…`, `0babeb66…`. The predecessor copy
  `/srv/minisa/app/core.phase5-previous-67b9555fe2714951ac1ce24c2b443a43`
  supports schema 2.
- Existing recovery material re-verified: the 2026-10-18 database-only capture
  matches its `SHA256SUMS` and passes `integrity_check` (schema 4, revision
  42); the five `/srv/minisa/backups/stage1-*` copies match their recorded
  SHA-256 values and pass `integrity_check`, but are schema 1–2 with
  `restore_test_status` NULL. All of them live on `nvme0n1`.
- Candidate test suite in the candidate workspace: 210 tests, `OK
  (skipped=6)`, also green with `PYTHONWARNINGS=error::ResourceWarning`
  (188 tests before the rollback-runbook work; 22 added by
  `tests/test_rollback_runbook.py`).
- Rollback procedure re-checked on 2026-10-21 against the rehearsal capture
  (disposable copies only): the restored pre-migration image verifies
  (`VERDICT: PASSED`, `capture_database_match`, `migration_compatible` 4 == 4);
  the predecessor core tree leaves it at schema 4 and reports
  `migration_compatible: true` on `/ready`; the candidate core tree re-applies
  migration 5 to the same image (`schema_version` 4 → 5, `memory_records`
  created).
- Preflight run on 2026-10-21: `preflight: blocked` with
  `blocking_checks = [service_configuration, independent_destination,
  recovery_point, tests]` (the last only because the run used `--skip-tests`).
