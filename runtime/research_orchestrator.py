"""Dream-driven research orchestrator (candidate-only, opt-in)."""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.db import Database
from runtime import research
from runtime.memory.manager import MemoryManager


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name, "").strip()
    if not val:
        return default
    try:
        v = int(val)
        return v if v >= 0 else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name, "").strip()
    if not val:
        return default
    try:
        v = float(val)
        return v
    except ValueError:
        return default


def _normalize_query(subject: str) -> str:
    s = " ".join(subject.split())
    if len(s) > 300:
        s = s[:300]
    return s


def _query_hash(q: str) -> str:
    return hashlib.sha256(q.encode("utf-8")).hexdigest()[:16]


class ResearchOrchestrator:
    def __init__(self, root: Path, db_path: Path | str):
        self.root = Path(root)
        self.db = Database(db_path)
        self.memory_manager = MemoryManager(self.root)
        self._ensure_state_tables()

    def _ensure_state_tables(self):
        with self.db.transaction(immediate=True) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS research_triggers (
                unit_id TEXT PRIMARY KEY, trigger_type TEXT NOT NULL, normalized_query_hash TEXT NOT NULL,
                first_seen_at REAL NOT NULL, last_evaluated_at REAL NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('eligible','skipped','triggered','blocked'))
            ) STRICT""")
            conn.execute("""CREATE TABLE IF NOT EXISTS research_cooldowns (
                key_type TEXT NOT NULL, key_id TEXT NOT NULL, normalized_hash TEXT NOT NULL,
                last_run_at REAL NOT NULL, PRIMARY KEY(key_type, key_id, normalized_hash)
            ) STRICT""")
            conn.execute("""CREATE TABLE IF NOT EXISTS research_quota_windows (
                window_key TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0 CHECK(count >= 0),
                window_start REAL NOT NULL, exhausted_until REAL
            ) STRICT""")
            conn.execute("""CREATE TABLE IF NOT EXISTS research_dedup (
                source_url_hash TEXT PRIMARY KEY, note_path TEXT NOT NULL,
                first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL
            ) STRICT""")

    def config(self) -> dict[str, Any]:
        return {
            "enabled": _env_bool("MINISA_RESEARCH_ENABLED", False),
            "provider": os.environ.get("MINISA_RESEARCH_PROVIDER", "").strip() or None,
            "max_per_hour": _env_int("MINISA_RESEARCH_MAX_PER_HOUR", 1),
            "max_per_day": _env_int("MINISA_RESEARCH_MAX_PER_DAY", 3),
            "per_unit_cooldown_hours": _env_int("MINISA_RESEARCH_PER_UNIT_COOLDOWN_HOURS", 24),
            "max_fetches_per_task": _env_int("MINISA_RESEARCH_MAX_FETCHES_PER_TASK", 2),
            "q_importance_min": _env_float("MINISA_RESEARCH_QUESTION_IMPORTANCE_MIN", 0.6),
            "q_uncertainty_min": _env_float("MINISA_RESEARCH_QUESTION_UNCERTAINTY_MIN", 0.4),
            "c_importance_min": _env_float("MINISA_RESEARCH_CONTRADICTION_IMPORTANCE_MIN", 0.7),
            "c_uncertainty_min": _env_float("MINISA_RESEARCH_CONTRADICTION_UNCERTAINTY_MIN", 0.6),
        }

    def _check_and_increment_quota(self, conn, cfg: dict[str, Any]) -> bool:
        if cfg["max_per_hour"] <= 0 or cfg["max_per_day"] <= 0:
            return False
        now = time.time()
        hour_key = f"hour:{datetime.fromtimestamp(now, tz=timezone.utc).strftime('%Y%m%d%H')}"
        day_key = f"day:{datetime.fromtimestamp(now, tz=timezone.utc).strftime('%Y%m%d')}"
        conn.execute("INSERT OR IGNORE INTO research_quota_windows(window_key,count,window_start) VALUES(?,?,?)",
                     (hour_key, 0, now))
        cur = conn.execute("UPDATE research_quota_windows SET count = count + 1 WHERE window_key = ? AND count < ?",
                           (hour_key, cfg["max_per_hour"]))
        if cur.rowcount == 0:
            return False
        conn.execute("INSERT OR IGNORE INTO research_quota_windows(window_key,count,window_start) VALUES(?,?,?)",
                     (day_key, 0, now))
        cur = conn.execute("UPDATE research_quota_windows SET count = count + 1 WHERE window_key = ? AND count < ?",
                           (day_key, cfg["max_per_day"]))
        if cur.rowcount == 0:
            conn.execute("UPDATE research_quota_windows SET count = count - 1 WHERE window_key = ?", (hour_key,))
            return False
        return True

    def _check_cooldown(self, conn, unit_id: str, qhash: str, cfg: dict[str, Any]) -> bool:
        now = time.time()
        cooldown = cfg["per_unit_cooldown_hours"] * 3600
        row = conn.execute("SELECT last_run_at FROM research_cooldowns WHERE key_type='unit' AND key_id=? AND normalized_hash=?",
                          (unit_id, qhash)).fetchone()
        if row and (now - row["last_run_at"]) < cooldown:
            return False
        return True

    def _update_cooldown(self, conn, unit_id: str, qhash: str):
        now = time.time()
        conn.execute("INSERT OR IGNORE INTO research_cooldowns(key_type,key_id,normalized_hash,last_run_at) VALUES('unit',?,?,?)",
                     (unit_id, qhash, now))
        conn.execute("UPDATE research_cooldowns SET last_run_at=? WHERE key_type='unit' AND key_id=? AND normalized_hash=?",
                     (now, unit_id, qhash))

    def _audit(self, conn, operation: str, target_id: str | None, details: dict[str, Any]):
        try:
            from core.db import Database
            safe = {k: v for k, v in details.items() if k not in ('key', 'api_key', 'token', 'secret', 'authorization')}
            Database.append_audit(conn, actor="research_orchestrator", operation=operation,
                                  target_category="research", target_id=target_id, details=safe)
        except Exception:
            pass

    def _eligible_units(self, conn, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        units = []
        for row in conn.execute("SELECT * FROM workspace_units WHERE kind='question' AND status!='resolved' ORDER BY importance DESC, created_at ASC"):
            r = dict(row)
            if float(r.get("importance", 0)) >= cfg["q_importance_min"] and float(r.get("uncertainty", 0)) >= cfg["q_uncertainty_min"]:
                units.append({**r, "trigger_type": "question"})
        for row in conn.execute("SELECT * FROM workspace_units WHERE kind='contradiction' AND status='active' ORDER BY importance DESC, created_at ASC"):
            r = dict(row)
            if float(r.get("importance", 0)) >= cfg["c_importance_min"] and float(r.get("uncertainty", 0)) >= cfg["c_uncertainty_min"]:
                units.append({**r, "trigger_type": "contradiction"})
        return units

    def run_once(self, max_tasks: int = 1) -> dict[str, Any]:
        cfg = self.config()
        if not cfg["enabled"]:
            return {"status": "disabled", "ran": 0}
        results = []
        tasks_run = 0
        with self.db.transaction(immediate=True) as conn:
            if not self._check_and_increment_quota(conn, cfg):
                self._audit(conn, "research.skipped", None, {"reason": "quota"})
                return {"status": "quota_exhausted", "ran": 0, "results": []}
            units = self._eligible_units(conn, cfg)
            for unit in units:
                if tasks_run >= max_tasks:
                    break
                unit_id = unit["unit_id"]
                subject = str(unit.get("subject", ""))
                query = _normalize_query(subject)
                qhash = _query_hash(query)
                if not query or not self._check_cooldown(conn, unit_id, qhash, cfg):
                    continue
                try:
                    search_results = research.search_web(query)
                    if not search_results:
                        self._audit(conn, "research.skipped", unit_id, {"reason": "no_results", "query_hash": qhash})
                        continue
                    saved_paths = []
                    fetches = 0
                    for res in search_results[: cfg["max_fetches_per_task"]]:
                        url = res.get("url")
                        if not url or fetches >= cfg["max_fetches_per_task"]:
                            break
                        try:
                            page = research.extract_page(url)
                            text = page.get("text", "")
                            cleaned = " ".join(text.split())
                            if not cleaned or len(cleaned) < 10:
                                continue
                            summary = cleaned[:300]
                            sentences = [s.strip() for s in cleaned.replace("\n", " ").split(".") if s.strip() and len(s.strip()) > 10][:3]
                            key_points = [s[:200] for s in sentences if s][:3]
                            def make_fetcher(p):
                                def f(u): return p
                                return f
                            saved = research.save_research(self.root, url, summary, key_points=key_points,
                                                          title=page.get("title"), fetcher=make_fetcher(page))
                            saved_paths.append(str(saved))
                            fetches += 1
                        except research.ResearchError:
                            continue
                        except Exception:
                            continue
                    if saved_paths:
                        self._update_cooldown(conn, unit_id, qhash)
                        self._audit(conn, "research.saved", unit_id, {"query_hash": qhash, "count": len(saved_paths), "trigger_type": unit.get("trigger_type")})
                        results.append({"unit_id": unit_id, "trigger_type": unit.get("trigger_type"), "saved": saved_paths})
                        tasks_run += 1
                    else:
                        self._update_cooldown(conn, unit_id, qhash)
                        self._audit(conn, "research.skipped", unit_id, {"reason": "no_saved", "query_hash": qhash})
                except research.ResearchError as e:
                    self._audit(conn, "research.failed", unit_id, {"error_category": str(type(e).__name__), "query_hash": qhash})
                    continue
                except Exception as e:
                    self._audit(conn, "research.failed", unit_id, {"error_category": "internal", "query_hash": qhash})
                    continue
        return {"status": "ok", "ran": len(results), "results": results}
