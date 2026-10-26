"""
Initialize Minisa's personality system with the default neutral state.
"""
from pathlib import Path
import sys

# Add the Minisa directory to the Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from runtime.personality.manager import PersonalityManager


def main():
    root = Path("C:\\Minisa")
    manager = PersonalityManager(root)
    
    # Create the default personality state
    default_state = manager.create_default()
    
    print("Personality system initialized successfully!")
    print(f"Personality state saved to: {manager.current_path}")
    print(f"History will be saved to: {manager.history_path}")
    
    # Display the initial state
    print("\nInitial Personality State:")
    print(f"Traits: {default_state.traits.to_dict()}")
    print(f"Communication Style: {default_state.communication_style.to_dict()}")
    print(f"Confidence: {default_state.confidence}")
    print(f"Version: {default_state.version}")
    print(f"Last Updated: {default_state.last_updated}")


if __name__ == "__main__":
    main()
