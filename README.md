# Minisa

Minisa is a constrained cognitive system with persistent identity, controlled memory, and bounded reflection. This repository contains the candidate codebase for development and testing.

## Architecture (high level)

- **Core identity** (`CORE/`): Persistent identity files (e.g., identity, values, goals, personality, instructions) are treated as authoritative for the system's identity.
- **Cognition engine** (`runtime/cognition/`): The cognition engine manages turns, workspace state, and integrates with memory/recall. The workspace tracks units, evidence, and proposals with strict validation.
- **Memory** (`runtime/memory/`): Memory management with support for different memory types; recall backends are configurable. Notes saved by autonomous research are explicitly marked as **unverified**.
- **Server/application** (`runtime/server/`): HTTP/service layer, configuration loading, and lifecycle management.
- **Migrations** (`core/migrations.py`): Schema migrations for the core database; schema versioning is enforced.

## Memory and cognition systems

- **Workspace**: A bounded workspace that ingests turn evidence, maintains units/questions/contradictions, applies pressure-based scheduling for reflection, and enforces proposal schemas and review requirements.
- **Dreaming (reflection)**: `runtime/cognition/dream.py` implements bounded sleep-time reflection via a `DreamWorker`. Dreams operate as a separate process/job path with strict timeouts, atomic coordination via the database, and audit logging. Proposals are validated against strict schemas; only certain change types are admitted and many require review.
- **Cognitive stages**: The system does not force NREM/REM stage modeling unless there are actual distinct cognitive stages that map meaningfully; current implementation treats dreaming as bounded reflection with quotas/cooldowns.
- **Research integration**: Dream ticks may trigger autonomous research (opt-in via environment variables) without user prompts. Research uses the separate orchestrator with its own quotas and safeguards.

## Dreaming

- **DreamWorker**: Background worker that ticks the workspace, enforces cooldowns/hourly caps, leases dream jobs atomically, runs bounded model calls via subprocess transport, validates proposals, and applies changes atomically.
- **Safety**: Timeouts on model calls, transport deadlines with teardown margins, failure categorization without leaking model output, circuit breaking on repeated failures, lease recovery for crashed workers, and prevention of overlapping runs.
- **Current behavior**: Dream scheduling is driven by workspace pressure/idle conditions with existing policies (cooldowns, calls per hour). See `DREAM_RECOVERY_RELEASE.md` and `DREAM_AUTO_RESEARCH.md` for details.

## Autonomous research

- **Research orchestrator** (`runtime/research_orchestrator.py`): Triggers research on eligible workspace units (questions/contradictions) when enabled. Uses `runtime/research.py` for web search/extraction.
- **Safety controls**: SSRF protections, fetch/time limits, per-hour/per-day quotas, per-unit cooldowns, deduplication, attribution, and explicit "unverified" marking on saved notes. All network access is constrained.
- **Separation of budgets**: Research and dreaming have separate budgets/quotas.
- **Opt-in**: Disabled by default; enabled via `MINISA_RESEARCH_ENABLED=true`.

## Configuration

Configuration is loaded via environment variables and/or TOML (see `runtime/server/config.py`). Key settings include:
- Database paths, model/host settings, timeouts
- Dream/research related settings (time budgets, quotas)
- Feature flags (e.g., research enablement)

Never commit secrets or API keys. Use environment variables or local untracked config files.

## Safety boundaries

- **Candidate-only changes**: This is a candidate workspace; production paths like `/srv/minisa` must not be modified.
- **No secrets in repo**: API keys, credentials, `.env` files, production databases, WAL/SHM files, logs, backups are excluded via `.gitignore`.
- **Bounded execution**: Timeouts, quotas, atomic DB coordination, and failure isolation prevent runaway behavior.
- **Untrusted content**: Web content is treated as untrusted; research notes are attributed and explicitly marked unverified.
- **Review-required changes**: Many cognitive changes require review and cannot be auto-applied.

## Tests

Run tests with Python unittest (see `CONTRIBUTING.md`):
```bash
cd /home/sbeppi/minisa-working-assistant-20261018/candidate
PYTHONPATH=/home/sbeppi/minisa-working-assistant-20261018/candidate python -m unittest discover -s tests
```
Key test suites: `tests/test_dream_recovery.py`, `tests/test_research_orchestrator.py`, `tests/test_learning_cycle.py`, etc.

## Current limitations

- Research is opt-in and heavily rate-limited by design.
- Dreaming uses existing job/lease infrastructure with conservative defaults.
- Some features are experimental/candidate-only; see individual docs (`DREAM_AUTO_RESEARCH.md`, `WEB_RESEARCH.md`).
- Network access depends on environment and safety controls - live research requires eligible questions and proper enablement.

---

*This README was added to document the system for a private backup. It distinguishes implemented features from their safety/experimental nature as noted above.*
