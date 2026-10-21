from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException

from . import __version__
from .db import Database
from .migrations import SUPPORTED_SCHEMA_VERSION

DB_PATH = os.environ.get("MINISA_DB_PATH", "/srv/minisa/data/minisa.db")
database = Database(DB_PATH)
app = FastAPI(title="Minisa Core", version=__version__)


@app.on_event("startup")
def startup() -> None:
    database.initialize()


@app.get("/health")
def health() -> dict:
    # Preserve the original keys and extend with non-sensitive persistence state.
    try:
        state = database.status()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {
        "status": "ok",
        "service": "minisa-core",
        "database": DB_PATH,
        **state,
        "supported_schema_version": SUPPORTED_SCHEMA_VERSION,
    }


@app.get("/ready")
def ready() -> dict:
    try:
        state = database.status()
        return {
            "status": "ready",
            "service": "minisa-core",
            **state,
            "supported_schema_version": SUPPORTED_SCHEMA_VERSION,
            "migration_compatible": state["schema_version"] == SUPPORTED_SCHEMA_VERSION,
            "job_queue_available": True,
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail="not ready") from exc


def _record(row) -> dict:
    value = dict(row)
    value["metadata"] = json.loads(value.pop("metadata_json"))
    return value


@app.get("/v1/cognitive-records")
def list_cognitive_records(
    category: str | None = None,
    authority: str | None = None,
    owner_kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    clauses, values = [], []
    for column, value in (("category", category), ("authority", authority), ("owner_kind", owner_kind)):
        if value is not None:
            clauses.append(f"{column}=?")
            values.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with database.connect(readonly=True) as conn:
        total = conn.execute("SELECT count(*) FROM cognitive_records" + where, values).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM cognitive_records" + where + " ORDER BY imported_revision,source_path LIMIT ? OFFSET ?",
            (*values, limit, offset),
        ).fetchall()
    return {"total": total, "records": [_record(row) for row in rows]}


@app.get("/v1/cognitive-records/{record_id}")
def get_cognitive_record(record_id: str) -> dict:
    with database.connect(readonly=True) as conn:
        row = conn.execute("SELECT * FROM cognitive_records WHERE record_id=?", (record_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="record not found")
    return _record(row)


@app.get("/v1/memories/search")
def search_memories(q: str, limit: int = 5) -> dict:
    limit = max(1, min(limit, 50))
    terms = [term for term in q.lower().split() if term]
    with database.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM cognitive_records WHERE category='memory' AND authority='authoritative' ORDER BY source_path"
        ).fetchall()
    scored = []
    for row in rows:
        content = row["raw_content"].lower()
        score = sum(term in content for term in terms)
        if score:
            scored.append((score, row["source_path"], row))
    scored.sort(key=lambda value: (-value[0], value[1]))
    return {"query": q, "records": [_record(row) for _, _, row in scored[:limit]]}
