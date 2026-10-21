# Learning-cycle release candidate — 2026-10-21

**Status: staged, verified, not deployed.** No production file, service, or
database row was changed to produce this candidate.

| Item | Value |
| --- | --- |
| Release | `/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021` |
| Manifest | `/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021.manifest.json` |
| Base release | `/home/sbeppi/minisa-releases/response-context-20261017T010152Z` |
| Changed runtime files | 20 (15 changed, 5 added) |
| Manifest SHA-256 | `410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405` |
| Drop-in candidate SHA-256 | `9e647d83c90946cf894c6f66a3db48323078019f046f38fdfe5bf93d35170879` |
| Supported schema version | 5 (production is still at 4) |
| `production_deployed` | `false` |

The previous candidate snapshot
(`/home/sbeppi/minisa-releases/minisa-dream-recovery-candidate-20261018`,
manifest SHA-256 `e151abc7…`) is unchanged and still verifies. It is superseded
by this release for the learning-cycle work.

## 1. A blocking migration defect, found and fixed

The candidate's `core/migrations.py` was a **divergent rewrite** of the
production migration chain. It produced two concrete failures against the
production database:

- `verify_migrations()` raised `MigrationChecksumError` for migration 1
  (`stage1_persistence_foundation`): the candidate reworded migrations 1–4,
  dropped the `backups` table, added `idempotency_records.details_json`, and
  reshaped `learned_procedures`.
- `apply_migrations()` applied the candidate's v5 and **then** raised the same
  error, i.e. it could leave a half-migrated database behind.

The production lineage is `/srv/minisa/app/core/migrations.py`. Its checksums
match the `schema_migrations` rows of the live database exactly:

| Version | Name | Checksum (production lineage and live database) |
| --- | --- | --- |
| 1 | `stage1_persistence_foundation` | `4d5491e09cb4dd38f43f634e353a2c8866388be0c6ce4e9e6f4109d8e757f845` |
| 2 | `cognitive_state_store` | `b08db440d499a9219e14c10ca9faa8059173be2a68d968a46279c326649b999c` |
| 3 | `persistent_workspace` | `b45c9a577a0aab5711aef9746170d08e0156aaa0769faabd5d343ea12a3a474e` |
| 4 | `phase5_hardening` | `3c28dcc8572902433f54bb398666e6e0b803d4a8c3aeb7a7d682efadc3d56e4e` |

The candidate chain now reproduces v1–v4 byte for byte and adds one additive
migration:

```
5  sourced_memory_records  a06db7fb269e7e7c88108ff5144919cf1841fee647746967afd003ed3849288c
```

Verified against a **disposable copy** of the live production database
(`/srv/minisa/data/minisa.db`, copied read-only; the live file was never
opened for writing):

- `verify_migrations()` → `OK` (version 4).
- `apply_migrations()` → applies **only** v5, leaving every other table and row
  unchanged; `PRAGMA integrity_check` → `ok`.
- A failing migration rolls back completely: the schema version stays at 4, the
  `learned_procedures` shape is unchanged, no v5 row is written
  (`tests/test_migration_chain.py`).

### What migration 5 does

- Creates `memory_records` (`id`, `memory_id`, `details_json`, `revision`,
  `created_at`) plus its indexes — the durable store for sourced memories.
- Rebuilds `learned_procedures` from the production six-column shape into the
  thirteen-column hypothesis shape, carrying every row over. The production
  table currently holds **0 rows**, so nothing is rewritten in practice.

Migration 5 is **additive and one-way**. There is no down migration; that is a
deliberate property, not an omission.

## 2. New tests (150 in the full suite)

| Test module | What it proves |
| --- | --- |
| `tests/test_migration_chain.py` | The candidate chain pins the production lineage; the upgrade preserves every protected row; a failed upgrade changes nothing; the runtime's own SQL fits the migrated schema; a full dream cycle runs on the upgraded production shape. |
| `tests/test_learning_acceptance.py` | Conversation-level acceptance: a real turn becomes dream evidence, a supported fact is admitted and answered by a later conversation, an invented fact is neither persisted nor answered. |
| `tests/test_retrieval_quality.py` | Runs the retrieval measurement and pins its floor. |

