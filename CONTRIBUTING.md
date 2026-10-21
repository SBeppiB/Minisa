# Contributing / working on the Minisa candidate workspace

This repository is a **private** snapshot of the Minisa candidate workspace used for
the learning-cycle release of 2026-10-21. It contains source code, tests,
migrations, operational scripts, runbooks and engineering reports. It does **not**
contain the Minisa database, any recovery capture, credentials or production
configuration.

Nothing here is licensed: no licence file is included, so all rights are reserved
by default. Do not add one without explicit authorisation from the owner.

## Supported Python versions

* The code was developed and verified on **CPython 3.14.8** (both `/usr/bin/python3`
  and the production virtual environment).
* The minimum is **3.11**, inferred from stdlib usage (`tomllib` in
  `runtime/server/config.py`). That is an inference, not a tested matrix.
* Other versions are untested here. Do not claim support for them without running
  the test suite on them.

## Dependencies

There is **no packaging metadata and no lock file** in this workspace: no
`pyproject.toml`, `setup.py`, `requirements*.txt`, `Pipfile` or `uv.lock`. Dependencies
are therefore *not pinned* and cannot be reproduced byte-for-byte from this
repository alone.

What the code actually imports:

| Dependency | Needed for | Notes |
| --- | --- | --- |
| `fastapi` | the core service (`core/app.py`) | only third-party runtime import |
| `universal_memory` | optional recall backends and some tests | the affected tests **skip** when it is absent |

Everything else is the standard library. The persistent runtime server
(`runtime/server/main.py`) uses the standard library HTTP server, not uvicorn.

If you add dependencies, add them to a requirements file in the same change and
say so in the commit message.

## Environment setup

```bash
python3 -m venv .venv                     # .venv/ is ignored by git
. .venv/bin/activate
pip install fastapi                       # required only for the core service
```

Point the code at a **local, throw-away** database — never at production:

```bash
export MINISA_DB_PATH="$PWD/.local/minisa-local.db"
```

`core/app.py` reads `MINISA_DB_PATH` and falls back to the production path
`/srv/minisa/data/minisa.db` when it is unset. Always set it while developing.

Other configuration the server reads: `runtime/server/config.py` loads a TOML file
passed with `--config`. No production configuration file is included here; create
your own local copy with placeholder values.

## Test commands

```bash
# full suite (read-only, self-contained; must not touch production)
python3 -m unittest discover -s tests

# one module
python3 -m unittest tests.test_migration_chain
python3 -m unittest tests.test_rollback_runbook

# with resource-leak warnings turned into errors (recommended before a release)
PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests
```

The last reported full-suite result was **210 tests, `OK (skipped=6)`** on
2026-10-21 under both interpreters. The skipped tests are the ones that need the
optional `universal_memory` package or a live service.

## Safe local development

* **Never point any command at the production database, config or services.**
  `/srv/minisa/data/minisa.db`, `/srv/minisa/app`, `/etc/systemd/system/minisa*`
  and the staged releases under `/home/sbeppi/minisa-releases/` are outside this
  repository and are only ever inspected read-only.
* `ops/preflight_learning_cycle_release.py` and `ops/verify_recovery_database.py`
  are read-only and fail closed — they open the database with `mode=ro` and
  `PRAGMA query_only=ON`. They are safe to run.
* `ops/capture_minisa_recovery.py` is the only script that writes outside the
  workspace, and only with `--execute-maintenance` (it stops services), and it
  requires root plus a verified independent destination. Do not run it while
  developing.
* `ops/build_candidate_manifest.py` writes a manifest for a staged release. The
  staged release is immutable until it is approved for deployment — do not
  regenerate or edit it from here.
* `deploy/*.candidate` are systemd drop-in **templates**. The `WorkingDirectory=`
  line in them is host-specific: adapt it, do not treat it as portable.
* `runtime/cognition/engine.py` and `runtime/cleanup_memories.py` contain the
  creator-identity string `sbeppi`. It is behaviour, not configuration — change it
  only deliberately, and see the manual-review list in
  `GITHUB_PREPARATION_REPORT_20261021.md`.

## Where things are documented

| Document | What it covers |
| --- | --- |
| `NEXT_ACTIONS.md` | deployment handoff, gate order, and why deployment is still NO-GO |
| `RECOVERY_RUNBOOK.md` | the operator procedure: capture, migration, activation, rollback |
| `ROLLBACK_RUNBOOK_FIX_REPORT_20261021.md` | what was wrong with the rollback procedure and how it was fixed |
| `RECOVERY_READINESS_REPORT_20261021.md` | readiness evidence and outstanding blockers |
| `DEPLOYMENT_REHEARSAL_REPORT_20261021.md` | the rehearsal findings (F1–F9) |
| `LEARNING_RELEASE_CANDIDATE_20261021.md` | what the learning-cycle release changes and how to verify it |
| `GITHUB_PREPARATION_REPORT_20261021.md` | how this snapshot was prepared for GitHub |

## Commit hygiene

* Keep the repository **private**. It contains host paths, internal hostnames and
  internal IPs.
* Do not commit databases, captures, logs or anything under `/srv/minisa`.
* Do not add `*.db`, `*.log` or `.env` files with `git add -f`; if the ignore rules
  are wrong, fix the rules instead.
* Run `python3 -m unittest discover -s tests` before proposing a change that
  touches `core/`, `runtime/` or `ops/`.
