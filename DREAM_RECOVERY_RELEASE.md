# Dream recovery candidate and recovery procedure

This directory is a candidate. It has not been deployed and must not be pointed
at the production workspace for qualification.

## Candidate change

The prior 20 second value was applied both to `urlopen()` and to the parent
process wall-clock deadline. The wall deadline included request transport,
server queueing/inference, response transfer, and the child’s safe envelope
parsing. The persisted historical failure records contain only a category and
total worker latency, so they cannot distinguish a model-server timeout from
the parent killing the child. The repeated 20.04–20.06 second durations are
consistent with the configured deadline being reached. Successful model calls
also recorded up to 9.75 seconds of model load, 3.92 seconds of prompt
evaluation, and 40 generated tokens; at the measured generation rate, 512
tokens plus that cold overhead can exceed 20 seconds. This makes an undersized
deadline a supported hypothesis, not a proven stage for any old failure.

The candidate uses a configurable 40 second request deadline (20–60 second
bounded range), with a 2 second parent teardown margin. The deadline remains
bounded and includes child startup and all transport/model/response handling.
Failures now retain only safe category, stage, and subtype metadata. Raw model
responses and provider error text do not cross the child-process boundary.
The invalid-output path differentiates malformed provider JSON/envelope,
malformed model JSON, and wire/schema rejection. Dream proposal validation,
identity protections, transaction atomicity, and the existing recovery circuit
are unchanged. A failed terminal job is still replaced by normal scheduling;
no circuit reset or unbounded retry was added.

Because worker shutdown may wait through the bounded request and teardown, the
candidate systemd drop-in raises `TimeoutStopSec` to 50 seconds. This is a
candidate deployment requirement, not installed on production.

The existing qualification provenance filter remains in place: public chat
assigns `user`, the explicitly enabled loopback-only qualification route
assigns `qualification`, and only `user` turns are admitted to workspace
evidence. Client JSON cannot choose this provenance. The qualification route
is disabled by default and the production config does not enable it. This also
prevents rollback to old code from re-indexing new qualification turns.
Historical arithmetic evidence is unchanged.

## Recovery material and consistency

Canonical production data is under `/srv/minisa`: `data/minisa.db` (SQLite WAL),
`CORE`, `PERSONALITY`, `SELF_MODEL`, `STATE`, `EXPERIENCE`, `MEMORY`,
`REFLECTION`, and `USER`; config is under `config` (including the protected
environment file), and service code is in the configured release directory.
The systemd unit and systemd journal are outside this tree; the Minisa application
logs and historical deploy artifacts under `/srv/minisa/logs` and `/srv/minisa/backups`
are inside it. The database currently uses
WAL and has live `-wal`/`-shm` sidecars. Copying only the main database file is
not a valid backup. The existing `core.backup.create_backup()` uses SQLite’s
online backup API and validates a copy, but also writes a backup registry row to
its source database; do not use it while production must remain read-only.

A verifiable full recovery point must capture the SQLite database through the
online backup API and capture all file-backed state, config, and the exact
release/unit metadata at one coordinated point. `/srv` is on XFS without an
available snapshot facility. The supported procedure therefore requires a
controlled service stop, acquisition of the Minisa writer lock, copying the
canonical file trees/config/release metadata, and use of an independent
SQLite-online-backup utility that does not register itself in the source DB.
Then run SQLite integrity/schema checks, hash/manifest verification, and a
restore test in a disposable directory. Do not restart onto a candidate until
the restored backup passes. No full live production backup was created or
verified in this investigation because the service must remain live and
unmodified.

### Database-only production recovery component created 2026-10-18

A live SQLite online backup was created without invoking `core.backup` and
without writing to the production database. It is stored at
`/home/sbeppi/minisa-recovery-backups/dream-recovery-db-20261018T123156Z`,
outside the candidate tree and on the root XFS logical volume (separate from
`/srv`, though not a separate host or verified physical failure domain). The
source was WAL mode; `Connection.backup()` captured a consistent SQLite
snapshot including committed WAL state. The copy passed `integrity_check`,
schema verification (version 4), manifest/hash verification, and an isolated
restore rehearsal. Rehearsal used a disposable database destination and did
not restore over production. The captured database revision was 42.

This artifact is a recoverable **database component only**. It does not include
the file-backed identity/CORE/personality/self-model/experience/memory/state
trees, production config, service units, active release, or journald records.
It is not the consistent full-system recovery point required before deploying
candidate code.

## Candidate deployment inventory and rollback plan

The staged release at
`/home/sbeppi/minisa-releases/minisa-dream-recovery-candidate-20261018` starts
from the exact active release and changes only ten runtime source files:

