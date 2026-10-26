from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional
import json

from runtime.personality.models import (
    PersonalityChange, 
    PersonalityState, 
    PersonalityHistoryEntry,
    PersonalityTraits,
    CommunicationStyle
)
from runtime.personality.validator import PersonalityValidator


class PersonalityUpdater:
    """Handles personality updates with validation and history recording."""

    def __init__(self, validator: PersonalityValidator):
        self.validator = validator

    def apply_change(
        self,
        state: PersonalityState,
        proposal: PersonalityChange,
        experience_ids: List[str] = None
    ) -> tuple[PersonalityState, Optional[PersonalityHistoryEntry]]:
        """
        Apply a validated personality change to the state.
        
        Returns:
            tuple: (updated_state, history_entry) or (original_state, None) if validation fails
        """
        # Validate the proposal is applicable
        is_valid, error, new_value = self.validator.validate_applicable(proposal, state)
        if not is_valid:
            return state, None

        # Create updated state
        updated_state = self._update_state_value(state, proposal.trait, new_value)
        updated_state.version += 1
        updated_state.last_updated = datetime.now().astimezone().isoformat()

        # Create history entry
        evidence = proposal.evidence.copy()
        if experience_ids:
            evidence.extend(experience_ids)

        history_entry = PersonalityHistoryEntry(
            timestamp=updated_state.last_updated,
            trait=proposal.trait,
            old_value=self._get_trait_value(state, proposal.trait),
            new_value=new_value,
            reason=proposal.reason,
            evidence=evidence,
            confidence=proposal.confidence,
            version=updated_state.version
        )

        return updated_state, history_entry

    def apply_batch_changes(
        self,
        state: PersonalityState,
        proposals: List[PersonalityChange],
        experience_ids: List[str] = None
    ) -> tuple[PersonalityState, List[PersonalityHistoryEntry]]:
        """
        Apply multiple personality changes in sequence.
        
        Returns:
            tuple: (updated_state, history_entries)
        """
        updated_state = state
        history_entries = []

        for proposal in proposals:
            # Validate each proposal
            is_valid, error = self.validator.validate_proposal(proposal)
            if not is_valid:
                continue  # Skip invalid proposals

            # Apply the change
            updated_state, history_entry = self.apply_change(
                updated_state, 
                proposal, 
                experience_ids
            )

            if history_entry:
                history_entries.append(history_entry)

        return updated_state, history_entries

    def _update_state_value(self, state: PersonalityState, trait: str, new_value: float) -> PersonalityState:
        """Update a specific trait value in the state."""
        # Create a deep copy of the state
        updated_state = PersonalityState.from_dict(state.to_dict())

        # Map communication style traits
        comm_trait_mapping = {
            "communication_formality": "formality",
            "communication_verbosity": "verbosity",
            "communication_directness": "directness",
        }

        if trait in comm_trait_mapping:
            # Update communication style
            comm_field = comm_trait_mapping[trait]
            setattr(updated_state.communication_style, comm_field, new_value)
        elif hasattr(updated_state.traits, trait):
            # Update traits
            setattr(updated_state.traits, trait, new_value)

        return updated_state

    def _get_trait_value(self, state: PersonalityState, trait: str) -> float:
        """Get the current value of a trait."""
        comm_trait_mapping = {
            "communication_formality": "formality",
            "communication_verbosity": "verbosity", 
            "communication_directness": "directness",
        }

        if trait in comm_trait_mapping:
            comm_field = comm_trait_mapping[trait]
            return getattr(state.communication_style, comm_field)
        elif hasattr(state.traits, trait):
            return getattr(state.traits, trait)

        return 0.0

    def calculate_change_from_evidence(
        self,
        trait: str,
        positive_evidence_count: int,
        negative_evidence_count: int,
        base_confidence: float = 0.5
    ) -> float:
        """
        Calculate a personality change magnitude based on evidence counts.
        
        This is a simple heuristic - more sophisticated methods can be added later.
        """
        total_evidence = positive_evidence_count + negative_evidence_count
        if total_evidence == 0:
            return 0.0

        # Calculate net evidence proportion
        net_proportion = (positive_evidence_count - negative_evidence_count) / total_evidence

        # Scale by confidence and maximum allowed change
        change = net_proportion * base_confidence * self.validator.MAX_CHANGE_MAGNITUDE

        return change

    def create_proposal_from_reflection(
        self,
        trait: str,
        reflection_reason: str,
        evidence_ids: List[str],
        change_direction: str,  # "increase", "decrease", or "neutral"
        confidence: float = 0.5
    ) -> PersonalityChange:
        """
        Create a personality change proposal from reflection data.
        
        Args:
            trait: The trait to modify
            reflection_reason: The reason for the change from reflection
            evidence_ids: List of experience IDs that support this change
            change_direction: Whether to increase, decrease, or maintain the trait
            confidence: Confidence in this proposed change
        """
        # Calculate change magnitude based on direction
        if change_direction == "increase":
            change = self.validator.MAX_CHANGE_MAGNITUDE * confidence
        elif change_direction == "decrease":
            change = -self.validator.MAX_CHANGE_MAGNITUDE * confidence
        else:
            change = 0.0

        return PersonalityChange(
            trait=trait,
            change=change,
            reason=reflection_reason,
            evidence=evidence_ids,
            confidence=confidence
        )

    def merge_proposals(self, proposals: List[PersonalityChange]) -> List[PersonalityChange]:
        """
        Merge multiple proposals for the same trait by averaging their changes.
        
        This prevents small changes from accumulating too quickly.
        """
        trait_proposals = {}

        for proposal in proposals:
            if proposal.trait not in trait_proposals:
                trait_proposals[proposal.trait] = []
            trait_proposals[proposal.trait].append(proposal)

        merged_proposals = []

        for trait, trait_list in trait_proposals.items():
            if len(trait_list) == 1:
                merged_proposals.append(trait_list[0])
            else:
                # Average the changes
                avg_change = sum(p.change for p in trait_list) / len(trait_list)
                avg_confidence = sum(p.confidence for p in trait_list) / len(trait_list)
                
                # Combine evidence
                all_evidence = []
                for p in trait_list:
                    all_evidence.extend(p.evidence)
                
                # Combine reasons
                combined_reason = f"Based on {len(trait_list)} reflections: " + "; ".join(p.reason for p in trait_list)

                merged_proposal = PersonalityChange(
                    trait=trait,
                    change=avg_change,
                    reason=combined_reason,
                    evidence=list(set(all_evidence)),  # Remove duplicates
                    confidence=avg_confidence
                )
                merged_proposals.append(merged_proposal)

        return merged_proposals
