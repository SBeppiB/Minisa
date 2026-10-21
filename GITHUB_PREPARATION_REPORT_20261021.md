# GitHub preparation report — Minisa candidate workspace (2026-10-21)

**Scope.** Prepare `/home/sbeppi/minisa-working-assistant-20261018/candidate` for a later
commit to a **private** GitHub repository.

**What was done.** A read-only audit of the whole workspace, a new `.gitignore`, a new
`CONTRIBUTING.md`, and a verification of exactly what an initial import would contain.
**No repository was created remotely, nothing was pushed, nothing was deployed, and
no production or staged-release file was modified.**

---

## 1. Existing Git history or remotes

**None found. No Git metadata exists anywhere in this workspace or its parents.**

| Check | Result |
| --- | --- |
| `.git` in the candidate workspace | absent |
| `.git` in any parent directory (`/home/sbeppi/minisa-working-assistant-20261018`, `/home/sbeppi`, `/home`, `/`) | absent |
| `git rev-parse --is-inside-work-tree` from the candidate | `fatal: not a git repository (or any of the parent directories): .git` |
| Existing commits / branches / remotes / stashes | none — there is no repository to inspect |
| Credential-bearing remote URL | none — no remote exists, so no URL could expose a token |
| Git tool version | `git version 2.56.0` |
| Global `user.name` / `user.email` | configured in the global git config; **values not printed and not copied into any file** |

**Stop condition not triggered.** There was no history to preserve, nothing to rewrite,
and no credential-bearing remote. Because no history exists, there is also nothing in
"existing Git history" that could contain a secret — the history scan is vacuously
clean and is reported as such rather than as a pass by assumption.

`git init` has **not** been run in the workspace. The commands in §8 are the ones to
run later, after a human review of this report.

---

## 2. Files added or modified

Three files were **added**. **No existing file was modified, renamed, moved or deleted.**

| File | Status | Why |
| --- | --- | --- |
| `.gitignore` | added (1,816 B) | No ignore rules existed. The only `.gitignore` in the tree was `.pytest_cache/.gitignore`, which pytest creates itself. |
| `CONTRIBUTING.md` | added (5,662 B) | Existing docs gave the test command but not the supported Python versions, environment setup, or safe-local-development rules. §6 lists what it covers and what it deliberately does not claim. |
| `GITHUB_PREPARATION_REPORT_20261021.md` | added (this file) | The required deliverable. |

Nothing was excluded by deletion: the ignore rules only affect what an import would
contain. The 121 `.pyc` files and the 4 `.pytest_cache` files are **still on disk** —
ignoring them does not remove them.

---

## 3. Workspace inventory

| Metric | Value |
| --- | --- |
| Total files in the workspace | 252 before this task → **255** now (3 added, 0 deleted) |
| Total size | 2,508,357 B → **≈ 2,538,200 B** now (≈ 2.42 MiB) |
| Directories | 39 |
| Python source files | 109 |
| Markdown documents | 16 → **18** (`CONTRIBUTING.md` and this report added) |
| Compiled `.pyc` files | 121 |
| Other files | 5 (2 systemd drop-in candidates, 3 pytest-cache files) + `.gitignore` |

### 3.1 Included in the initial import — 130 files, ≈ 1,079,600 B (≈ 1.03 MiB)

Verified with `git add --dry-run -A` against the workspace, using a **scratch** git
directory outside the workspace (`/tmp/opencode/ignorecheck`, `--work-tree` pointing
at the candidate). The scratch directory holds no index entries and the workspace
holds no `.git`.

| Group | Files | Bytes |
| --- | --- | --- |
| `runtime/` (server, cognition, memory, personality, filesystem, services, reasoning, state, …) | 74 | 435,309 |
| `tests/` (22 test modules + `retrieval_quality_eval.py`) | 23 | 206,741 |
| Reports, runbooks, notes + `CONTRIBUTING.md` + this report (`.md`) | 18 | 286,876 |
| `ops/` (6 operational scripts) | 6 | 102,721 |
| `core/` (`app.py`, `db.py`, `migrations.py`, `cognitive.py`, `backup.py`, `__init__.py`) | 6 | 45,570 |
| `deploy/` (2 systemd drop-in candidates) | 2 | 514 |
| `.gitignore` | 1 | 1,816 |
| **Total** | **130** | **≈ 1,079,600 B (≈ 1.03 MiB)** |

