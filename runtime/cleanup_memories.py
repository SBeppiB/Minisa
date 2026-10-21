"""Disabled legacy writer; use the awake runtime or a reviewed offline migration."""

if __name__ == "__main__":
    raise SystemExit("Legacy direct writer disabled; use the awake server. Offline maintenance requires a reviewed migration.")

# Historical implementation retained for review; never executed on import.
def _legacy_reference_only():
    raise RuntimeError("Legacy persistent-write authority has been revoked")
    from pathlib import Path
    from datetime import datetime
    
    root = Path(__file__).resolve().parent.parent
    memory_path = root / "MEMORY"
    active = memory_path / "active"
    trash = memory_path / "Minisa Mem Trash.txt"
    checklist = memory_path / "checklist.md"
    
    files = sorted(active.glob("memory_*.md"))
    
    keep = None
    duplicates = []
    
    for path in files:
        content = path.read_text(encoding="utf-8").lower()
    
        if "sbeppi" in content and "creator" in content:
            if keep is None:
                keep = path
            else:
                duplicates.append(path)
    
    if not duplicates:
        print("No duplicate creator memories found.")
        raise SystemExit
    
    timestamp = datetime.now().astimezone().isoformat()
    
    with trash.open("a", encoding="utf-8") as f:
        f.write(f"\n\n## Cleanup {timestamp}\n\n")
    
        for path in duplicates:
            content = path.read_text(encoding="utf-8")
    
            f.write(f"### Deleted memory: {path.name}\n")
            f.write(f"Original filename: {path.name}\n")
            f.write(f"Date deleted: {timestamp}\n")
            f.write("Reason: Duplicate creator memory.\n\n")
            f.write(content)
            f.write("\n\n")
    
            path.unlink()
    
    print(f"Kept: {keep.name}")
    print(f"Moved to trash: {len(duplicates)} duplicate memories.")
    
    remaining = sorted(active.glob("memory_*.md"))
    
    lines = [
        "# Minisa Memory Index",
        "",
        "This file tracks the existence and status of Minisa's memories.",
        "",
        "## Active Memories",
        ""
    ]
    
    for path in remaining:
        lines.append(f"- {path.name}")
    
    lines += [
        "",
        "## Archived Memories",
        "",
        "## Deleted Memories",
        "",
        "Deleted memories are preserved in `Minisa Mem Trash.txt`.",
    ]
    
    checklist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    
    print("\nActive memories:")
    for path in remaining:
        print(f" - {path.name}")
