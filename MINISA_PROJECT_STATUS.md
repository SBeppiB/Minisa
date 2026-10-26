# Minisa project status

**Checked:** 2026-10-18 (UTC)  
**Documentation root:** `/home/sbeppi/minisa-working-assistant-20261018/candidate`  
**Scope:** candidate development and read-only production checks. No production
deployment, restart, forced dream, recovery-circuit reset, or data write was
performed for this status update.

**Updated 2026-10-21:** candidate-only learning-cycle milestone added and
re-tested (see below). No production read-only check, deployment or restart was
performed on that date; the production observations below remain the
2026-10-18 ones and should be rechecked before any later maintenance action.

**Updated 2026-10-21 (later):** the migration chain was realigned with the
production lineage, an additive migration 5 was added, a staged release snapshot
with 20 changed runtime files was built and verified, and the retrieval quality
of durable memories was measured. Details, commands and results are in
`LEARNING_RELEASE_CANDIDATE_20261021.md`. Still candidate-only: nothing was
deployed, no service was restarted, and the live database was only read.

## Current production state

Read-only checks on 2026-10-18 found:

- `minisa.service` and `minisa-core.service` were active and running.
- `minisa.service` still used
  `/home/sbeppi/minisa-releases/response-context-20261017T010152Z`;
  `minisa-core.service` still used `/srv/minisa/app`.
- `GET /health` returned `{"status":"ok","service":"minisa"}`.
- `GET /v1/dream-status` returned 404. That route exists in the candidate,
  but is not available in the current production release.
- `GET /v1/cognitive-health` returned `review`: 6 unresolved experiences,
  5 failed dream jobs, 4 repeated failures, and an active recovery circuit.
- Read-only SQLite inspection found three completed dream results. The latest
  is `2026-10-17T01:00:56.342Z`, source revision 34, result revision 35, with
  7 records considered. The other two results advanced revisions 25→26 and
  32→33 after considering 11 and 10 records.
- Five later failed jobs remain recorded: four `timeout`, one
  `invalid_output`. The newest recorded failure is a timeout created at
  `2026-10-18T04:32:10Z`. The available current status exposes the recovery
  gate through `2026-10-19T04:32:30Z`; this is a scheduler state, not a
  successful dream.
- There are 37 workspace evidence rows, 6 still unprocessed. These remain
  available for normal future processing; none were deleted or rewritten.
- The live configuration identifies `qwen3:8b-q4_K_M` at the configured
  remote model endpoint. Historical successful model call metadata is recorded
  in `WORKING_ASSISTANT_ACCEPTANCE.md`; do not infer current model health from
  configuration alone.

These are observations from the stated check time. Recheck status before a
later maintenance action. The last success time is not evidence of a new
success after that cycle.

## Recovery and deployment

The dream-recovery release is candidate-only. The verified manifest currently
lists 10 runtime files, has SHA-256
`e151abc79aa3b83c1238682c2e38f2ed30bd403409f3bbff2a3d1d5ea75a7746`, and
reports `production_deployed: false`. Candidate testing passed 30 focused
tests and 36 tests in the full suite with SQLite `ResourceWarning`s promoted
to errors. The exact commands and test scope are in
`DREAM_RECOVERY_RELEASE.md` and `MINISA_FIX_LIST.md`.

No complete, independent full-system recovery point exists. The only recorded
production backup is a database-only SQLite snapshot stored on the same
physical NVMe as production. The recovery capture script has passed disposable
restore tests, but it has not captured production. A controlled full capture
needs an independent destination and administrator-authenticated maintenance
access; it temporarily stops both Minisa services and then restores their
prior service state. Do not deploy before the full capture and disposable
restore verification pass.

The candidate deployment does not require a database migration. Its planned
code-only rollback removes the candidate systemd drop-in and restarts the
unchanged original release; it does not restore an older database over newer
Minisa state. See `DREAM_RECOVERY_RELEASE.md` for the detailed procedure.

## Internet research candidate

The separate research candidate consists of `runtime/research.py`,
`runtime/research_cli.py`, `tests/test_research.py`, and `WEB_RESEARCH.md`.
It supports optional Tavily credentials or a configured SearXNG-compatible
JSON endpoint, bounded public-page extraction, SSRF checks, and concise,
deduplicated source-linked notes saved through existing `MEMORY` storage.
Saving currently requires the caller to provide a concise summary. Its notes
are eligible for the existing bounded memory context when relevant; no new
dream evidence path is added.

Research is **not verified as integrated into the live chat API or GUI and is
not deployed**. Do not present it as a production feature. A Tavily key is
optional; before use, keep the account on its free plan and ensure paid
overages are disabled. SearXNG must have JSON output enabled.

## GUI and chat

Earlier candidate GUI work is recorded in `WORKING_ASSISTANT_ACCEPTANCE.md`.
That record says a basic live greeting was durably stored and a later
multi-question reply returned unrelated notes. It also says graphical display
verification could not be performed in the server shell. Treat those as
historical observations, not current acceptance. No claim is made here that
the current GUI is running or that normal chat has passed full acceptance.

## Learning-cycle milestone (candidate)

A first verified learning cycle exists in the candidate and is tested
deterministically: controlled experiences -> dream -> guarded admission ->
durable sourced memory -> retrieval by a later conversation -> behavioural
benefit in the reasoning context. The stored fact is always the user's own
observed sentence, re-derived from the cited observation; unsupported or
invented claims are rejected and persist nothing. The live configured model was
also run through the unmodified dream path in an isolated temporary workspace
and produced only safe outcomes (empty proposal or review-only admission).
Details, commands and results are in `LEARNING_CYCLE_MILESTONE.md`.