Every test module is included; no source file is ignored by any rule.

The definitive per-file list is in §5.

### 3.2 Excluded from the import — 125 files, 1,458,560 B (57 % of the workspace)

| Excluded | Files | Bytes | Rule |
| --- | --- | --- | --- |
| `__pycache__/**/*.pyc` | 121 | 1,443,851 | `__pycache__/`, `*.py[cod]` |
| `.pytest_cache/**` (`.gitignore`, `CACHEDIR.TAG`, `README.md`, `v/cache/nodeids`) | 4 | 14,709 | `.pytest_cache/` |

Both groups are regenerable build/cache output and are not part of a reproducible
source checkout. No database, capture, backup, log, credential or environment file
exists anywhere in the workspace, so no such file had to be excluded — the rules
that would exclude them are in place and tested (§4.3).

### 3.3 Deliberately kept

* **All 17 existing `.md` documents**, including the reports, the runbook and the
  deployment handoff — they are the record of the learning-cycle work and the reason
  the repository is reproducible.
* **`deploy/*.candidate`** — the systemd drop-in sources referenced by the runbook and
  by `NEXT_ACTIONS.md`. They contain no credentials, only a host-specific
  `WorkingDirectory=` line (see §7).
* **Machine-specific absolute paths** (`/home/sbeppi/…`, `/srv/minisa/…`,
  `/etc/systemd/system/…`) — they are the substance of the deployment procedure and
  cannot be removed without making the runbook unusable. Flagged for review in §7.

---

## 4. Secret scan

### 4.1 Method

No secret-scanning tool is installed on this host, and none was installed (installing
tools and uploading files to third-party scanners are both disallowed):

| Tool | Status |
| --- | --- |
| `gitleaks`, `trufflehog`, `detect-secrets`, `shhgit`, `git-secrets` | not installed |
| Python `detect_secrets`, `bandit`, `pipenv` modules | not installed |
| `rg`, `ag` | not installed |

**Method used instead:** a local, in-process pattern scan (stdlib `re`) over exactly
the 130 files that would be imported, covering 27 categories: private-key and
OpenSSH-key blocks, AWS access-key IDs and secret keys, GitHub/GitLab PATs, Slack
tokens and webhooks, Discord webhooks, Stripe/Twilio/SendGrid/Telegram/OpenAI/
Anthropic/Google/HuggingFace keys, JSON web tokens, literal `Bearer` headers,
credentials embedded in URLs, long base64/hex blobs, literal password/secret/token
assignments, e-mail addresses, phone-like numbers and card-like digit strings.
Separately, a filename sweep looked for `*.db`, `*.db-*`, `*.sqlite*`, `*-wal`,
`*-shm`, `*.bak`, `*.tar*`, `*.zip`, `*.gz`, `.env*`, `*.pem`, `*.key`, `*.crt`,
`*.p12`, `*.pfx`, `id_rsa*`, `*credential*`, `*secret*`, `*token*`, `*password*`,
`*.toml`, `*.ini`, `*.cfg`, `*.yaml`, `*.yml`, `*.json`, `*.conf`.

### 4.2 Results

**No secrets, credentials, private keys, tokens or e-mail addresses were found.**
No secret value was printed, copied into a file, or included in this report.

