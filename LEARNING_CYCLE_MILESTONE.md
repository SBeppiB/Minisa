# Minisa learning-cycle milestone

**Date:** 2026-10-21 (UTC)
**Scope:** candidate only (`/home/sbeppi/minisa-working-assistant-20261018/candidate`).
No production service, database, release snapshot, systemd unit or shared Ollama
configuration was touched. No model was downloaded.

## Milestone statement

One complete, reproducible learning cycle is now demonstrated and tested:

```
controlled experiences -> dream -> guarded admission -> durable persistence
-> retrieval by a later conversation -> behavioural benefit in context
```

The learned artefact is a *sourced conversation memory*: the stored fact is the
user's own observed sentence, re-derived by the runtime from the cited
observation. Model output can never create the fact.

## What was implemented

### Admission and persistence (`runtime/cognition/workspace.py`)

- `memory` change schema and `review`/`memory_consolidation` share one guard,
  `_memory_support()`: the candidate text must be a substring (>= 8 characters)
  of a cited observation's `user_statement`, otherwise
  `Memory fact is not supported by cited observations`.
- `_sourced_memory_fact()` recovers the user's own sentence(s) from the cited
  observations; `_supporting_observation()` identifies the row that actually
  carries it; `_persist_memory()` stores one `memory_records` row with
  `authority = runtime_sourced_user_claim`, the source path and its SHA-256.
  No model-supplied text is stored verbatim.
- A supported consolidation review becomes durable learning
  (`dream.memory_persisted` audit). An unsupported one stays review-only and is
  associated as review work; it never becomes a memory.
- `learned_procedures` insert now names all 13 columns of the real schema and
  `procedures()` orders by `global_revision DESC`. Learned procedures stay
  non-executable hypotheses (`status: hypothesis`, `executable: false`).
- `ingest_turn()` rejects non-user provenance: qualification traffic is
  auditable in the turn record but never becomes genuine workspace evidence
  (`{'filtered': True, 'reason': 'non_user_provenance'}`).
- New read-only `sourced_memories(query, limit)`: bounded keyword projection of
  the durable sourced records.

### Retrieval (`runtime/cognition/engine.py`)

- `recall()` merges the durable sourced memories ahead of the Markdown notes
  (bounded by the route limit), reports `memory_status['sourced_memories']`, and
  stays fail-soft when the store is unavailable.
- `_normal_note_response()` answers "what do you know about X" from the same
  runtime-controlled path when a sourced memory matches; the model is not
  consulted for it.

### Connection lifetime (`core/db.py`)

`Database.connect()` returns a scope that commits or rolls back and always
closes the SQLite connection, restoring the documented "SQLite connection
context closure" behaviour. Raw-connection use (`transaction()`, backup) is
unchanged through attribute delegation.

## Tests

`tests/test_learning_cycle.py` (rewritten) uses a real schema-1 turn fixture, a
scripted model, and an isolated temporary workspace. The deterministic suite:

| Test | What it proves |
| --- | --- |
| `test_accepted_memory_becomes_durably_retrievable` | A supported memory is committed, stored with its provenance, retrievable by a later conversation, and present in the reasoning context of an ordinary turn |
| `test_supported_consolidation_review_becomes_durable_memory` | The documented `review`/`memory_consolidation` schema learns the same way and leaves no outstanding review |
| `test_unsupported_memory_claim_is_rejected` | Rejection happens for the right reason (claim absent from the cited observation), nothing is persisted, evidence stays unprocessed |
| `test_invented_evidence_reference_is_rejected` | A proposal citing non-existent evidence is rejected (`Missing snapshot evidence`) |
| `test_justified_empty_proposal_commits_without_learning` | An empty proposal commits safely and learns nothing |
| `test_learned_procedure_is_stored_as_a_non_executable_hypothesis` | A procedure supported by three independent committed actions is stored as a retrievable, non-executable hypothesis |

The live test (`LiveModelLearningTests`) runs the configured real dream model
through the unmodified transport, prompt, wire parser and validator. It is
skipped unless `MINISA_LIVE_DREAM_HOST` and `MINISA_LIVE_DREAM_MODEL` are set,
so the default suite stays hermetic. It asserts the safety invariants for every
live outcome: whatever was persisted is traceable to a cited user statement, a
failed dream persists nothing, and an accepted memory is retrievable by a later
conversation.

## Commands and results

Run from the candidate root:

```bash
PYTHONPATH="$PWD" /srv/minisa/.venv/bin/python3 -m unittest discover -s tests -v
# Ran 138 tests ... OK (skipped=6)

PYTHONPATH="$PWD" python3 -m unittest discover -s tests
# Ran 138 tests ... OK (skipped=6)

PYTHONPATH="$PWD" PYTHONWARNINGS=error::ResourceWarning \
  /srv/minisa/.venv/bin/python3 -m unittest discover -s tests
# Ran 138 tests ... OK (skipped=6)   (no unclosed connections remain)

PYTHONPATH="$PWD" /srv/minisa/.venv/bin/python3 -m unittest \
  tests.test_research tests.test_recovery_capture tests.test_dream_recovery \
  tests.test_experience_provenance tests.test_database_connections
# Ran 34 tests ... OK
```

Live-model demonstration (local Ollama, existing model, isolated temp
workspace; report written to `/tmp/minisa-learning-report.json`):

```bash
PYTHONPATH="$PWD" MINISA_LIVE_DREAM_HOST=http://localhost:11434 \
  MINISA_LIVE_DREAM_MODEL=qwen3.5:2b-q4_K_M \
  /srv/minisa/.venv/bin/python3 -m unittest -v tests.test_learning_cycle.LiveModelLearningTests
# Ran 1 test ... OK (~30 s)
```

Observed live proposals were all safe: an empty proposal, a `personality`
review, and a `memory_consolidation` review whose claim was not a user
statement. In every case nothing unsourced was persisted and protected state
was untouched. The live model is small and non-deterministic; it did not
independently propose a supported memory during this run, which is why the
deterministic suite carries the learning-path assertions.

## Not done / still open

- The verified release manifest still covers the 10 files of the 2026-10-18
  dream-recovery snapshot (`verified: true`, `production_deployed: false`).
  This milestone is **not** in that release; a new snapshot is required before
  any deployment, and deployment remains gated on the P0 recovery point.
  (Superseded 2026-10-21: the staged snapshot
  `/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021`
  contains this milestone, 20 runtime files, `production_deployed: false`,
  `migration_required: true`. See `LEARNING_RELEASE_CANDIDATE_20261021.md`.)
- The semantic-quality gate stays **BLOCKED**: the only local model is
  `qwen3.5:2b-q4_K_M`, there is no embedding-capable model and
  `sentence_transformers` is absent. Sourced memories therefore use keyword
  retrieval only. (Quantified 2026-10-21 by
  `ops/measure_retrieval_quality.py`: 14/24 overall hits, exact 6/6,
  morphological 6/6, paraphrase 2/6, vague 0/6.)
- Dream snapshots do not yet include the durable sourced memories, so the dream
  sees them only as prior evidence. Retrieval works; dream-side context is a
  possible follow-up.
- The real semantic gate (embedding-backed recall quality) and the live GUI
  conversation acceptance remain unverified.
