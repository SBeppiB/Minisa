"""Portable JSON CLI; no server is required."""

import argparse
import json
import sys

from pydantic import BaseModel

from .engine import MemoryEngine
from .models import Settings


def serialize(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(type(value).__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="umem", description=__doc__)
    parser.add_argument("--data", default="./data")
    parser.add_argument("--namespace", default="default")
    parser.add_argument(
        "--model", help="Sentence Transformers model; omit for lexical hash baseline"
    )
    parser.add_argument("--qdrant-url")
    parser.add_argument("--vector-backend", choices=["local", "qdrant"])
    parser.add_argument("--config", help="Settings JSON file")
    sub = parser.add_subparsers(dest="command", required=True)
    remember = sub.add_parser("remember")
    remember.add_argument("content")
    remember.add_argument("--importance", type=float, default=0.5)
    remember.add_argument("--entity", action="append", default=[])
    remember.add_argument("--tag", action="append", default=[])
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--top-k", type=int, default=10)
    search.add_argument("--entity", action="append", default=[])
    search.add_argument("--strict-entities", action="store_true")
    search.add_argument("--no-archives", action="store_true")
    listing = sub.add_parser("list")
    listing.add_argument("--tier", choices=["PRIORITY", "ACTIVE", "ARCHIVED"])
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--after", default="")
    listing.add_argument("--tag")
    listing.add_argument("--entity")
    for command in (
        "inspect",
        "archive",
        "restore",
        "forget",
        "pin",
        "unpin",
        "delete",
        "history",
        "consolidation",
        "reverse-consolidation",
    ):
        sub.add_parser(command).add_argument("id")
    edit = sub.add_parser("edit")
    edit.add_argument("id")
    edit.add_argument("changes", help='JSON object, e.g. {"importance":0.9}')
    for command in (
        "stats",
        "health",
        "backend-info",
        "rebuild-index",
        "repair-local-index",
        "sync-index",
        "duplicates",
        "logs",
    ):
        sub.add_parser(command)
    decay = sub.add_parser("decay")
    decay.add_argument("--apply", action="store_true")
    for command in ("export", "import", "backup"):
        sub.add_parser(command).add_argument("file")
    consolidate = sub.add_parser("consolidate")
    consolidate.add_argument("content")
    consolidate.add_argument("ids", nargs="+")
    for command in ("link", "supersede"):
        p = sub.add_parser(command)
        p.add_argument("source")
        p.add_argument("target")
    args = parser.parse_args(argv)
    try:
        from .embeddings import SentenceTransformerEmbedder

        settings = Settings()
        if args.config:
            from pathlib import Path

            settings = Settings.model_validate_json(Path(args.config).read_text(encoding="utf-8"))
        with MemoryEngine(
            args.data,
            namespace=args.namespace,
            settings=settings,
            embedder=SentenceTransformerEmbedder(args.model) if args.model else None,
            qdrant_url=args.qdrant_url,
            vector_backend=args.vector_backend,
        ) as engine:
            command = args.command
            if command == "remember":
                result = engine.remember(
                    args.content, importance=args.importance, tags=args.tag, entity_ids=args.entity
                )
            elif command == "search":
                result = engine.recall(
                    args.query,
                    top_k=args.top_k,
                    entity_ids=args.entity,
                    strict_entities=args.strict_entities,
                    include_archived=not args.no_archives,
                )
            elif command == "list":
                result = engine.list(
                    tier=args.tier,
                    limit=args.limit,
                    after=args.after,
                    tag=args.tag,
                    entity_id=args.entity,
                )
            elif command == "edit":
                result = engine.edit(args.id, **json.loads(args.changes))
            elif command == "decay":
                result = engine.decay(dry_run=not args.apply)
            elif command in ("export", "import", "backup"):
                result = getattr(engine, "import_file" if command == "import" else command)(
                    args.file
                )
            elif command == "consolidate":
                result = engine.consolidate(args.ids, args.content)
            elif command in ("link", "supersede"):
                result = getattr(engine, command)(args.source, args.target)
            else:
                method = {"inspect": "get", "logs": "retrieval_logs"}.get(
                    command, command.replace("-", "_")
                )
                result = (
                    getattr(engine, method)(args.id)
                    if hasattr(args, "id")
                    else getattr(engine, method)()
                )
            print(json.dumps(result, default=serialize, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        print(f"umem: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