| Category | Hits | Note |
| --- | --- | --- |
| Private / SSH key blocks | 0 | |
| AWS, GitHub, GitLab, Slack, Discord, Stripe, Twilio, SendGrid, Telegram, OpenAI, Anthropic, Google, HuggingFace keys | 0 | |
| JSON web tokens, literal `Bearer` headers, credential-bearing URLs | 0 | |
| Literal password / secret / api-key assignments | 0 | |
| E-mail addresses | 0 | |
| Long hex blobs (64 hex chars) | 35 | **False positives** — all are SHA-256 digests of files (e.g. the release manifest digest `410d865e…`, the `engine.py` digest, the drop-in digest) quoted in the reports |
| Phone-like digit strings | 8 | **False positives** — byte sizes such as `692 224` and section/line numbers |
| Card-like digit strings | 4 | **False positives** — UUIDs in test fixtures and a numeric constant in a SQL expression |

Two individual matches were inspected by hand:

* `runtime/research.py:159` — reads the API key from the environment at runtime
  (`env.get("TAVILY_API_KEY") or ""`). **No literal value is stored in the source.**
* `tests/test_deployment_preflight.py:229` — the string
  `minisa-recovery-20261021T000000Z`, a test-fixture capture directory name.

The credential-word sweep over all `.py` files found only: environment-variable
reads, `Authorization`/`Bearer` header construction, CLI option help text, and the
word "token" used as an NLP token in the retrieval tests.

### 4.3 Ignore rules verified to work

Checked with `git check-ignore -v` against the workspace (scratch git directory):

* **Correctly ignored:** `minisa.db`, `minisa.db-wal`, `minisa.db-shm`,
  `minisa.sqlite`, `minisa.sqlite3-journal`, `.env`, `.env.production`,
  `config.env`, `secrets/token.txt`, `credentials/db.ini`, `server.key`,
  `client.pem`, `cert.crt`, `keystore.p12`, `id_rsa`, `id_ed25519`, `.venv/`,
  `venv/`, `__pycache__/x.pyc`, `module.cpython-314.pyc`, `.pytest_cache/…`,
  `.mypy_cache/`, `.ruff_cache/`, `.coverage`, `htmlcov/`, `minisa.log`,
  `logs/server.log`, `*.pid`, `*.sock`, `*.tmp`, `minisa-recovery-*/manifest.json`,
  `minisa-recovery-*/SHA256SUMS`, `minisa-recovery-*/CAPTURE_VERIFIED`,
  `minisa-recovery-*/file-state.tar`, `minisa-recovery-*/file-manifest.json`,
  `backups/minisa.db`, `capture.tar.gz`, `dump.sql.gz`, `retrieval-eval.json`,
  `minisa-retrieval-report.json`, `build/`, `dist/`, `*.egg-info/`.
* **Correctly NOT ignored:** `core/backup.py`, `runtime/chat.py`,
  `tests/test_recovery_capture.py`, `ops/capture_minisa_recovery.py`, `core/db.py`,
  `core/migrations.py`, `runtime/cognition/engine.py`,
  `deploy/minisa-learning-cycle-timeout.conf.candidate`, `README.md`,
  `CONTRIBUTING.md`, `.gitignore`, and `.env.example` (a redacted template is
  explicitly un-ignored; verified in a scratch directory that `.env.example` is
  added while `.env` and `config.env` stay ignored).
* **No blanket rules** were used: no pattern such as `*backup*` or `*.json` could
  match source code. `core/backup.py` is source and stays tracked.

---

## 5. Definitive list of files in the initial import

Produced by `git add --dry-run -A` against the workspace (129 files before this
report existed; 130 with it). Byte counts are rounded to the nearest ~100 B because
this report measures itself.