- `runtime/cognition/dream.py`, `dream_request.py`, and `dream_wire.py`:
  bounded 40-second default request deadline, transport-stage and safe output
  failure classification, and matching worker shutdown allowance.
- `runtime/server/config.py` and `application.py`: bounded 20–60 second
  `dream_timeout_seconds` configuration and application wiring.
- `runtime/cognition/turns.py`, `engine.py`, and `workspace.py`, plus
  `runtime/server/http.py`: server-assigned user/qualification provenance,
  loopback-only qualification path, and exclusion of qualification records
  from workspace evidence. `http.py` also exposes the safe read-only dream
  status route.
- `core/db.py`: closes SQLite connections when their context manager exits,
  while retaining normal commit/rollback behavior. This fixes the test and
  runtime `ResourceWarning`s from unclosed read connections; it does not change
  schema or migration behavior.

The full working candidate also contains unrelated memory/note response edits
in `engine.py`; those edits are deliberately excluded from the staged release.
The staged `engine.py` carries only provenance plumbing and no note-response
changes. No CORE workspace content, personality, self-model, identity, or
schema migration code is changed.

The live database is schema version 4 and the candidate supports version 4;
there is no candidate migration. The current TOML has no timeout override, and
the service environment file has no `MINISA_DREAM_TIMEOUT_SECONDS` override,
so candidate configuration resolves to its bounded 40-second default. The
candidate drop-in is `deploy/minisa-dream-timeout.conf.candidate`; it points
the service `WorkingDirectory` at the staged release and sets
`TimeoutStopSec=50`. Production currently has no Minisa-specific drop-in and
the effective stop timeout is 30 seconds. The active service remains user/group `sbeppi`, uses
`/srv/minisa/.venv/bin/python3`, and reads the existing config/environment file.
The staged file hashes are recorded in
`/home/sbeppi/minisa-releases/minisa-dream-recovery-candidate-20261018.manifest.json`.
The verified manifest SHA-256 is
`e151abc79aa3b83c1238682c2e38f2ed30bd403409f3bbff2a3d1d5ea75a7746` and the
candidate drop-in SHA-256 is
`c9505722ea892ab88663996c26e67073d4ab3b2c3df54ef7acf4bb4d450462b1`.
The production TOML currently points dream inference at
`http://100.123.208.117:11434` with model `qwen3:8b-q4_K_M`; a read-only model
inventory request confirmed that model is listed by the configured server at
inspection time. That inference server and its model storage are external
dependencies and are not part of the Minisa file backup. The candidate capture
protects Minisa state/configuration/release on this Fedora host; it cannot
restore the remote inference host.

### Planned deployment, only after a full backup and plan review

1. Attach and mount a destination on a physically independent device at
   `/mnt/minisa-recovery`. Run the capture utility in preflight mode:

   ```bash
   python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/capture_minisa_recovery.py \
     /mnt/minisa-recovery
   ```

   It must report `preflight: passed` and a different physical-device ancestry.
   During the administrator-authenticated maintenance window run:

   ```bash
   sudo python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/capture_minisa_recovery.py \
     /mnt/minisa-recovery --execute-maintenance
   ```

   `sudo` will require the administrator's password. The script stops both
   `minisa.service` and `minisa-core.service`, captures `/srv/minisa`, the active
   release, unit files and drop-ins, uses SQLite online backup for the WAL DB,
   restores the archive and DB in a private disposable directory, verifies
   hashes/schema/integrity/metadata, and restores each service's original
   active/inactive state before reporting success. If using a remote mount,
   require an administrator to verify its independent storage and add
   `--confirm-independent-target` to both commands.
2. In the resulting `minisa-recovery-*` directory, require the script's
   `capture=verified` result, `restore_rehearsal.database_integrity_check` of
   `ok`, matching schema version, all per-file hashes verified, and successful
   service-state restoration. Independently run `sha256sum -c SHA256SUMS` from
   that directory. Preserve this backup and the existing active release.
3. Recheck the staged manifest against all ten staged files and confirm that
   `/etc/systemd/system/minisa.service.d/20-dream-recovery.conf` does not
   already exist:

   ```bash
   python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/verify_candidate_manifest.py \
     /home/sbeppi/minisa-releases/minisa-dream-recovery-candidate-20261018.manifest.json
   ```

   Require `verified: true`, `runtime_file_count: 10`, the manifest digest
   recorded with the deployment notes, and `production_deployed: false`. Then
   install and activate the candidate:

   ```bash
   sudo install -D -o root -g root -m 0644 \
     /home/sbeppi/minisa-working-assistant-20261018/candidate/deploy/minisa-dream-timeout.conf.candidate \
     /etc/systemd/system/minisa.service.d/20-dream-recovery.conf
   sudo systemctl daemon-reload
   sudo systemctl restart minisa.service
   ```

   `minisa-core.service` and schema are unchanged. Confirm service properties,
   `/health`, `/v1/dream-status`, effective 40-second timeout and schema 4. Do
   not send a prompt or force a dream for deployment verification. The current
   service address is `http://100.108.164.57:8765`; read-only checks are:

   ```bash
   curl --fail --silent --show-error http://100.108.164.57:8765/health
   curl --fail --silent --show-error http://100.108.164.57:8765/v1/dream-status
   ```
