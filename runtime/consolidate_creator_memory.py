"""Disabled legacy writer; use the awake runtime or a reviewed offline migration."""

if __name__ == "__main__":
    raise SystemExit("Legacy direct writer disabled; use the awake server. Offline maintenance requires a reviewed migration.")

# Historical implementation retained for review; never executed on import.
def _legacy_reference_only():
    raise RuntimeError("Legacy persistent-write authority has been revoked")
    from pathlib import Path
    from datetime import datetime
    
    root = Path(__file__).resolve().parent.parent
    memory = root / "MEMORY"
    active = memory / "active"
    trash = memory / "Minisa Mem Trash.txt"
    checklist = memory / "checklist.md"
    
    old = active / "memory_002.md"
    
    if old.exists():
        content = old.read_text(encoding="utf-8")
        timestamp = datetime.now().astimezone().isoformat()
    
        with trash.open("a", encoding="utf-8") as f:
            f.write(f"\n\n## Consolidated memory {timestamp}\n\n")
            f.write("Original filename: memory_002.md\n")
            f.write("Reason: Replaced by canonical creator relationship memory_003.md.\n\n")
            f.write(content)
            f.write("\n")
    
        old.unlink()
        print("memory_002.md moved to Minisa Mem Trash.txt")
    else:
        print("memory_002.md already removed.")
    
    print("\nActive memories:")
    for path in sorted(active.glob("memory_*.md")):
        print(f" - {path.name}")