```
ADMIN READ ME PLEASE I BEG.md
CONTRIBUTING.md
DEPLOYMENT_REHEARSAL_REPORT_20261021.md
DREAM_RECOVERY_RELEASE.md
ENGINEERING_REPORT.md
GITHUB_PREPARATION_REPORT_20261021.md
LEARNING_CYCLE_MILESTONE.md
LEARNING_RELEASE_CANDIDATE_20261021.md
MINISA_FIX_LIST.md
MINISA_PROJECT_STATUS.md
NEXT_ACTIONS.md
RECOVERY_READINESS_REPORT_20261021.md
RECOVERY_RECHECK_2026-10-18.md
RECOVERY_RUNBOOK.md
ROLLBACK_RUNBOOK_FIX_REPORT_20261021.md
SEMANTIC_RECALL_EXPERIMENT.md
WEB_RESEARCH.md
WORKING_ASSISTANT_ACCEPTANCE.md
.gitignore
core/__init__.py
core/app.py
core/backup.py
core/cognitive.py
core/db.py
core/migrations.py
deploy/minisa-dream-timeout.conf.candidate
deploy/minisa-learning-cycle-timeout.conf.candidate
ops/build_candidate_manifest.py
ops/capture_minisa_recovery.py
ops/measure_retrieval_quality.py
ops/preflight_learning_cycle_release.py
ops/verify_candidate_manifest.py
ops/verify_recovery_database.py
runtime/__init__.py
runtime/actions/__init__.py
runtime/actions/manager.py
runtime/belief_manager.py
runtime/chat.py
runtime/cleanup_memories.py
runtime/client.py
runtime/cognition/__init__.py
runtime/cognition/context.py
runtime/cognition/conversation.py
runtime/cognition/dream.py
runtime/cognition/dream_context.py
runtime/cognition/dream_request.py
runtime/cognition/dream_wire.py
runtime/cognition/engine.py
runtime/cognition/evidence.py
runtime/cognition/health.py
runtime/cognition/letta_adapter.py
runtime/cognition/persistence.py
runtime/cognition/reviews.py
runtime/cognition/router.py
runtime/cognition/turns.py
runtime/cognition/workspace.py
runtime/consolidate_creator_memory.py
runtime/core/loader.py
runtime/decision/__init__.py
runtime/decision/engine.py
runtime/experience_manager.py
runtime/filesystem/__init__.py
runtime/filesystem/filelist.py
runtime/filesystem/models.py
runtime/filesystem/operations.py
runtime/filesystem/safeguards.py
runtime/initialize_personality.py
runtime/main.py
runtime/memory/embeddings.py
runtime/memory/facts.py
runtime/memory/index.py
runtime/memory/manager.py
runtime/memory/normalizer.py
runtime/memory/retriever.py
runtime/memory/semantic_retriever.py
runtime/personality/__init__.py
runtime/personality/manager.py
runtime/personality/models.py
runtime/personality/updater.py
runtime/personality/validator.py
runtime/reasoning/base.py
runtime/reasoning/ollama/__init__.py
runtime/reasoning/ollama/reasoner.py
runtime/record_experience.py
runtime/record_reflection.py
runtime/reflection_manager.py
runtime/remove_bad_memory.py
runtime/research.py
runtime/research_cli.py
runtime/reset_personality.py
runtime/reasoning/__init__.py
runtime/self_model/__init__.py
runtime/self_model/manager.py
runtime/self_model/updater.py
runtime/self_model/validator.py
runtime/server/__init__.py
runtime/server/application.py
runtime/server/config.py
runtime/server/http.py
runtime/server/main.py
runtime/services/__init__.py
runtime/services/memory.py
runtime/services/shadow_recall.py
runtime/services/universal_memory_readonly.py
runtime/services/universal_recall.py
runtime/state/manager.py
runtime/storage.py
tests/retrieval_quality_eval.py
tests/test_concurrency_lifecycle.py
tests/test_config_hardening.py
tests/test_database_connections.py
tests/test_deployment_preflight.py
tests/test_dream_recovery.py
tests/test_embedding_provider.py
tests/test_experience_provenance.py
tests/test_fault_injection_recovery.py
tests/test_keyword_scoring.py
tests/test_learning_acceptance.py
tests/test_learning_cycle.py
tests/test_lifecycle_recovery.py
tests/test_memory_recall_fallback.py
tests/test_migration_chain.py
tests/test_normal_note_lookup.py
tests/test_recall_contract.py
tests/test_recovery_capture.py
tests/test_research.py
tests/test_retrieval_quality.py
tests/test_rollback_runbook.py
tests/test_shadow_recall.py
tests/test_universal_recall_backend.py
```

