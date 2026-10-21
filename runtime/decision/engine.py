from dataclasses import dataclass


@dataclass
class Decision:
    action: str
    reason: str
    confidence: float
    memory: dict | None = None


class DecisionEngine:
    def decide(self, reasoning: dict) -> Decision:
        return Decision(
            action=reasoning.get("proposed_action", "respond"),
            reason=reasoning.get("interpretation", ""),
            confidence=reasoning.get("confidence", 0.0),
            memory=reasoning.get("memory"),
        )