This is candidate-only. It is **not** in the verified 2026-10-18 release
snapshot (`verified: true`, `production_deployed: false`), and deployment
remains gated on the P0 recovery point. The semantic-quality gate stays
blocked: no embedding-capable model is available, so retrieval is keyword based.

## Release candidate 2026-10-21 (candidate)

A second staged release,
`/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021`, holds
the learning cycle plus a corrected migration chain. Its manifest verifies with
20 runtime files, SHA-256
`410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405`,
`migration_required: true`, `production_deployed: false`. The staged artifact
itself passes the 150-test suite, not only the working tree.

The correction matters: the candidate's earlier migration chain was a
divergent rewrite, so `verify_migrations()` failed against the production
database and `apply_migrations()` could leave a half-migrated database behind.
v1–v4 now match the production lineage byte for byte, and v5
`sourced_memory_records` is additive (new `memory_records` table plus a
`learned_procedures` rebuild that carries rows over; the production procedure
table is empty). The rehearsal ran only against a disposable copy of the live
database, which was never modified.

Retrieval measurement (24 queries, 6 stored memories): exact 6/6,
morphological 6/6, paraphrase 2/6, vague 0/6 — 14/24 overall. Exact questions
are answered deterministically from the durable store; the lexical matcher is
at its ceiling for meaning-based questions. The smallest next step is an
opt-in semantic recall using the embedding endpoint the configuration already
names, measured with the same harness; it is deliberately not written while no
embedding model is available to test it.

Deployment is still P0-gated, and the reason is now concrete: migration 5 has
no down migration, so the pre-capture database state is part of the rollback
plan. The order is recovery point first, then the core service applies
migration 5, then the runtime release is activated. The runtime refuses to
start against a schema version other than 5.

## Recovery readiness (2026-10-21)

The deployment procedure for the schema-5 candidate is now written down and
machine-checked, and it is blocked for concrete, verifiable reasons.

`ops/preflight_learning_cycle_release.py` is a read-only, fail-closed
preflight. It verifies the candidate manifest and every tracked file in the
staged release, the migration lineage against the checksums recorded in the
live database, the live database's integrity and schema state, the core-service
code copy step, the effective service configuration, the presence of a
genuinely independent recovery destination, a verified recovery capture, the
rollback prerequisites, and the test suite. Anything it cannot verify is a
failure, not a warning; on 2026-10-21 it reported `preflight: blocked` with
`independent_destination`, `recovery_point` and `service_configuration`
blocking. It never stops a service and never opens the production database for
writing. 38 tests cover its fail-closed behaviour on disposable fixtures.

The recovery destination question has a definitive answer for this host: there
is exactly one physical disk (`nvme0n1`), and `/`, `/home`, `/var`, `/srv` and
the Ollama model directory all descend from it. `/mnt`, `/media` and
`/run/media` are empty, `fstab` declares no external device, no backup or
capture timer exists, and the only non-local mount is a kernel pipe. The
existing recovery material — the 2026-10-18 database-only capture and the five
registry backups — was re-verified (hashes match, `integrity_check ok`) but
lives on that same disk and is schema 1–4 material only, so it is not an
independent recovery point.

`RECOVERY_RUNBOOK.md` holds the operator procedure: the gates, the pre-migration
backup with its verification requirements, the migration step and its
verification, the runtime activation, the restoration procedure, the rollback
ladder, and the explicit stop conditions. Migration 5 rebuilds
`learned_procedures` and has no down migration, so the rollback ladder is:
fix forward, then a database restore from the verified pre-migration capture,
then a full restore — with the base runtime unable to start against a schema-5
database made explicit.

The semantic-recall experiment is now a written proposal with thresholds and a
blocking prerequisite (`SEMANTIC_RECALL_EXPERIMENT.md`), separate from
deployment work and not implemented: the local Ollama serves only
`qwen3.5:2b-q4_K_M`, which has no embedding capability, and the configured
remote endpoint does not answer from this host, so there is nothing testable to
measure yet.

## Project motto

> copy the features from other AI and repo documents so we dont have to struggle with waiting hours for a feature to be made

Use this as a development direction: inspect existing and prior-art
implementations, reuse established patterns where suitable, and keep
Minisa-specific provenance, privacy, protection, and recovery requirements.

## Related documents

- `MINISA_FIX_LIST.md` — ordered candidate work and release blockers.
- `LEARNING_CYCLE_MILESTONE.md` — verified learning-cycle milestone, tests and
  live-model demonstration.
- `LEARNING_RELEASE_CANDIDATE_20261021.md` — staged release snapshot, migration
  chain correction, retrieval measurement, and the deployment order.
- `ADMIN READ ME PLEASE I BEG.md` — administrator backup, deployment, and
  rollback instructions.
- `RECOVERY_RUNBOOK.md` — deployment procedure, gates, migration and rollback
  ladder, restoration steps, and explicit stop conditions.
- `SEMANTIC_RECALL_EXPERIMENT.md` — proposal for a separate, measurable
  semantic-recall experiment and its blocking prerequisite.
- `DREAM_RECOVERY_RELEASE.md` — release-specific candidate diff and procedure.
- `WORKING_ASSISTANT_ACCEPTANCE.md` — dated historical investigation and
  acceptance notes.
- `WEB_RESEARCH.md` — separate candidate research tool and setup notes.

No README or documentation index existed in this candidate root at the time of
inspection, so none was overwritten or changed.
