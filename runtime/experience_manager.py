from pathlib import Path
from datetime import datetime
from runtime.storage import atomic_write, identifier, serialized


class ExperienceManager:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.experience_path = root / "EXPERIENCE"
        self.events_path = self.experience_path / "events"
        self.mistakes_path = self.experience_path / "mistakes"
        self.reflections_path = self.experience_path / "reflections"

    @serialized
    def record_event(self, event_id: str, content: str) -> Path:
        identifier(event_id)
        path = self.events_path / f"{event_id}.md"

        timestamp = datetime.now().astimezone().isoformat()

        event = f"""# Experience {event_id}

Timestamp: {timestamp}

{content}
"""

        self.last_receipt = atomic_write(path, event, root=self.root)

        return path

    def list_events(self) -> list[Path]:
        return sorted(self.events_path.glob("*.md"))

    def read_event(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")
