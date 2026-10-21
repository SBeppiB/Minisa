from pathlib import Path

from runtime.core.loader import CoreLoader
from runtime.experience_manager import ExperienceManager
from runtime.memory.manager import MemoryManager
from runtime.reflection_manager import ReflectionManager
from runtime.state.manager import StateManager


def main():
    root = Path(__file__).resolve().parent.parent

    loader = CoreLoader(root)
    state_manager = StateManager(root)
    memory_manager = MemoryManager(root)
    experience_manager = ExperienceManager(root)
    reflection_manager = ReflectionManager(root)

    core = loader.load()
    current_state = state_manager.load_current_state()
    continuity = state_manager.load_continuity()
    memories = memory_manager.list_active_memories()
    experiences = experience_manager.list_events()
    reflections = reflection_manager.list_reflections()

    print("MINISA RUNTIME")
    print("================")

    for name, content in core.items():
        print(f"\n[{name.upper()}]")
        print(content)

    print("\n[CURRENT STATE]")
    print(current_state)

    print("\n[CONTINUITY]")
    print(continuity)

    print("\n[ACTIVE MEMORIES]")

    for memory in memories:
        print(f"\n--- {memory.name} ---")
        print(memory_manager.read_memory(memory))

    print("\n[EXPERIENCES]")

    for experience in experiences:
        print(f"\n--- {experience.name} ---")
        print(experience_manager.read_event(experience))

    print("\n[REFLECTIONS]")

    for reflection in reflections:
        print(f"\n--- {reflection.name} ---")
        print(reflection_manager.read_reflection(reflection))


if __name__ == "__main__":
    main()
