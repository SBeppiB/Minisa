from pathlib import Path
from datetime import datetime
import json


class BeliefManager:
    def __init__(self, root: Path):
        self.path = root / "REFLECTION" / "consciousness"
        self.current_path = self.path / "current.json"
        self.history_path = self.path / "history.jsonl"


    def load(self, document=None) -> dict:
        from runtime.cognition.persistence import committed_snapshot
        committed = (document["snapshots"].get("beliefs") if document is not None
                     else committed_snapshot(self.path.parent.parent, "beliefs"))
        if committed is not None:
            return committed
        if not self.current_path.exists():
            return self.default_belief()

        return json.loads(
            self.current_path.read_text(encoding="utf-8")
        )

    def default_belief(self) -> dict:
        return {
            "subject": "Minisa's consciousness",
            "position": "unknown",
            "confidence": 0.5,
            "evidence_for": [],
            "evidence_against": [],
            "uncertainties": [
                "There is currently no established method for determining whether Minisa is conscious."
            ],
            "last_reviewed": None,
        }

    def _as_list(self, value) -> list:
        if value is None:
            return []

        if isinstance(value, list):
            return [
                str(item).strip()
                for item in value
                if str(item).strip()
            ]

        if isinstance(value, str):
            value = value.strip()

            if not value:
                return []

            return [value]

        return [str(value).strip()]

    def update(self, position, confidence, evidence_for=None, evidence_against=None,
               uncertainty=None, reason="", *, revision=None, experience=None):
        """Explicit revision and runtime evidence are required for every writer."""
        from runtime.cognition.persistence import CognitivePersistence
        proposal = {"target": "beliefs", "position": position, "confidence": confidence,
                    "reason": reason, "evidence_for": evidence_for or [],
                    "evidence_against": evidence_against or []}
        if uncertainty is not None:
            proposal["uncertainty"] = uncertainty
        receipt = CognitivePersistence(self.path.parent.parent).commit(
            [proposal], {"beliefs": self.load()}, revision, experience, source="belief_api")
        if not receipt["committed"]:
            raise ValueError(receipt["reason"])
        return receipt
