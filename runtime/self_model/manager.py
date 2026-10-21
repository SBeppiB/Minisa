from pathlib import Path
from datetime import datetime
import json


class SelfModelManager:
    def __init__(self, root: Path):
        self.path = root / "SELF_MODEL"
        self.current_path = self.path / "current.json"
        self.history_path = self.path / "history.jsonl"


    def snapshot(self, document=None) -> dict:
        """Exclude disconnected legacy identity/state; never initialize on read."""
        from runtime.cognition.persistence import committed_snapshot, self_snapshot
        data = (document["snapshots"].get("self_model") if document is not None
                else committed_snapshot(self.path.parent, "self_model"))
        if data is None:
            data = json.loads(self.current_path.read_text(encoding="utf-8")) if self.current_path.exists() else {}
        return self_snapshot(data)

    def load(self) -> dict:
        from runtime.cognition.persistence import committed_snapshot
        committed = committed_snapshot(self.path.parent, "self_model")
        if committed is not None:
            return committed
        if not self.current_path.exists():
            return {}
        return json.loads(self.current_path.read_text(encoding="utf-8"))

    def get_history(self, limit: int = 50) -> list:
        from runtime.cognition.persistence import read_document
        history = []
        if self.history_path.exists():
            for line in self.history_path.read_text(encoding="utf-8").splitlines():
                try:
                    history.append(json.loads(line))
                except ValueError:
                    continue
        history.extend(h for h in read_document(self.path.parent)["history"] if h["target"] == "self_model")
        return list(reversed(history[-limit:]))

    def create_default(self):
        raise ValueError("Cognitive state is runtime-owned; legacy mutation is disabled")

    def save(self, model, reason=""):
        raise ValueError("Cognitive state is runtime-owned; legacy mutation is disabled")
