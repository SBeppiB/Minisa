# Dream-driven automatic research (candidate-only)

## Enable
Set environment variable: `MINISA_RESEARCH_ENABLED=true` (disabled by default).

## Behavior
- During each dream tick, if enabled and orchestrator available, the candidate `DreamWorker` calls `ResearchOrchestrator.run_once(max_tasks=1)` automatically with no user prompts.
- Triggers from eligible unresolved questions (`kind='question'`, `status!='resolved'`, meets importance/uncertainty thresholds) and contradictions (`kind='contradiction'`, `status='active'`).
- Uses existing `runtime/research.py` (SSRF protections, limits). Dream model never calls network directly.
- Fetched content treated as untrusted; notes saved via `MemoryManager` with source attribution and marked unverified.
- Research failures are caught and logged; they do not block or crash the dream cycle.

## Defaults (configurable via env)
- `MINISA_RESEARCH_MAX_PER_HOUR=1`
- `MINISA_RESEARCH_MAX_PER_DAY=3`
- `MINISA_RESEARCH_PER_UNIT_COOLDOWN_HOURS=24`
- `MINISA_RESEARCH_MAX_FETCHES_PER_TASK=2`
- Question: importance>=0.6, uncertainty>=0.4
- Contradiction: importance>=0.7, uncertainty>=0.6

## Safety
- Opt-in only, default disabled
- Concurrency-safe quotas/cooldowns via SQLite transactions
- Deduplication by source URL (via `research.save_research`) and cooldowns
- No interactive prompts
- No writes to `/srv/minisa`, no production changes
- No new background worker added (runs inline in dream tick)
- Audit events logged (no secrets)

## Testing (offline)
```bash
cd /home/sbeppi/minisa-working-assistant-20261018/candidate
PYTHONPATH=/home/sbeppi/minisa-working-assistant-20261018/candidate \
  python -m unittest tests.test_research_orchestrator -v
PYTHONPATH=/home/sbeppi/minisa-working-assistant-20261018/candidate \
  python -m unittest tests.test_research -v
```