Commands and results (run from the candidate root):

```bash
PYTHONPATH="$PWD" /srv/minisa/.venv/bin/python3 -m unittest discover -s tests
# Ran 150 tests — OK (skipped=6)

PYTHONWARNINGS=error::ResourceWarning PYTHONPATH="$PWD" /srv/minisa/.venv/bin/python3 -m unittest discover -s tests
# Ran 150 tests — OK (skipped=6)

PYTHONWARNINGS=error::ResourceWarning PYTHONPATH="$PWD" python3 -m unittest discover -s tests
# Ran 150 tests — OK (skipped=6)

/srv/minisa/.venv/bin/python3 ops/verify_candidate_manifest.py \
  /home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021.manifest.json
# verified: true, runtime_file_count: 20, production_deployed: false
```

The six skips are the opt-in live-model tests
(`MINISA_LIVE_DREAM_HOST` / `MINISA_LIVE_DREAM_MODEL` unset).

The **staged artifact itself** was also tested, not only the working tree: the
test tree was copied outside the repository and run with the staged release on
`PYTHONPATH`, so `core` and `runtime` resolved to
`/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021`.
Result: `Ran 150 tests — OK (skipped=6)`.

## 3. Retrieval measurement and the smallest next step

`ops/measure_retrieval_quality.py` measures whether a later conversation can
find what the dream learned. 24 queries over 6 stored memories, in four
categories:

| Category | Example | Retrieved | Answered without the model | Handed to the model |
| --- | --- | --- | --- | --- |
| exact | "what do you know about the tea ceremony" | 6/6 | 6/6 | 6/6 |
| morphological | "how do we prepare the tea ceremony" | 6/6 | 1/6 | 6/6 |
| paraphrase | "how do we brew the green powder ritual" | 2/6 | 0/6 | 2/6 |
| vague | "what did we agree on for the ritual" | 0/6 | 0/6 | 0/6 |
| **overall** | | **14/24 (0.58)** | **7/24 (0.29)** | **14/24 (0.58)** |

Reading of the numbers:

- Exact questions are fully served by the runtime: the memory is retrieved and
  answered deterministically from the durable store, never from the model.
- The lexical matcher is at its ceiling. Morphological variants still hit
  because the topic words overlap; paraphrases mostly miss, and vague
  references always miss. No amount of stopword or stemming work recovers
  those: they need meaning, not spelling.
- Where retrieval succeeds but the answer is not deterministic, the memory is
  still handed to the model as context, so the answer depends on the model
  following the provided context.

**Smallest next step (one change, no new dependency):** add an *opt-in*
semantic recall for durable memories, using the embedding endpoint the runtime
configuration already names (`embedding_model`, currently `nomic-embed-text`),
and measure it with the same harness before and after. It stays off by
default, it must never invent memory content, and it must not be written until
an embedding model is actually available — an untestable semantic path would be
an unsourced claim. No model was downloaded and no semantic code was added in
this round; the measurement above is the evidence that decides the work.

Second, smaller step available without any model: log a *recall gap* (query
category only, no content) whenever a topic question finds no durable memory,
so the operator can see what the runtime cannot answer. This is an additive
audit event and needs a review of the audit schema before it is written.

## 4. Deployment order — P0-gated, not executed

This release is **not deployable** until the P0 recovery point exists
(`MINISA_FIX_LIST.md`, P0 section). The reason is now sharper than before:
migration 5 has no down migration, so the pre-migration database state is part
of the rollback plan.

Proposed order for a future administrator window:

1. **P0 first.** Independent recovery destination, administrator-authenticated
   window, `ops/capture_minisa_recovery.py` preflight, then maintenance capture
   with `capture=verified`, matching checksums, SQLite integrity and schema
   checks, and a disposable restore rehearsal. Nothing below may start before
   this succeeds.
2. **Core service, migration.** Copy the candidate's `core/db.py` and
   `core/migrations.py` into the core service application directory
   (`/srv/minisa/app/core`). The core service applies migration 5 on start
   (migrations are an operator step; the runtime never migrates on its own).
3. **Verify the database.** Schema version 5, `PRAGMA integrity_check` = `ok`,
   `backups`, `core_meta`, and all existing rows still present.
