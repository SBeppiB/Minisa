"""Disabled legacy writer; use the awake runtime or a reviewed offline migration."""

if __name__ == "__main__":
    raise SystemExit("Legacy direct writer disabled; use the awake server. Offline maintenance requires a reviewed migration.")

# Historical implementation retained for review; never executed on import.
def _legacy_reference_only():
    raise RuntimeError("Legacy persistent-write authority has been revoked")
    from pathlib import Path
    
    from runtime.experience_manager import ExperienceManager
    
    
    root = Path(__file__).resolve().parent.parent
    
    manager = ExperienceManager(root)
    
    event = manager.record_event(
        "001",
        """Minisa's persistent runtime successfully loaded its core,
    state, continuity and first memory.
    
    This was the first successful execution of the complete runtime."""
    )
    
    print(event)
