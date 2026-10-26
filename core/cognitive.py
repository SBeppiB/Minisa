from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


MIGRATION_NAMESPACE = uuid.UUID("9fc45a4c-6c58-4fa7-b87b-2f341afbb3c9")
SOURCE_NAME = "main-pc-runtime-root"


SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS cognitive_records (
    record_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    owner_kind TEXT NOT NULL CHECK(owner_kind IN ('minisa','user')),
    owner_key TEXT,
    authority TEXT NOT NULL CHECK(authority IN ('authoritative','historical','non_authoritative')),
    legacy_label TEXT,
    source_name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL UNIQUE,
    raw_content TEXT NOT NULL,
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
    source_created_at TEXT,
    imported_at TEXT NOT NULL,
    imported_revision INTEGER NOT NULL CHECK(imported_revision > 0),
    migration_id TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS cognitive_records_category_idx
    ON cognitive_records(category, authority);
CREATE INDEX IF NOT EXISTS cognitive_records_legacy_label_idx
    ON cognitive_records(legacy_label);
CREATE INDEX IF NOT EXISTS cognitive_records_owner_idx
    ON cognitive_records(owner_kind, owner_key);
CREATE TABLE IF NOT EXISTS cognitive_migrations (
    migration_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL UNIQUE,
    source_root TEXT NOT NULL,
    inventory_json TEXT NOT NULL CHECK(json_valid(inventory_json)),
    record_count INTEGER NOT NULL CHECK(record_count >= 0),
    applied_at TEXT NOT NULL,
    global_revision INTEGER NOT NULL CHECK(global_revision > 0)
) STRICT;
"""


@dataclass(frozen=True)
class InventoryItem:
    category: str
    owner_kind: str
    owner_key: str | None
    authority: str
    legacy_label: str | None
    source_path: str
    sha256: str
    size_bytes: int
    metadata: dict[str, Any]
    source_created_at: str | None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _markdown_metadata(text: str) -> tuple[dict[str, Any], str | None]:
    metadata: dict[str, Any] = {}
    for line in text.splitlines():
        match = re.match(r"^(ID|Type|Subject|Source|Status|Confidence|Created|Timestamp):\s*(.*)$", line)
        if match:
            # Preserve names and values exactly; absence remains absence.
            metadata[match.group(1)] = match.group(2)
    created = metadata.get("Created") or metadata.get("Timestamp")
    return metadata, created


def _item(root: Path, path: Path, *, category: str, authority: str,
          legacy_label: str | None = None, owner_kind: str = "minisa",
          owner_key: str | None = None, extra: dict[str, Any] | None = None) -> InventoryItem:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    metadata, created = _markdown_metadata(text)
    if extra:
        metadata.update(extra)
    return InventoryItem(category, owner_kind, owner_key, authority, legacy_label,
                         path.relative_to(root).as_posix(), _sha(raw), len(raw), metadata, created)


def build_inventory(root: Path) -> dict[str, Any]:
    root = root.resolve()
    items: list[InventoryItem] = []
    exclusions: list[dict[str, str]] = []

    for name in ("identity", "values", "goals", "personality", "instructions"):
        path = root / "CORE" / f"{name}.md"
        if path.is_file() and path.stat().st_size:
            items.append(_item(root, path, category="core_document", authority="authoritative",
                               legacy_label=name, extra={"document_kind": name}))

    for path in sorted((root / "MEMORY" / "active").glob("memory_*.md")):
        label_match = re.match(r"(memory_\d+)", path.name)
        items.append(_item(root, path, category="memory", authority="authoritative",
                           legacy_label=label_match.group(1) if label_match else path.stem,
                           extra={"memory_status": "active"}))
    for path in sorted((root / "MEMORY" / "archive").glob("memory_*.md")):
        label_match = re.match(r"(memory_\d+)", path.name)
        items.append(_item(root, path, category="memory", authority="historical",
                           legacy_label=label_match.group(1) if label_match else path.stem,
                           extra={"memory_status": "archived"}))

    for path in sorted((root / "EXPERIENCE" / "events").glob("*.md")):
        items.append(_item(root, path, category="experience", authority="historical",
                           legacy_label=path.stem))
    for path in sorted((root / "REFLECTION" / "self_reflections").glob("*.md")):
        items.append(_item(root, path, category="reflection", authority="non_authoritative",
                           legacy_label=path.stem, extra={"model_output": True}))

    structured = (
        ("REFLECTION/consciousness/current.json", "belief_state", "authoritative", "current"),
        ("REFLECTION/consciousness/history.jsonl", "belief_history", "historical", "history"),
        ("PERSONALITY/current.json", "personality_state", "authoritative", "current"),
        ("PERSONALITY/history.jsonl", "personality_history", "historical", "history"),
    )
    for relative, category, authority, label in structured:
        path = root / relative
        if path.is_file() and path.stat().st_size:
            items.append(_item(root, path, category=category, authority=authority,
                               legacy_label=label, extra={"format": path.suffix.lstrip(".")}))

    for name in ("current_state", "continuity"):
        path = root / "STATE" / f"{name}.md"
        if path.is_file() and path.stat().st_size:
            items.append(_item(root, path, category="runtime_state", authority="authoritative",
                               legacy_label=name, extra={"state_kind": name}))

    user_root = root / "USER"
    if user_root.exists():
        for path in sorted(p for p in user_root.rglob("*") if p.is_file() and p.stat().st_size):
            relative = path.relative_to(user_root)
            owner_key = relative.parts[0] if len(relative.parts) > 1 else None
            items.append(_item(root, path, category="user_state", authority="authoritative",
                               legacy_label=path.stem, owner_kind="user", owner_key=owner_key))

    explicit_exclusions = {
        "MEMORY/index.json": "derived embedding index",
        "MEMORY/checklist.md": "operational checklist, not cognitive state",
        "MEMORY/Minisa Mem Trash.txt": "operational archive log, not a memory record",
        "SELF_MODEL/current.json": "disconnected default self-model with no history",
        "SELF_MODEL/history.jsonl": "empty disconnected self-model history",
        "Minisa/": "nested divergent repository copy, not active runtime root",
        "C:\\Minisa/": "platform-artifact directory, not active runtime root",
    }
    for relative, reason in explicit_exclusions.items():
        path = root / relative.rstrip("/")
        if path.exists():
            exclusions.append({"path": relative, "reason": reason})

    item_docs = [asdict(item) for item in items]
    counts: dict[str, int] = {}
    for item in items:
        counts[item.category] = counts.get(item.category, 0) + 1
    manifest_body = {"source_name": SOURCE_NAME, "items": item_docs}
    return {
        "format_version": 1,
        "source_name": SOURCE_NAME,
        "source_root": str(root),
        "items": item_docs,
        "counts": dict(sorted(counts.items())),
        "total_records": len(items),
        "excluded": exclusions,
        "manifest_sha256": _sha(_canonical(manifest_body).encode()),
    }


def validate_inventory(root: Path, inventory: dict[str, Any]) -> None:
    rebuilt = build_inventory(root)
    if rebuilt["manifest_sha256"] != inventory["manifest_sha256"]:
        raise RuntimeError("source inventory changed after capture")


def _fingerprint(item: dict[str, Any]) -> str:
    return _sha(f'{SOURCE_NAME}\0{item["source_path"]}\0{item["sha256"]}'.encode())


def _record_id(item: dict[str, Any]) -> str:
    return str(uuid.uuid5(MIGRATION_NAMESPACE, _fingerprint(item)))


def dry_run(conn: sqlite3.Connection, inventory: dict[str, Any]) -> dict[str, Any]:
    existing: set[str] = set()
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognitive_records'").fetchone():
        existing = {row[0] for row in conn.execute("SELECT source_fingerprint FROM cognitive_records")}
    proposed = []
    for item in inventory["items"]:
        fingerprint = _fingerprint(item)
        proposed.append({"record_id": _record_id(item), "source_path": item["source_path"],
                         "category": item["category"], "legacy_label": item["legacy_label"],
                         "action": "unchanged" if fingerprint in existing else "insert"})
    return {"manifest_sha256": inventory["manifest_sha256"],
            "current_global_revision": conn.execute("SELECT global_revision FROM core_state WHERE singleton=1").fetchone()[0],
            "insert_count": sum(x["action"] == "insert" for x in proposed),
            "unchanged_count": sum(x["action"] == "unchanged" for x in proposed),
            "records": proposed, "excluded": inventory["excluded"], "counts": inventory["counts"]}


def migrate(conn: sqlite3.Connection, root: Path, inventory: dict[str, Any], *,
            fail_after: int | None = None) -> dict[str, Any]:
    validate_inventory(root, inventory)
    manifest = inventory["manifest_sha256"]
    existing_batch = conn.execute(
        "SELECT migration_id,global_revision,record_count FROM cognitive_migrations WHERE manifest_sha256=?", (manifest,)
    ).fetchone()
    if existing_batch:
        return {"migration_id": existing_batch[0], "global_revision": existing_batch[1],
                "record_count": existing_batch[2], "replayed": True}
    revision = conn.execute(
        "UPDATE core_state SET global_revision=global_revision+1 WHERE singleton=1 RETURNING global_revision"
    ).fetchone()[0]
    migration_id = str(uuid.uuid5(MIGRATION_NAMESPACE, manifest))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for index, item in enumerate(inventory["items"]):
        path = root / item["source_path"]
        raw = path.read_bytes()
        if _sha(raw) != item["sha256"]:
            raise RuntimeError(f'source checksum changed: {item["source_path"]}')
        fingerprint = _fingerprint(item)
        conn.execute(
            """INSERT INTO cognitive_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_record_id(item), item["category"], item["owner_kind"], item["owner_key"], item["authority"],
             item["legacy_label"], SOURCE_NAME, item["source_path"], item["sha256"], fingerprint,
             raw.decode("utf-8-sig"), _canonical(item["metadata"]), item["source_created_at"], now,
             revision, migration_id),
        )
        if fail_after is not None and index + 1 >= fail_after:
            raise RuntimeError("injected migration interruption")
    conn.execute(
        "INSERT INTO cognitive_migrations VALUES (?,?,?,?,?,?,?,?)",
        (migration_id, SOURCE_NAME, manifest, str(root.resolve()), _canonical(inventory),
         len(inventory["items"]), now, revision),
    )
    event_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?)",
        (event_id, revision, now, "migration", "cognitive_state.import", "cognitive_migration",
         migration_id, _canonical({"manifest_sha256": manifest, "record_count": len(inventory["items"])}),
         f"cognitive-migration:{manifest}"),
    )
    return {"migration_id": migration_id, "global_revision": revision,
            "record_count": len(inventory["items"]), "replayed": False}


def install_schema_v2(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_V2_SQL)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--inventory-out", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    inventory = build_inventory(args.source)
    if args.inventory_out:
        args.inventory_out.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n")
    result: dict[str, Any] = inventory
    if args.database:
        conn = sqlite3.connect(args.database)
        try:
            if args.dry_run:
                result = dry_run(conn, inventory)
            elif args.apply:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = migrate(conn, args.source.resolve(), inventory)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        finally:
            conn.close()
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