Not in the list, by rule: every `__pycache__/*.pyc` (121) and every
`.pytest_cache/**` file (4).

---

## 6. `CONTRIBUTING.md` — what it adds and what it does not claim

Added because the essentials were incomplete: the test command was documented in
several reports, but the supported Python version, the environment setup and the
safe-local-development rules were not.

It states, from evidence only:

* Python: verified on **CPython 3.14.8**; **3.11 minimum inferred** from stdlib
  `tomllib` usage — labelled as an inference, not a tested matrix.
* Dependencies: **not pinned**. There is no `pyproject.toml`, `setup.py`,
  `requirements*.txt`, `Pipfile` or lock file in this workspace, and the document
  says so instead of implying reproducibility. The only third-party runtime import
  is `fastapi`; `universal_memory` is optional and its tests skip when absent.
* Test commands: `python3 -m unittest discover -s tests`, single-module invocations,
  and `PYTHONWARNINGS=error::ResourceWarning`; last reported result **210 tests,
  `OK (skipped=6)`** on 2026-10-21 under both interpreters (not re-run during this
  task — the suite is read-only, but nothing in this task required it).
* Safe local development: never point anything at `/srv/minisa`, set
  `MINISA_DB_PATH` to a local path, the preflight/verification scripts are read-only
  and fail closed, the capture script is the only writer and needs root plus an
  independent destination, and the staged release is immutable until approved.

**No licence file was added** — there is none in the workspace and adding one is not
authorised. `CONTRIBUTING.md` states that no licence is included and that all rights
are reserved by default.

---

## 7. Files that need manual review before committing

None of these are secrets, and none is an automatic blocker for a **private**
repository. They are listed because they are machine- or person-specific.

| # | Item | Where | Why it needs a decision |
| --- | --- | --- | --- |
| 1 | Creator identifier `sbeppi` / "SBeppi is Minisa's creator" | `runtime/cognition/engine.py:91-96`, `runtime/cleanup_memories.py:26` | A personal identifier embedded in **behaviour**, not configuration. Rewriting it changes what the assistant answers, so it was deliberately left untouched. Parameterise it before the code is ever made public. |
| 2 | Same identifier in docs and paths | 15 `.md` documents, `ops/build_candidate_manifest.py:13-17`, `ops/preflight_learning_cycle_release.py:37-39`, `ops/capture_minisa_recovery.py:23` | `/home/sbeppi/…` paths and the creator name appear throughout the operational documentation. Acceptable in a private repo; review before any wider publication. |
| 3 | Hard-coded absolute paths | `ops/capture_minisa_recovery.py:23` (`ACTIVE_RELEASE`, **no CLI override**), `ops/preflight_learning_cycle_release.py:37-39` (overridable via `--candidate-root` / `--manifest`) | Host-specific and not portable. The capture script's path cannot be changed without editing code — left as is, flagged here. |
| 4 | Host-specific systemd drop-ins | `deploy/minisa-dream-timeout.conf.candidate`, `deploy/minisa-learning-cycle-timeout.conf.candidate` | The `WorkingDirectory=` line points at this host's release directories. They are templates; `CONTRIBUTING.md` says so. |
| 5 | Internal hostnames / IPs | `100.108.164.57`, `100.123.208.117` (in `NEXT_ACTIONS.md`, `RECOVERY_RUNBOOK.md`, reports, `runtime/`), `93.184.216.34` (a documentation example) | Internal infrastructure detail. Fine for a private repository. |
| 6 | `ops/measure_retrieval_quality.py` default output `/tmp/minisa-retrieval-report.json` | `ops/measure_retrieval_quality.py:163` | Writes outside the repository by default; the filename is ignored by `.gitignore`. |
| 7 | Filename with spaces and capitals | `ADMIN READ ME PLEASE I BEG.md` | Awkward in shell commands; kept because it is an operator document. Rename only if the owner wants to. |
| 8 | The 54 untracked `.pyc` files in the **staged release** | `/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021` | Outside this workspace and **untouched**. They stay byte-identical with the release; removing them is a separate, approved decision (readiness report §7). |

