# Minisa fix list

Status verified 2026-10-18. Items are grouped by release priority. “Candidate”
means implemented and tested in the isolated candidate; it does not mean
available in production.

## P0 — protect the existing Minisa history

- [ ] Attach and verify a physically independent recovery destination. `/`,
  `/home`, and `/srv` currently share one NVMe ancestry; the existing database
  snapshot on that device is not a full recovery point. Re-verified 2026-10-21:
  the host has exactly one physical disk (`nvme0n1`); `/`, `/home`, `/var` are on
  `fedora-root` and `/srv` plus `/usr/local/lib/ollama` are on `fedora-server`,
  both LVs on `nvme0n1p3`. `/mnt`, `/media` and `/run/media` are empty, `fstab`
  declares no external device, no backup or capture timer exists, and the only
  non-local mount (`sunrpc`) is a kernel pipe, not storage. No independent
  destination exists yet.
- [ ] Arrange an administrator-authenticated maintenance window. The capture
  utility needs root privileges to coordinate `minisa.service` and
  `minisa-core.service`, inspect/copy protected service configuration, and
  capture the live file-backed state consistently. The account used for
  candidate work cannot read `/etc/systemd/system/minisa.service`, so the
  deployment preflight fails closed on `service_configuration` until it is run
  with administrative privileges.
- [ ] Run `ops/capture_minisa_recovery.py` in preflight, then maintenance mode
  only after confirming the target is independent. Require
  `capture=verified`, matching checksums, SQLite integrity/schema checks, and a
  successful restore rehearsal to a disposable location. Never restore the
  rehearsal over production.
- [ ] Preserve failed jobs, the six unresolved experiences, historical
  arithmetic qualification evidence, dream journals, and protected identity,
  CORE, personality, and self-model files. No cleanup or manual success
  marking is part of recovery.
- [x] Candidate: the deployment procedure, its gates and its rollback ladder
  are written down and machine-checked. `RECOVERY_RUNBOOK.md` holds the
  operator procedure, `ops/preflight_learning_cycle_release.py` verifies the
  manifest, migration lineage, live database, service configuration, an
  independent destination, the recovery point, rollback prerequisites and the
  test suite, and fails closed when any of them cannot be verified. It is
  read-only: it never stops a service and never opens the production database
  for writing.

## P1 — resume ordinary dream processing

- [x] Candidate: bound and configure the model request deadline; classify
  failures with safe stage/category metadata; retain validation, atomic
  acceptance, and the existing recovery circuit.
- [x] Candidate: server-assigned user/qualification provenance so qualification
  traffic is excluded from future genuine workspace evidence.
- [x] Candidate: safe read-only `/v1/dream-status` route and SQLite connection
  context closure, with focused tests.
- [x] Candidate: first verified learning cycle (dream -> guarded admission ->
  durable sourced memory -> retrieval by a later conversation), with a
  deterministic suite and an opt-in live-model run. See
  `LEARNING_CYCLE_MILESTONE.md`.
- [x] Candidate: the migration chain was realigned with the production
  lineage. The candidate chain had been a divergent rewrite, so
  `verify_migrations()` failed against the production database and
  `apply_migrations()` could half-apply. v1-v4 are now byte-identical to the
  production lineage (checksums match the live `schema_migrations` rows) and
  v5 `sourced_memory_records` is additive. Verified against a disposable copy
  of the live database; the live database was not modified.
- [x] Candidate: conversation-level acceptance test (turn -> evidence ->
  guarded admission -> later conversation answer) and a retrieval-quality
  measurement with a pinned floor. See `LEARNING_RELEASE_CANDIDATE_20261021.md`.
- [x] Candidate manifest verified at 10 runtime files,
  SHA-256 `e151abc79aa3b83c1238682c2e38f2ed30bd403409f3bbff2a3d1d5ea75a7746`,
  `production_deployed: false`.
- [x] Candidate release snapshot 2026-10-21 verified at 20 runtime files,
  manifest SHA-256
  `410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405`,
  `migration_required: true`, `production_deployed: false`. It supersedes the
  2026-10-18 snapshot for the learning-cycle work.
- [x] Candidate tests re-run 2026-10-21: full suite passed 150 tests (6
  skipped) with `PYTHONWARNINGS=error::ResourceWarning`, on both the system
  interpreter and `/srv/minisa/.venv/bin/python3`, and the same suite passed
  against the staged release artifact itself.
- [ ] Only after P0 is complete: verify the manifest and install the candidate
  systemd drop-in. Do not overwrite the original release. For the 2026-10-21
  snapshot the order is: recovery point first, then the core service copies
  `core/db.py` and `core/migrations.py` and applies migration 5, then the
  runtime release is activated. Migration 5 has no down migration, so the
  pre-capture database is part of the rollback plan.

