from pathlib import Path
from datetime import datetime
from runtime.storage import atomic_write, identifier, serialized


class ReflectionManager:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.reflection_path = root / "REFLECTION"
        self.self_reflections_path = self.reflection_path / "self_reflections"
        self.identity_changes_path = self.reflection_path / "identity_changes"
        self.belief_changes_path = self.reflection_path / "belief_changes"
        self.consciousness_path = self.reflection_path / "consciousness"

    @serialized
    def create_reflection(
        self,
        reflection_id: str,
        content: str
    ) -> Path:
        identifier(reflection_id)
        path = self.self_reflections_path / f"{reflection_id}.md"

        timestamp = datetime.now().astimezone().isoformat()

        reflection = f"""# Reflection {reflection_id}

Timestamp: {timestamp}

{content}
"""

        self.last_receipt = atomic_write(path, reflection, root=self.root)

        return path

    def list_reflections(self) -> list[Path]:
        return sorted(
            self.self_reflections_path.glob("*.md")
        )

    def read_reflection(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")
