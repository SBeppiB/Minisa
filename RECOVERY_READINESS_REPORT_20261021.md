# Recovery readiness report — 2026-10-21

Scope: advance the schema-5 learning-cycle candidate towards a safe deployment
without interactive input, without touching production, and without weakening
any gate. Nothing was deployed, no service was stopped or restarted, no
production file, configuration or database row was changed.

---

## 1. Work completed

| Artifact | Path | What it is |
| --- | --- | --- |
| Recovery runbook | `RECOVERY_RUNBOOK.md` | The operator procedure: gates and their current state, the independent-destination requirement, the pre-migration backup with its verification requirements, the migration step, runtime activation, the restoration procedure, the rollback ladder, twelve explicit stop conditions, a sign-off checklist, and the 2026-10-21 evidence log. |
| Deployment preflight | `ops/preflight_learning_cycle_release.py` | Read-only, fail-closed preflight over ten prerequisites: manifest, staged-release content, migration lineage against the live database, live database health, core-service copy step, effective service configuration, independent destination, verified recovery point, rollback prerequisites, test suite. Exit `0` = all verified, `2` = a required prerequisite failed *or could not be verified*, `3` = the preflight could not run. |
| Preflight tests | `tests/test_deployment_preflight.py` | 38 tests on disposable fixtures: malformed manifests, tampered release files, checksum divergence, missing migrations, corrupt databases, incomplete captures, marker/digest mismatches, non-independent destinations, an existing drop-in, an unreadable unit file, nested test runs, and a proof that the preflight writes nothing outside the report file it is given. |
| Semantic-recall proposal | `SEMANTIC_RECALL_EXPERIMENT.md` | A separate, measurable experiment with a control, hypotheses, thresholds, prerequisites, method, risks and a rejection criterion. No experimental retrieval code was written and no embedding dependency was introduced. |
| Updated status docs | `MINISA_PROJECT_STATUS.md`, `MINISA_FIX_LIST.md`, `LEARNING_RELEASE_CANDIDATE_20261021.md` | Record the recovery-readiness outcome, the concrete P0 evidence, and the new artifacts. |

The staged release was **not** touched. `ops/verify_candidate_manifest.py`
still returns `verified: true`, 20 runtime files, manifest SHA-256
`410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405`,
`production_deployed: false`, and rebuilding the manifest from the tree
reproduces it byte for byte.

## 2. Tests and their actual results

| Command | Result |
| --- | --- |
| `PYTHONWARNINGS=error::ResourceWarning /srv/minisa/.venv/bin/python3 -m unittest discover -s tests` | `Ran 188 tests ... OK (skipped=6)` |
| `/usr/bin/python3 -m unittest discover -s tests` | `Ran 188 tests ... OK (skipped=6)` |
| `/srv/minisa/.venv/bin/python3 -m unittest tests.test_deployment_preflight` | `Ran 38 tests ... OK` |
| `/srv/minisa/.venv/bin/python3 -m compileall -q core runtime ops tests` | clean (exit 0) |
| AST unused-import check on the two new Python files | no unused imports |
| `ops/verify_candidate_manifest.py …20261021.manifest.json` | `verified: true`, 20 runtime files, `production_deployed: false` |
| `ops/build_candidate_manifest.py` (output to a temporary file) | rebuild is byte-identical to the deployed manifest |

The 6 skipped tests are the opt-in live-model tests, which need
`MINISA_LIVE_DREAM_HOST`/`MINISA_LIVE_DREAM_MODEL` and a reachable model
server. No linter (ruff, flake8, pyflakes, pylint, mypy, black) is installed on
this host and none was installed, so the static checks are the syntax check and
the AST check above.

Final preflight run (with the test suite enabled):

```
preflight: blocked
blocking: ['service_configuration', 'independent_destination', 'recovery_point']
  passed   manifest                20 changed files, supported schema 5, production_deployed=false
  passed   release_content         20 tracked files match the manifest
  passed   migration_lineage       production migrations 1-4 match the candidate chain; candidate adds migration [5]
  passed   live_database           schema 4, revision 195, integrity ok, opened read-only
  passed   core_service_files      live core code differs from the candidate as expected before the copy step
  failed   service_configuration   unit file not readable by this account; rerun as an administrator
  failed   independent_destination no mounted filesystem is independent of the production block devices
  failed   recovery_point          no recovery capture directory was supplied
  passed   rollback_prerequisites  base release intact, live core hashes recorded
  passed   tests                   OK (skipped=6)
```

`blocked` is the correct result for this host.

## 3. Recovery destination status: none exists (evidence)

The question was asked and answered with read-only inspection of the live host:

- `lsblk` shows exactly one physical disk: `nvme0n1` (WD SN740
  SDDQNQD-256G-1014, 238.5 G) plus `zram0` (swap). There is no second disk,
  no USB device and no removable media.
- Block-device ancestry: `/` → `fedora-root` → `nvme0n1p3` → `nvme0n1`;
  `/srv` → `fedora-server` → `nvme0n1p3` → `nvme0n1`. `/home` and `/var` are on
  `fedora-root`; `/usr/local/lib/ollama` is a bind of `/srv`; `/boot` and
  `/boot/efi` are on `nvme0n1p2`/`nvme0n1p1`. Everything is one device.
