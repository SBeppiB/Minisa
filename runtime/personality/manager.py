from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional
import json

from runtime.personality.models import (
    PersonalityState, 
    PersonalityChange, 
    PersonalityHistoryEntry,
    PersonalityTraits,
    CommunicationStyle
)
from runtime.personality.validator import PersonalityValidator
from runtime.personality.updater import PersonalityUpdater


class PersonalityManager:
    """Main manager for personality state persistence and updates."""

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "PERSONALITY"
        self.current_path = self.path / "current.json"
        self.history_path = self.path / "history.jsonl"


        self.validator = PersonalityValidator()
        self.updater = PersonalityUpdater(self.validator)

    def snapshot(self, document=None) -> Dict[str, Any]:
        """Read validated live cognition state without repairing legacy data."""
        from runtime.cognition.persistence import committed_snapshot, personality_snapshot
        data = (document["snapshots"].get("personality") if document is not None
                else committed_snapshot(self.root, "personality"))
        if data is None:
            if not self.current_path.exists():
                return PersonalityState().to_dict()
            try:
                data = json.loads(self.current_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError(f"Personality unavailable/corrupt at {self.current_path}; "
                                 f"original file preserved: {error}") from error
        return personality_snapshot(data)

    def load(self) -> PersonalityState:
        """Load the current personality state from disk."""
        # Read failures never reset or overwrite a live personality file.
        return PersonalityState.from_dict(self.snapshot())

    def create_default(self) -> PersonalityState:
        """Create the default neutral personality state."""
        default_state = PersonalityState(
            traits=PersonalityTraits(
                curiosity=0.50,
                openness=0.50,
                honesty=0.80,
                uncertainty_awareness=0.80,
                patience=0.70,
                assertiveness=0.50,
                humour=0.50,
                warmth=0.50,
                formality=0.50,
                skepticism=0.60,
                creativity=0.50,
                adaptability=0.60,
            ),
            communication_style=CommunicationStyle(
                formality=0.50,
                verbosity=0.50,
                directness=0.50,
            ),
            confidence=0.50,
            version=1,
            last_updated=datetime.now().astimezone().isoformat()
        )

        # Save the default state
        self.save_state(default_state, "Initial neutral personality created.")

        return default_state

    def save_state(self, state, reason=""):
        raise ValueError("Cognitive state is runtime-owned; legacy mutation is disabled")

    def record_history(self, entry):
        raise ValueError("Cognitive state is runtime-owned; legacy mutation is disabled")

    def get_current_traits(self) -> Dict[str, float]:
        """Get current personality traits as a dictionary."""
        state = self.load()
        return state.traits.to_dict()

    def get_communication_style(self) -> Dict[str, float]:
        """Get current communication style as a dictionary."""
        state = self.load()
        return state.communication_style.to_dict()

    def get_full_state(self) -> Dict[str, Any]:
        """Get the complete personality state as a dictionary."""
        state = self.load()
        return state.to_dict()

    def propose_change(
        self,
        proposal: PersonalityChange,
        experience_ids: List[str] = None
    ) -> tuple[bool, Optional[str], PersonalityState]:
        """
        Propose a personality change and apply it if valid.
        
        Returns:
            tuple: (success, error_message, updated_state)
        """
        # Validate the proposal
        is_valid, error = self.validator.validate_proposal(proposal)
        if not is_valid:
            return False, error, self.load()

        # Load current state
        current_state = self.load()

        # Apply the change
        updated_state, history_entry = self.updater.apply_change(
            current_state, 
            proposal, 
            experience_ids or []
        )

        if history_entry is None:
            return False, "Failed to apply change", current_state

        # Save the updated state
        try:
            self.save_state(updated_state, proposal.reason)
            self.record_history(history_entry)
            return True, None, updated_state
        except Exception as e:
            return False, f"Failed to save state: {str(e)}", current_state

    def propose_batch_changes(
        self,
        proposals: List[PersonalityChange],
        experience_ids: List[str] = None
    ) -> tuple[bool, Optional[str], PersonalityState, List[PersonalityHistoryEntry]]:
        """
        Propose multiple personality changes and apply valid ones.
        
        Returns:
            tuple: (success, error_message, updated_state, history_entries)
        """
        # Validate all proposals first
        for proposal in proposals:
            is_valid, error = self.validator.validate_proposal(proposal)
            if not is_valid:
                return False, f"Invalid proposal: {error}", self.load(), []

        # Load current state
        current_state = self.load()

        # Apply changes
        updated_state, history_entries = self.updater.apply_batch_changes(
            current_state, 
            proposals, 
            experience_ids or []
        )

        if not history_entries:
            return False, "No valid changes to apply", current_state, []

        # Save the updated state
        try:
            self.save_state(updated_state, f"Batch update: {len(proposals)} changes")
            for entry in history_entries:
                self.record_history(entry)
            return True, None, updated_state, history_entries
        except Exception as e:
            return False, f"Failed to save state: {str(e)}", current_state, []

    def get_history(self, limit: int = 50) -> List[PersonalityHistoryEntry]:
        """Get personality change history."""
        from runtime.cognition.persistence import read_document
        cognitive_history = []
        for entry in read_document(self.root)["history"]:
            if entry["target"] != "personality":
                continue
            old, new = entry["previous"], entry["new"]
            for section in ("traits", "communication_style"):
                for trait, value in new[section].items():
                    if old[section][trait] != value:
                        cognitive_history.append(PersonalityHistoryEntry(
                            timestamp=datetime.fromtimestamp(entry["timestamp"]).astimezone().isoformat(),
                            trait=trait if section == "traits" else "communication_" + trait,
                            old_value=old[section][trait], new_value=value,
                            reason=entry["reason"], evidence=[entry["experience"]],
                            confidence=entry["proposal"]["confidence"], version=new["version"],
                        ))
        if not self.history_path.exists():
            return list(reversed(cognitive_history[-limit:]))

        history = []
        try:
            with self.history_path.open("r", encoding="utf-8") as file:
                for line in file:
                    if line.strip():
                        try:
                            entry_data = json.loads(line.strip())
                            history.append(PersonalityHistoryEntry.from_dict(entry_data))
                        except (json.JSONDecodeError, KeyError, TypeError):
                            continue
        except OSError:
            return []

        # Return most recent entries first
        return list(reversed((history + cognitive_history)[-limit:]))

    def get_trait_history(self, trait: str, limit: int = 20) -> List[PersonalityHistoryEntry]:
        """Get history for a specific trait."""
        all_history = self.get_history(limit * 2)  # Get more to filter
        return [entry for entry in all_history if entry.trait == trait][:limit]

    def reset_to_default(self) -> PersonalityState:
        """Reset personality to the default neutral state."""
        # Record this reset in history
        current_state = self.load()
        
        reset_entry = PersonalityHistoryEntry(
            timestamp=datetime.now().astimezone().isoformat(),
            trait="system_reset",
            old_value=0.0,
            new_value=0.0,
            reason="Personality reset to default neutral state",
            evidence=[],
            confidence=1.0,
            version=1
        )
        
        default_state = self.create_default()
        self.record_history(reset_entry)
        
        return default_state

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of the current personality state."""
        state = self.load()
        history = self.get_history(limit=10)
        
        return {
            "current_traits": state.traits.to_dict(),
            "communication_style": state.communication_style.to_dict(),
            "confidence": state.confidence,
            "version": state.version,
            "last_updated": state.last_updated,
            "total_changes": len(history),
            "recent_changes": [
                {
                    "trait": entry.trait,
                    "change": entry.new_value - entry.old_value,
                    "reason": entry.reason,
                    "timestamp": entry.timestamp
                }
                for entry in history[:5]
            ]
        }

    def validate_proposal(self, proposal_data: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Validate a personality change proposal from dictionary format."""
        return self.validator.validate_dict_proposal(proposal_data)
