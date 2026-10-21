"""
Reset personality to default neutral state after testing.
"""
from pathlib import Path
import sys

# Add the Minisa directory to the Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from runtime.personality.manager import PersonalityManager


def main():
    root = Path("C:\\Minisa")
    manager = PersonalityManager(root)
    
    print("Resetting personality to default neutral state...")
    default_state = manager.reset_to_default()
    
    print("Personality reset successfully!")
    print(f"Version: {default_state.version}")
    print(f"Traits: {default_state.traits.to_dict()}")
    print(f"Communication: {default_state.communication_style.to_dict()}")


if __name__ == "__main__":
    main()
