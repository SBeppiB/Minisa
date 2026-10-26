#!/usr/bin/env python3
"""Verify the staged Minisa candidate runtime-file and systemd drop-in hashes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify(manifest_path: Path) -> dict:
    manifest_path = manifest_path.resolve(strict=True)
    data = json.loads(manifest_path.read_text())
    release = Path(data["candidate_release"]).resolve(strict=True)
    entries = data["changed_runtime_files"]
    paths = set()
    for entry in entries:
        rel = Path(entry["path"])
        if rel.is_absolute() or ".." in rel.parts or rel.as_posix() in paths:
            raise ValueError("invalid or duplicate candidate manifest path")
        paths.add(rel.as_posix())
        path = (release / rel).resolve(strict=True)
        if not path.is_relative_to(release):
            raise ValueError("candidate manifest path escapes the staged release")
        payload = path.read_bytes()
        if len(payload) != entry["bytes"] or hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise ValueError(f"candidate runtime file does not match manifest: {rel}")
    dropin = data["candidate_systemd_dropin"]
    dropin_path = Path(dropin["source"]).resolve(strict=True)
    if sha256(dropin_path) != dropin["sha256"]:
        raise ValueError("candidate systemd drop-in does not match manifest")
    return {"verified": True, "runtime_file_count": len(entries),
            "manifest_sha256": sha256(manifest_path), "dropin_sha256": sha256(dropin_path),
            "candidate_release": str(release), "production_deployed": data.get("production_deployed")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.manifest), indent=2))
        return 0
    except Exception as exc:
        print(f"candidate manifest verification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