**Not required for the private import:** no database, capture, backup, log, `.env`,
key or certificate is present in the workspace at all.

---

## 8. Commands for a future initial commit and push — **not executed**

Run these only after reviewing this report. Adjust the owner and remote name.

```bash
cd /home/sbeppi/minisa-working-assistant-20261018/candidate

# 0. sanity check before initialising: nothing exists yet
git rev-parse --is-inside-work-tree   # expect: fatal: not a git repository

# 1. create the local repository (default branch name of your choice)
git init -b main

# 2. stage only what the ignore rules allow, and review before committing
git add -A
git status --short
git status --short --ignored        # the .pyc / .pytest_cache entries must be listed as ignored

# 3. optional: confirm the staged set is exactly the 130 files in section 5
git diff --cached --name-only | sort > /tmp/opencode/staged.txt
wc -l /tmp/opencode/staged.txt

# 4. commit
git commit -m "Minisa learning-cycle candidate workspace: source, tests, ops scripts, runbooks and reports"

# 5. create the PRIVATE repository on GitHub (in the browser or with gh),
#    then point at it and push
git remote add origin git@github.com:<OWNER>/<PRIVATE_REPO>.git
git push -u origin main

# 6. make sure the GitHub repository is private before the first push:
#    Settings -> General -> Danger Zone -> Change visibility -> Private
```

Notes for whoever runs this:

* Do **not** use `git add -f` on anything ignored. If a file should be tracked, fix
  the rule in `.gitignore` instead.
* Do not add a licence file without explicit authorisation.
* `git push` requires GitHub authentication; nothing was authenticated or pushed
  during this task.

---

## 9. Verification of untouched state

| Object | Expected | Observed |
| --- | --- | --- |
| Staged release manifest | `410d865e4ceb770bdc5bcf4669dc5bcf4669fb22f6db95c48155dca95e197a2109df909405` | unchanged (`410d865e4ceb770bdc5bcf4669fb22f6db95c48155dca95e197a2109df909405`) |
| Staged release tracked files | 20 matching the manifest | 20/20 match |
| Staged release `.pyc` files | 54, untouched | 54 present, untouched |
| Live database `/srv/minisa/data/minisa.db` | SHA-256 `8d7c11be…4508a0`, size 692224, mtime 2026-10-19 07:22:31 +0100 | identical — not read into the repository, not modified |
| `minisa.service` | active, PID 1324 | active, PID 1324 (not restarted) |
| `minisa-core.service` | active, PID 1007 | active, PID 1007 (not restarted) |
| `/etc/systemd/system/minisa.service.d/20-dream-recovery.conf` | absent | absent (no drop-in installed) |
| Candidate workspace files | 127 pre-existing source/doc files | 127 pre-existing files untouched; 3 files added; 0 deleted |
| Deployment, migration, capture, service management | not run | not run |

The only command executed against the live database was the read-only
`sha256sum`/`stat` shown above; no query was run against its contents and none of
its data is in the repository.

The scratch git directory used for the dry-run checks is `/tmp/opencode/ignorecheck`
(outside the workspace, no index entries). Delete it with
`rm -rf /tmp/opencode/ignorecheck` when convenient.

---

## 10. Confirmations

* **No GitHub repository was created.** No remote was created on GitHub or anywhere else.
* **Nothing was pushed.** No push of any kind was attempted.
* **No authentication to GitHub was performed.** No credential was read, stored or
  printed.
* **No secret value appears in this report, in `.gitignore`, in `CONTRIBUTING.md`, or
  in any file in the workspace.** The scan in §4 reports filenames and categories
  only.
* **No deployment, migration, capture or service-management command was run.** The
  production database, production configuration and the two services are unchanged.
* **The staged release and its manifest are byte-identical** to their pre-task state.
* **No existing file was deleted, renamed, rewritten or overwritten**, and no Git
  history was created, rewritten or destroyed.
* **No licence was added.**
* The workspace is **not** yet a Git repository; the commands in §8 will make it one.