- `findmnt` shows no NFS, CIFS, SMB or other remote filesystem. The only
  non-local entry is `sunrpc` (`rpc_pipefs`), a kernel pipe, not storage.
- `/mnt`, `/media` and `/run/media` are empty; `/etc/fstab` declares only the
  local partitions and the Ollama bind mount.
- `systemctl list-timers` contains no backup or capture timer; the only Minisa
  units are `minisa.service` and `minisa-core.service`.

Existing recovery material, all on `nvme0n1`, re-verified today:

| Artifact | Result | Sufficiency |
| --- | --- | --- |
| `/home/sbeppi/minisa-recovery-backups/dream-recovery-db-20261018T123156Z` | `SHA256SUMS` match, `integrity_check ok`, schema 4, revision 42 | database component only — no file trees, config, units or release; lost with the device |
| 5 registry backups in `/srv/minisa/backups/stage1-*` | recorded SHA-256 all match, `integrity_check ok` | schema 1–2 (September), `restore_test_status` NULL; useless against today's data |
| `/srv/minisa/backups/{20261003T010357Z-r4b,deploy-*,pre-stage1-*}` | not re-verified in detail | same device |

Conclusion: there is **no** genuinely independent recovery destination on this
host, and no existing artifact qualifies as a full-system recovery point. The
`ops/capture_minisa_recovery.py` utility would refuse every local path here
(it compares physical device ancestry) — its refusal is correct, not a bug.

## 4. Remaining blockers (each needs a person, not more code)

| ID | Blocker | Needs |
| --- | --- | --- |
| B1 | No independent recovery destination | physical device or a remote filesystem whose failure domain the administrator verifies, mounted at `/mnt/minisa-recovery` |
| B2 | No administrator-authenticated maintenance window | `sudo` for the capture utility and for reading `/etc/systemd/system/minisa.service` |
| B3 | No verified full recovery point | follows from B1+B2: capture, `capture=verified`, matching checksums, integrity and schema checks, restore rehearsal |
| B4 | Deployment is a production change | explicit approval to copy the two core files, apply migration 5, install the drop-in and restart the two services |
| B5 | Semantic-recall experiment cannot be measured | an approved, reachable embedding-capable endpoint: the local Ollama serves only `qwen3.5:2b-q4_K_M` (capabilities: completion, vision, tools, thinking — no embeddings), and the configured remote `100.123.208.117:11434` does not answer from this host (curl timeout, ICMP unreachable) |

Migration 5 is one-way, so B3 is a hard prerequisite rather than good practice:
the pre-migration database is part of the rollback plan.

## 5. Exact next action when the administrator returns

1. Attach the independent device and mount it at `/mnt/minisa-recovery`.
   Verify independence: `findmnt -o TARGET,SOURCE,FSTYPE` and
   `lsblk -srno NAME "$(findmnt -n -o SOURCE -T /)"` — the destination ancestry
   must not intersect `nvme0n1`.
2. As the administrator, run the read-only preflights and keep their output:

   ```bash
   sudo python3 .../ops/capture_minisa_recovery.py /mnt/minisa-recovery
   sudo python3 .../ops/preflight_learning_cycle_release.py
   ```

   The preflight must report `preflight: passed`. If it still reports
   `service_configuration` as blocking, the unit file was still unreadable —
   rerun it with the administrator's own authenticated session.
3. In the approved window, capture and verify the recovery point
   (`RECOVERY_RUNBOOK.md` §4), then follow §5 (migration) and §6 (activation)
   in order, checking each verification line before continuing.
4. Do not skip the stop conditions in `RECOVERY_RUNBOOK.md` §9.

## 6. Production integrity

| Check | Result |
| --- | --- |
| `/srv/minisa/data/minisa.db` | mtime `2026-10-19 07:22:31`, size 692224 — unchanged |
| schema version / global revision / applied migrations | `4` / `195` / `[1,2,3,4]` — unchanged |
| `minisa.service` | active, PID 1324, `WorkingDirectory` still the base release — not restarted |
| `minisa-core.service` | active, PID 1007 — not restarted |
| `/srv/minisa/app/core/{db.py,migrations.py}` | unchanged (`479bb191…`, `425aaa16…`) |
| systemd drop-in directory | unchanged; `20-dream-recovery.conf` still absent |
| The `-wal`/`-shm` mtimes | these move because the running service keeps writing to the WAL; every inspection in this work opened the database `mode=ro` |

## 7. Known cosmetic item

The staged release contains 54 `.pyc` files in 10 `__pycache__` directories,
left behind by the staged-artifact smoke test that ran against the release
tree. They are not part of the manifest, they do not affect the tracked
content, and the preflight reports them as untracked bytecode rather than
silently ignoring them. They were deliberately **not** deleted, because the
staged release was to stay byte-identical to how it was found. The operator may
remove them before deployment with
`find /home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021 -name __pycache__ -type d -prune -exec rm -rf {} +`,
which changes no tracked file and does not change the manifest digest.