- [ ] Confirm service health and `/v1/dream-status` without sending a chat
  prompt. Let the unchanged normal scheduler and recovery circuit operate.
- [ ] Verify a new durable successful result: timestamp, source/result
  revisions, evidence count, and corresponding audit/result metadata. Compare
  protected file hashes against the verified recovery point. Until these
  checks pass, do not call dreaming recovered.
- [ ] If a failure recurs, use only safe stage/category diagnostics and the
  durable job record. Do not clear the circuit or manufacture success.

## P2 — production observability

- [ ] Deploy only after P0 and the reviewed P1 release. Then confirm
  `/v1/dream-status` returns a safe summary; production returned 404 on the
  2026-10-18 read-only check.
- [ ] Keep status reporting limited to counts, timestamps, safe categories,
  scheduler/recovery state, and revision/evidence summaries. Do not expose
  conversation content, memory bodies, credentials, or model private reasoning.

## P3 — research and everyday use

- [x] Separate candidate: bounded public web search/fetch and attributed,
  deduplicated notes in existing `MEMORY`, with Tavily/SearXNG providers and
  SSRF checks.
- [ ] Research feature remains candidate-only. Decide how it should be
  exposed to Minisa's normal chat/GUI, review authorization for note creation,
  and add only a separate, focused integration. The current CLI requires a
  caller-supplied summary.
- [ ] Keep Tavily use on the free plan with paid overages disabled, or use a
  trusted configured SearXNG endpoint with JSON enabled. No required spend or
  new search server is part of the candidate.
- [ ] Re-test GUI launch and normal conversation on the intended desktop. The
  server shell does not establish that the graphical application opens or
  that live conversation is reliable.
- [ ] Investigate the historical unrelated-note response in isolation. It is
  not established as a cause of dream failures and is excluded from the staged
  dream-recovery release.

## Test commands and results

Run from `/home/sbeppi/minisa-working-assistant-20261018/candidate`:

```bash
python -m unittest tests.test_research tests.test_recovery_capture tests.test_dream_recovery tests.test_experience_provenance tests.test_database_connections -v
PYTHONWARNINGS=error::ResourceWarning python -m unittest discover -s tests -v
python ops/verify_candidate_manifest.py /home/sbeppi/minisa-releases/minisa-dream-recovery-candidate-20261018.manifest.json
python ops/verify_candidate_manifest.py /home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021.manifest.json
python ops/measure_retrieval_quality.py --output /tmp/minisa-retrieval-report.json
```

On 2026-10-18, the first command passed 30 tests, the full suite passed 36,
and the manifest verifier returned `verified: true`, 10 runtime files, and
`production_deployed: false`. These validate candidate behavior only; no
production model prompt, deployment, or real post-fix dream was tested.

Re-verified 2026-10-21 after the learning-cycle milestone: the same commands
report 34 focused tests and 138 tests in the full suite, all passing, with
`PYTHONWARNINGS=error::ResourceWarning` enabled. The manifest verifier still
returns `verified: true` for the unchanged 2026-10-18 release snapshot; the
learning-cycle milestone is candidate-only and is not in that release.

Re-verified 2026-10-21 after the release-candidate work: the full suite passes
**150 tests** (6 skipped: the opt-in live-model tests) with
`PYTHONWARNINGS=error::ResourceWarning` on both interpreters, and the same
suite passes against the staged 2026-10-21 release artifact itself. The
2026-10-21 manifest verifier returns `verified: true`, 20 runtime files,
manifest SHA-256
`410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405`,
`migration_required: true`, `production_deployed: false`. The retrieval
measurement reports 14/24 overall hits (exact 6/6, morphological 6/6,
paraphrase 2/6, vague 0/6). No production file, service, or database row was
changed; the live production database was only read, and the migration rehearsal
ran against a disposable copy of it.

Re-verified 2026-10-21 after the recovery-readiness work: the full suite passes
**188 tests** (6 skipped: the opt-in live-model tests) with
`PYTHONWARNINGS=error::ResourceWarning` on the project interpreter and on
`/usr/bin/python3`, and `python -m compileall core runtime ops tests` is clean.
The 38 new tests cover the deployment preflight's fail-closed behaviour on
disposable fixtures. `ops/verify_candidate_manifest.py` still returns
`verified: true` for the unchanged 2026-10-21 release, and rebuilding the
manifest from the tree reproduces it byte for byte. The preflight itself
reports `preflight: blocked` with the expected blockers
(`independent_destination`, `recovery_point`, `service_configuration`); that is
the correct result while no independent recovery destination and no
administrator session exist.
