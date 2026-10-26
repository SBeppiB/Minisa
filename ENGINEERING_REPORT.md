# Engineering Report: Minisa × Universal Memory Reliability Hardening

## Scope
- Phases A–I hardening on candidate workspace: `/home/sbeppi/minisa-working-assistant-20261018/candidate`
- Non-production work; no shared Ollama configuration changes; shadow never selects universal backend; no network/model downloads.

## Test Results
- System Python (`python3`): **128 tests, 5 skipped, 0 failures, 0 errors**
- Universal Memory venv (`.venvs/universal-memory/bin/python`): **136 tests, 0 skipped, 0 failures, 0 errors**

## Implemented
- **Phase A (config):** unknown TOML keys rejected; `FileNotFoundError`/decode → explicit `ValueError` with path; shadow+universal conflict rejected; explicit universal backend fails fast on setup errors (not silent fallback).
- **Phase B (recall):** unreadable/corrupt notes skipped in keyword and semantic recall + inspect; robust query/limit coercion; UM adapter filters non-finite/negative scores, drops malformed hits, deterministic tie-break+dedup, guarded `close()`.
- **Phase C (embeddings):** thread-safe cooldown; bounded timeout (30s); unusable vectors classified as `embedding_protocol_error`.
- **Phase D/E (shadow + universal):** shadow evaluator write-lock + partial-line repair, write-failure circuit (50 consecutive → disabled), error messages redacted; universal recall watchdog (10s), single-outstanding gate, circuit breaker (3 failures → 60s cooldown), counters in status.
- **Phase F/G/H (concurrency/lifecycle/persistence/fault basics):** added `tests/test_concurrency_lifecycle.py`, `tests/test_lifecycle_recovery.py` (deterministic temp data; no production changes).
- **Phase I (operational recovery/finalization):** added `tests/test_fault_injection_recovery.py` covering corruption resilience, partial init validation, and cross-reconstruction consistency. Candidate-only; no prod changes.

## Blockers
Real semantic quality gate remains **BLOCKED** (no embedding-capable Ollama/model, `sentence_transformers` absent; cannot reconfigure shared Ollama/download models).

All existing+new tests pass in both environments. No production changes.
