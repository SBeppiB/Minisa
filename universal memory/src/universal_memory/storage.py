"""SQLite source of truth, FTS fallback, history and transactional index outbox."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from .embeddings import tokens
from .models import Memory, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
 id TEXT PRIMARY KEY, namespace TEXT NOT NULL, content TEXT NOT NULL,
 normalized TEXT NOT NULL, tier TEXT NOT NULL, updated_at TEXT NOT NULL,
 data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_scope ON memories(namespace,tier,id);
CREATE INDEX IF NOT EXISTS memories_exact ON memories(namespace,normalized);
CREATE TABLE IF NOT EXISTS entities (
 namespace TEXT NOT NULL, entity TEXT NOT NULL, memory_id TEXT NOT NULL
 REFERENCES memories(id) ON DELETE CASCADE, PRIMARY KEY(namespace,entity,memory_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content, content='memories', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS memories_insert AFTER INSERT ON memories BEGIN
 INSERT INTO memory_fts(rowid,content) VALUES(new.rowid,new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_update AFTER UPDATE OF content ON memories
WHEN old.content != new.content BEGIN
 INSERT INTO memory_fts(memory_fts,rowid,content) VALUES('delete',old.rowid,old.content);
 INSERT INTO memory_fts(rowid,content) VALUES(new.rowid,new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_delete AFTER DELETE ON memories BEGIN
 INSERT INTO memory_fts(memory_fts,rowid,content) VALUES('delete',old.rowid,old.content);
END;
CREATE TABLE IF NOT EXISTS history (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL
 REFERENCES memories(id) ON DELETE CASCADE, action TEXT NOT NULL,
 at TEXT NOT NULL, snapshot TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_memory ON history(memory_id,seq);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
 source TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 target TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, PRIMARY KEY(source,target,kind)
);
CREATE TABLE IF NOT EXISTS consolidations (
 id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
 namespace TEXT NOT NULL, data TEXT NOT NULL, reversed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS recall_logs (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL,
 at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
PRAGMA user_version=1;
"""


def normalized(content: str) -> str:
    return " ".join(content.casefold().split())


class SQLiteStore:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            self.db.close()
            raise RuntimeError(f"Database schema {version} is newer than supported version 2")
        if version == 0:
            self.db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")

        if version < 2:
            self.db.executescript("""BEGIN IMMEDIATE;
                CREATE INDEX IF NOT EXISTS entities_memory ON entities(memory_id);
                CREATE INDEX IF NOT EXISTS links_target ON links(target);
                CREATE INDEX IF NOT EXISTS logs_scope ON recall_logs(namespace,seq);
                CREATE INDEX IF NOT EXISTS outbox_scope ON outbox(namespace);
                PRAGMA user_version=2;
                COMMIT;""")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, id: str, namespace: str) -> Memory:
        row = self.db.execute(
            "SELECT data FROM memories WHERE id=? AND namespace=?", (id, namespace)
        ).fetchone()
        if row is None:
            raise KeyError(id)
        return Memory.model_validate_json(row[0])

    def save(self, memory: Memory, action: str, *, index: bool = True) -> None:
        memory.updated_at = now()
        if index:
            memory.index_status = "pending"
        data = memory.model_dump_json()
        self.db.execute(
            """INSERT INTO memories VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET content=excluded.content,
            normalized=excluded.normalized,tier=excluded.tier,
            updated_at=excluded.updated_at,data=excluded.data""",
            (
                memory.id,
                memory.namespace,
                memory.content,
                normalized(memory.content),
                memory.tier.value,
                memory.updated_at.isoformat(),
                data,
            ),
        )
        self.db.execute("DELETE FROM entities WHERE memory_id=?", (memory.id,))
        self.db.executemany(
            "INSERT INTO entities VALUES(?,?,?)",
            [(memory.namespace, e, memory.id) for e in set(memory.entity_ids)],
        )
        self.db.execute(
            "INSERT INTO history(memory_id,action,at,snapshot) VALUES(?,?,?,?)",
            (memory.id, action, now().isoformat(), data),
        )
        if index:
            self.db.execute(
                "INSERT OR REPLACE INTO outbox VALUES(?,?,?)",
                (memory.id, memory.namespace, "upsert"),
            )

    def list(
        self,
        namespace: str,
        *,
        tier: str | None = None,
        entity_id: str | None = None,
        tag: str | None = None,
        category: str | None = None,
        after: str = "",
        limit: int = 100,
    ) -> list[Memory]:
        clauses, args = ["namespace=?", "id>?"], [namespace, after]
        if tier:
            clauses.append("tier=?")
            args.append(tier)
        if entity_id:
            clauses.append("id IN (SELECT memory_id FROM entities WHERE namespace=? AND entity=?)")
            args.extend([namespace, entity_id])
        if tag:
            clauses.append("EXISTS(SELECT 1 FROM json_each(data,'$.tags') WHERE value=?)")
            args.append(tag)
        if category:
            clauses.append("json_extract(data,'$.category')=?")
            args.append(category)
        rows = self.db.execute(
            "SELECT data FROM memories WHERE " + " AND ".join(clauses) + " ORDER BY id LIMIT ?",
            [*args, limit],
        ).fetchall()
        return [Memory.model_validate_json(row[0]) for row in rows]

    def lexical(
        self,
        query: str,
        namespace: str,
        tiers: list[str],
        limit: int,
        entity_ids: list[str] | None = None,
    ) -> list[str]:
        words = list(dict.fromkeys(tokens(query)))[:64]
        if not words:
            return []
        expression = " OR ".join('"' + word + '"' for word in words)
        placeholders = ",".join("?" for _ in tiers)
        entity_clause = ""
        entity_args = []
        if entity_ids:
            entity_clause = (
                " AND m.id IN (SELECT memory_id FROM entities WHERE namespace=? AND entity IN ("
                + ",".join("?" for _ in entity_ids)
                + "))"
            )
            entity_args = [namespace, *entity_ids]
        rows = self.db.execute(
            f"""SELECT m.id FROM memory_fts f JOIN memories m ON m.rowid=f.rowid
            WHERE memory_fts MATCH ? AND m.namespace=? AND m.tier IN ({placeholders})
            {entity_clause} ORDER BY rank LIMIT ?""",
            [expression, namespace, *tiers, *entity_args, limit],
        )
        return [row[0] for row in rows]

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(destination)) as target:
            self.db.backup(target)

    def history(self, id: str, namespace: str) -> list[dict]:
        self.get(id, namespace)
        return [
            {"action": r[0], "at": r[1], "memory": json.loads(r[2])}
            for r in self.db.execute(
                "SELECT action,at,snapshot FROM history WHERE memory_id=? ORDER BY seq", (id,)
            )
        ]

    def close(self) -> None:
        self.db.close()