4. Preserve all historical rows and wait for the existing recovery gate to
   expire. Allow the normal worker to schedule from the six pending evidence
   rows only when its regular cooldown, rate, pressure/idle, and recovery
   conditions are met. Do not enable or call the qualification route in
   production; enabled mode is for isolated test servers only.

### Rollback

If startup/health validation fails, remove only the candidate drop-in and start
the unchanged original release. The original unit's `WorkingDirectory` remains
`/home/sbeppi/minisa-releases/response-context-20261017T010152Z` because the
candidate changes it only through that drop-in:

```bash
sudo systemctl stop minisa.service
sudo rm /etc/systemd/system/minisa.service.d/20-dream-recovery.conf
sudo systemctl daemon-reload
sudo systemctl start minisa.service
```

Do not restore or replace the database for this code-only rollback; that keeps
all turns, experiences, failed jobs and any successful results created after
deployment. The previous active release is never overwritten. A full data
restore is a separate disaster-recovery action: preserve the current state to
another verified destination first, stop both services, and restore only after
confirming the backup, destination paths and target revision.

### Current access blocker

The account can read systemd properties but cannot read
`/etc/systemd/system/minisa.service` or `minisa-core.service`; `systemctl cat`
and direct reads return permission denied, and noninteractive sudo reports a
password is required. The running services are active, and the full file trees
cannot be captured consistently against concurrent writers without a planned
quiescence/snapshot. No separate physical backup device is mounted. Therefore
the full backup/deploy gate is not met. Do not deploy or restart production
until privileged maintenance access, a coordinated capture, and a verified
restore rehearsal are available.

### Prepared maintenance capture utility (not run against production)

`ops/capture_minisa_recovery.py` performs a preflight by default and refuses
same-device destinations. For a block device it compares the physical device
ancestry of the destination with `/` and `/srv`; a remote filesystem requires
explicit administrator confirmation after the remote target is checked.
Execution requires root and `--execute-maintenance`. It records both service
states, stops active services (and any unexpected activation during capture),
takes an SQLite online backup from a read-only connection, archives `/srv/minisa`, the
configured active release, and both systemd units/drop-in directories, and
then restores the archive and database to a private disposable subdirectory on
the backup target. GNU tar preserves numeric ownership, ACLs, xattrs, and
SELinux labels. It acquires both existing Minisa filesystem locks without
creating or truncating lock files; failure to acquire either aborts capture.
Database sidecars are excluded from the archive because the SQLite backup API
captures committed WAL contents consistently. The systemd journal and remote
inference host are outside this Minisa recovery package; `/srv/minisa/logs` and
all Minisa-owned historical backups are included. SHA-256 hashes
cover the archive, database image, manifest, and every archived regular file.
The script restores each service's original active/inactive state after either
success or failure. It does not change candidate deployment, recovery-circuit,
or historical record state.

The script has only been exercised against disposable test trees. No mounted
independent target is available here, so it has not been run with production
paths. Ordinary exceptions and SIGINT/SIGTERM/SIGHUP run service restoration;
SIGKILL or host power loss cannot be trapped by an in-process script. After
such an interruption, the administrator must inspect both unit states before
resuming maintenance. An incomplete output directory is retained for audit
and is not a backup unless the script reports `capture: verified` and all
checksums pass. The currently configured dream model is hosted remotely and
is not part of this Minisa backup. The regression test is
`tests/test_recovery_capture.py`.

## Verification sequence before any deployment

1. Keep the production candidate separate and run `python -m unittest discover
   -s tests -v` in an isolated checkout.
2. During an approved maintenance window, create and independently restore-test
   the coordinated full backup described above.
3. Install the candidate release and drop-in only after the backup verifies.
4. Verify the service is healthy and observe ordinary scheduling; do not force a
   dream or clear the recovery circuit.
5. Confirm a new `dream_results` row, source/result revisions, evidence count,
   model usage metadata, and matching `dream.committed` audit event. Confirm
   unresolved evidence decreases only by records actually processed. Confirm
   protected identity/CORE/personality/self-model files remain unchanged.
6. Read the new per-failure safe `stage`/`kind` fields if a later failure occurs.

Do not delete or rewrite historical evidence, jobs, or failures to alter health
reporting. Keep the previous production release and verified recovery point
available for rollback.
