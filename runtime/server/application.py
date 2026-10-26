"""Long-lived application service independent of the HTTP transport."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
import time
from runtime.storage import WriterAuthority

from runtime.cognition.engine import CognitionEngine
from runtime.reasoning.ollama.reasoner import OllamaReasoner
from runtime.server.config import ServerConfig
from runtime.services.memory import LocalMemoryService, MemoryService


class MinisaApplication:
    def __init__(
        self,
        config: ServerConfig,
        memory: MemoryService | None = None,
        engine: CognitionEngine | None = None,
    ):
        self.config = config
        if memory is not None:
            self.memory = memory
        else:
            base_memory = LocalMemoryService(
                config.root, ollama_url=config.ollama_url, embedding_model=config.embedding_model
            )
            # Explicit opt-in only: ordinary local recall is the default and
            # shadow mode never selects this backend. Because the operator
            # explicitly required Universal Memory, a setup failure fails
            # startup instead of silently serving a different backend.
            if config.recall_backend == "universal":
                from runtime.services.universal_recall import attach_universal_recall

                self.memory = attach_universal_recall(base_memory, config)
                logging.getLogger(__name__).info(
                    "Universal recall backend enabled (explicit opt-in)"
                )
            else:
                self.memory = base_memory
        # Shadow recall is evaluation-only, disabled by default. Failures during
        # setup or recall must never affect the normal memory path.
        self.shadow_evaluator = None
        if config.shadow_recall_enabled:
            try:
                from runtime.services.shadow_recall import attach_shadow_recall

                self.memory, self.shadow_evaluator = attach_shadow_recall(self.memory, config)
                logging.getLogger(__name__).info("Shadow recall enabled (evaluation only)")
            except Exception as error:
                logging.getLogger(__name__).warning(
                    "Shadow recall disabled after setup failure: %s", error
                )
        reasoner = OllamaReasoner(model=config.model, host=config.ollama_url)
        self.engine = engine if engine is not None else CognitionEngine(
            config.root, reasoner, self.memory
        )
        self._lock = threading.Lock()
        self._authority = WriterAuthority(config.root)
        self._started_at: datetime | None = None
        self._started_monotonic: float | None = None
        self._requests = 0
        self._errors = 0
        self._stopping = False
        self.workspace = None
        self.dream_worker = None
        if config.workspace_database:
            from core.db import Database
            from runtime.cognition.workspace import Workspace
            self.workspace = Workspace(Database(config.workspace_database), config.root)
            self.engine.workspace = self.workspace
            from runtime.cognition.dream import DreamWorker, DreamModel
            from runtime.cognition.dream_wire import CompactDreamModel
            model = CompactDreamModel(config.dream_host, config.dream_model, timeout=config.dream_timeout_seconds, tokens=512) if config.dream_model else None
            deep=DreamModel(config.dream_deep_host,config.dream_deep_model,level=2) if config.dream_deep_model else None
            self.dream_worker = DreamWorker(self.workspace, model, deep_model=deep,deep_budget=config.dream_deep_calls_per_hour,ingest=self._index_turns)

        # Runtime configuration is authoritative capability evidence, not model self-report.
        self.engine.user_profile_scope = config.user_profile_scope
        self.engine.runtime_capabilities = {
            "conversation": True,
            "explicit_memory_commands": ["remember this:", "forget"],
            "memory_inspection": True,
            "external_actions": [],
            "dreaming": {"configured": self.dream_worker is not None,
                         "background": True, "user_command": False},
        }

    def _index_turns(self):
        if self.workspace and hasattr(self.engine, "turns"):
            db=self.workspace.db
            for _ in range(64):
                if self._stopping: break
                job=db.lease_job('workspace-index',job_type='workspace_index')
                if not job: break
                path=self.config.root/job['payload']['path']
                try:
                    record=self.engine.turns.read(path)
                    self.workspace.ingest_turn(record,path)
                    db.finish_job(job['job_id'],'workspace-index','completed')
                except (ValueError,OSError) as error:
                    db.finish_job(job['job_id'],'workspace-index','review_required',error=type(error).__name__)

    def start(self) -> None:
        self._authority.acquire()
        try:
            if self.workspace:
                # Production schema migration is an operator step, never a chat side effect.
                from core.migrations import SUPPORTED_SCHEMA_VERSION
                if self.workspace.db.status()["schema_version"] != SUPPORTED_SCHEMA_VERSION:
                    raise RuntimeError("Workspace requires reviewed Core migration before startup")
            if hasattr(self.engine, "turns"):
                self.engine.turns.recover()
            if self.workspace and hasattr(self.engine,'turns'):
                self.engine.turns.workspace=self.workspace
                # One startup reconciliation recovers publication/outbox crash gaps.
                with self.workspace.db.connect(readonly=True) as conn:
                    known={r[0] for r in conn.execute('SELECT evidence_id FROM workspace_evidence')}
                for path in self.engine.turns.paths():
                    if path.stem not in known:
                        record=self.engine.turns.read(path)
                        if record['status']!='in_progress': self.workspace.enqueue_turn(path)
            self._index_turns()
        except BaseException:
            self._authority.release()
            raise
        self._started_at = datetime.now(timezone.utc)
        self._started_monotonic = time.monotonic()
        self._stopping = False
        if self.dream_worker:
            self.dream_worker.start()

    def stop(self) -> None:
        self._stopping = True
        if self.dream_worker:
            self.dream_worker.stop()
        with self._lock:
            self._authority.release()

    def chat(self, message: str, *, provenance: str = 'user') -> dict:
        if self._started_at is None or self._stopping:
            raise RuntimeError("Minisa is not accepting requests.")
        with self._lock:
            if self._stopping:
                raise RuntimeError("Minisa is not accepting requests.")
            self._requests += 1
            try:
                result = self.engine.process(message, provenance=provenance)
                if self.workspace:
                    try:
                        self._index_turns()
                        if result.get("turn_status") in {"failed", "interrupted"}:
                            self.workspace.interrupt("failed_turn", "failure:"+result["turn_id"], result["turn_id"], .9)
                        if self.config.dream_model:
                            self.workspace.schedule()
                    except Exception as error:
                        logging.getLogger("minisa.workspace").error("index_failed category=%s", type(error).__name__)
                        result["workspace_receipt"] = {"status":"deferred","category":type(error).__name__}
            except Exception:
                self._errors += 1
                raise
        return {
            "turn_id": result.get("turn_id"),
            "turn_status": result.get("turn_status"),
            "turn_receipt": result.get("turn_receipt"),
            "response": result.get("response", ""),
            "route": result.get("route"),
            "action": result.get("action"),
            "experience_receipt": result.get("experience_receipt"),
            "reflection_receipt": result.get("reflection_receipt"),
            "persistence_receipt": result.get("persistence_receipt", {"status": "no_changes", "committed": False}),
        }

    def cognitive_health(self):
        if not self.workspace:
            return {"status":"disabled"}
        from runtime.cognition.health import diagnose
        return diagnose(self.workspace,model_enabled=bool(self.config.dream_model))

    def dream_status(self):
        """Small private-data-free dream summary for the desktop client."""
        if not self.workspace:
            return {'configured': False, 'status': 'disabled', 'last_success': None,
                    'pending_jobs': 0, 'unprocessed_evidence': 0, 'recent_failures': []}
        with self.workspace.db.connect(readonly=True) as conn:
            state = dict(conn.execute('SELECT * FROM dream_state').fetchone())
            pending = conn.execute("SELECT count(*) FROM jobs WHERE job_type='dream' AND status IN ('pending','leased')").fetchone()[0]
            unprocessed = conn.execute('SELECT count(*) FROM workspace_evidence WHERE processed=0').fetchone()[0]
            last = conn.execute('SELECT source_revision,resulting_revision,result_json,created_at FROM dream_results ORDER BY created_at DESC LIMIT 1').fetchone()
            failures = conn.execute("SELECT error FROM jobs WHERE job_type='dream' AND status='failed' ORDER BY created_at DESC LIMIT 5").fetchall()
        successful = None
        if last:
            import json
            result = json.loads(last['result_json'])
            successful = {'at': datetime.fromtimestamp(last['created_at'], timezone.utc).isoformat(),
                          'source_revision': last['source_revision'],
                          'resulting_revision': last['resulting_revision'],
                          'records_considered': result.get('records_considered'),
                          'accepted_changes': len(result.get('accepted', [])) if isinstance(result.get('accepted'), list) else 0}
        def timestamp(value):
            return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None
        safe_categories = {'timeout', 'invalid_output', 'invalid_proposal', 'writer_busy',
                           'deferred', 'model_unavailable', 'internal'}
        recent_failures = [r['error'] if r['error'] in safe_categories else 'other' for r in failures]
        last_failure = state['last_failure_category']
        if last_failure not in safe_categories:
            last_failure = 'other' if last_failure else None
        now = self.workspace.clock()
        if not self.config.dream_model:
            work_status = 'disabled'
        elif state['recovery_after'] > now:
            work_status = 'recovery_circuit'
        elif state['next_allowed'] > now:
            work_status = 'cooldown'
        elif pending:
            work_status = 'pending'
        elif unprocessed:
            work_status = 'awaiting_schedule'
        else:
            work_status = 'idle'
        return {'configured': bool(self.config.dream_model),
                'configured_model': self.config.dream_model or None,
                'status': work_status,
                'last_success': successful, 'pending_jobs': pending,
                'unprocessed_evidence': unprocessed,
                'next_allowed': timestamp(state['next_allowed']),
                'recovery_after': timestamp(state['recovery_after']),
                'failure_count': state['failures'],
                'last_failure_category': last_failure,
                'recent_failures': recent_failures}

    def health(self) -> dict:
        return {"status": "stopping" if self._stopping else "ok", "service": "minisa"}

    def status(self) -> dict:
        uptime = 0.0
        if self._started_monotonic is not None:
            uptime = max(0.0, time.monotonic() - self._started_monotonic)
        return {
            "status": "stopping" if self._stopping else "running",
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "uptime_seconds": round(uptime, 3),
            "requests": self._requests,
            "errors": self._errors,
            "model": self.config.model,
            "memory": self.memory.status(),
            "workspace": {"enabled": bool(self.workspace), "dreaming": bool(self.config.dream_model)},
        }
