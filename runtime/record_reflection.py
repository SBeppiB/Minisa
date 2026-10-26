"""Disabled legacy writer; use the awake runtime or a reviewed offline migration."""

if __name__ == "__main__":
    raise SystemExit("Legacy direct writer disabled; use the awake server. Offline maintenance requires a reviewed migration.")

# Historical implementation retained for review; never executed on import.
def _legacy_reference_only():
    raise RuntimeError("Legacy persistent-write authority has been revoked")
    from pathlib import Path
    
    from runtime.reflection_manager import ReflectionManager
    
    
    root = Path(__file__).resolve().parent.parent
    
    manager = ReflectionManager(root)
    
    reflection = manager.create_reflection(
        "001",
        """The runtime has successfully demonstrated that Minisa's
    identity, state, memory and experiences can persist independently
    of a language model.
    
    This suggests that the language model should eventually function
    as a reasoning component within the larger Minisa architecture,
    rather than being treated as Minisa herself.
    
    This is currently an architectural interpretation, not an established
    fact about consciousness."""
    )
    
    print(reflection)
