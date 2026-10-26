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
    
    target = active / "memory_003.md"
    
    if not target.exists():
        print("memory_003.md does not exist.")
        raise SystemExit
    
    content = target.read_text(encoding="utf-8")
    timestamp = datetime.now().astimezone().isoformat()
    
    with trash.open("a", encoding="utf-8") as f:
        f.write(f"\n\n## Removed incorrect memory {timestamp}\n\n")
        f.write("Original filename: memory_003.md\n")
        f.write("Reason: Incorrect entity attribution. The memory incorrectly assigned SBeppi as the user's creator instead of Minisa's creator.\n\n")
        f.write(content)
        f.write("\n")
    
    target.unlink()
    
    print("memory_003.md moved to Minisa Mem Trash.txt")