4. **Runtime.** Activate
   `/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021` and
   install the drop-in candidate
   `deploy/minisa-learning-cycle-timeout.conf.candidate`
   (`WorkingDirectory=` the release, `TimeoutStopSec=50`). The runtime refuses
   to start against a schema version other than 5 — it fails closed.
5. **Post-checks.** `/health`, `/v1/dream-status`, one new durable dream
   result (timestamp, source and result revisions, evidence count, matching
   audit rows), and protected-file hashes unchanged against the verified
   recovery point.

**Rollback.** With the database at version 5, the previous runtime release
cannot start: its `core/migrations.py` supports version 4 and raises
`SchemaTooNewError` for a newer database, and `MinisaApplication.start()`
refuses any schema version other than its own supported one. Two ways back:

- Keep the candidate `core/` directory and revert only the `runtime/` files —
  the new `core/db.py` is compatible with both schema versions.
- Or restore the verified pre-capture database, which is exactly why step 1 is
  a hard prerequisite rather than good practice.

### 4.1 Recovery-readiness artifacts (added 2026-10-21)

The procedure above is now written down and machine-checked, without changing
anything in this release:

- `RECOVERY_RUNBOOK.md` — the operator procedure: gates, pre-migration backup
  and its verification, the migration step, activation, the restoration
  procedure, the rollback ladder (fix forward → hybrid release → database
  restore), and the explicit stop conditions.
- `ops/preflight_learning_cycle_release.py` — a read-only, fail-closed
  preflight over the manifest, the staged release, the migration lineage, the
  live database, the core-service copy step, the effective service
  configuration, the block-device topology, a verified recovery capture, the
  rollback prerequisites and the test suite. On 2026-10-21 it reported
  `preflight: blocked` with `independent_destination`, `recovery_point` and
  `service_configuration` blocking, which is the correct state of this host:
  it has one physical disk and no administrator session.
- `SEMANTIC_RECALL_EXPERIMENT.md` — the semantic-recall experiment is a
  written proposal with acceptance thresholds, kept out of deployment work and
  out of this release.

None of these change the manifest: rebuilding it from the tree reproduces
`410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405` byte for
byte, and `ops/verify_candidate_manifest.py` still returns `verified: true`
with 20 runtime files and `production_deployed: false`.

## 5. Files in this release (20)

Changed (15): `core/db.py`, `core/migrations.py`,
`runtime/cognition/dream.py`, `runtime/cognition/dream_context.py`,
`runtime/cognition/dream_request.py`, `runtime/cognition/dream_wire.py`,
`runtime/cognition/engine.py`, `runtime/cognition/turns.py`,
`runtime/cognition/workspace.py`, `runtime/memory/embeddings.py`,
`runtime/memory/semantic_retriever.py`, `runtime/server/application.py`,
`runtime/server/config.py`, `runtime/server/http.py`,
`runtime/services/memory.py`.

Added (5): `runtime/research.py`, `runtime/research_cli.py`,
`runtime/services/shadow_recall.py`,
`runtime/services/universal_memory_readonly.py`,
`runtime/services/universal_recall.py`.

The five added files are required by the changed `runtime/server/application.py`
(the universal and shadow recall backends are explicit opt-ins and stay
disabled by default). The earlier 2026-10-18 snapshot did not contain them and
therefore did not match the current candidate tree.

## 6. Reproducing this release

```bash
cd /home/sbeppi/minisa-working-assistant-20261018/candidate
/srv/minisa/.venv/bin/python3 ops/build_candidate_manifest.py \
  --base /home/sbeppi/minisa-releases/response-context-20261017T010152Z \
  --release /home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021 \
  --tree "$PWD" \
  --dropin deploy/minisa-learning-cycle-timeout.conf.candidate \
  --out /home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021.manifest.json \
  --supported-schema-version 5 --live-schema-version 4 --live-global-revision 195 \
  --migration-required --core-service-migration-required
/srv/minisa/.venv/bin/python3 ops/verify_candidate_manifest.py \
  /home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021.manifest.json
```

The builder copies nothing: it compares base release and working tree, checks
that the staged release already contains exactly those files byte for byte, and
writes the manifest. A mismatch is an error.
