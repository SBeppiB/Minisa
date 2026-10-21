from pathlib import Path


class StateManager:
    def __init__(self, root: Path):
        self.state_path = root / "STATE"
        self.current_state_file = self.state_path / "current_state.md"
        self.continuity_file = self.state_path / "continuity.md"

    def snapshot(self, document=None) -> dict:
        """Runtime state expires; legacy notes remain labeled operator context."""
        import math
        import time
        from runtime.cognition.persistence import committed_snapshot, STATE_FIELDS, text
        data = (document["snapshots"].get("state") if document is not None
                else committed_snapshot(self.state_path.parent, "state"))
        if data is None:
            notes = self.current_state_file.read_text(encoding="utf-8") if self.current_state_file.exists() else ""
            return {"owner": "legacy_operator", "notes": notes[:1200]}
        if (not isinstance(data, dict) or data.get("owner") != "cognition"
                or type(data.get("expires_at")) not in (int, float)
                or not math.isfinite(data["expires_at"])
                or not isinstance(data.get("values"), dict)
                or not set(data["values"]) <= STATE_FIELDS):
            raise ValueError("Invalid dynamic state")
        values = {key: text(value) for key, value in data["values"].items()}
        return {"owner": "cognition", "values": values if data["expires_at"] > time.time() else {},
                "expires_at": data["expires_at"]}

    def load_current_state(self) -> str:
        import json
        return json.dumps(self.snapshot(), ensure_ascii=False)

    def load_continuity(self) -> str:
        return self.continuity_file.read_text(encoding="utf-8") if self.continuity_file.exists() else ""

    def save_current_state(self, content: str, *, revision=None, experience=None,
                           ttl_seconds=3600) -> dict:
        from runtime.cognition.persistence import CognitivePersistence
        proposal = {"target": "state", "operation": "replace",
                    "values": {"current_focus": content}, "ttl_seconds": ttl_seconds,
                    "reason": "Explicit state API update"}
        receipt = CognitivePersistence(self.state_path.parent).commit(
            [proposal], {}, revision, experience, source="state_api")
        if not receipt["committed"]:
            raise ValueError(receipt["reason"])
        return receipt

    def save_continuity(self, content: str) -> None:
        raise ValueError("Legacy continuity is read-only; use revisioned temporary state")
