from typing import List, Dict, Any, Optional
from runtime.personality.models import PersonalityChange, PersonalityState, PersonalityTraits, CommunicationStyle


class PersonalityValidator:
    """Validates personality changes and prevents unauthorized modifications."""

    # Valid trait names
    VALID_TRAITS = {
        "curiosity",
        "openness", 
        "honesty",
        "uncertainty_awareness",
        "patience",
        "assertiveness",
        "humour",
        "warmth",
        "formality",
        "skepticism",
        "creativity",
        "adaptability",
        # Communication style traits
        "communication_formality",
        "communication_verbosity",
        "communication_directness",
    }

    # Maximum magnitude of a single personality change
    MAX_CHANGE_MAGNITUDE = 0.10

    # Minimum confidence required for a change to be accepted
    MIN_CONFIDENCE = 0.30

    # Protected identity fields that cannot be modified through personality system
    PROTECTED_IDENTITY_FIELDS = {
        "name",
        "self_designation",
        "creator",
        "nature",
        "identity",
        "values",
        "goals",
        "instructions",
    }

    def validate_trait_name(self, trait: str) -> bool:
        """Validate that a trait name is allowed."""
        return trait.lower() in self.VALID_TRAITS

    def validate_numeric_range(self, value: float) -> bool:
        """Validate that a numeric value is within the valid range [0.0, 1.0]."""
        try:
            float_value = float(value)
            return 0.0 <= float_value <= 1.0
        except (TypeError, ValueError):
            return False

    def validate_change_magnitude(self, change: float) -> bool:
        """Validate that the magnitude of a change is within acceptable limits."""
        try:
            change_value = float(change)
            return abs(change_value) <= self.MAX_CHANGE_MAGNITUDE
        except (TypeError, ValueError):
            return False

    def validate_confidence(self, confidence: float) -> bool:
        """Validate that confidence is within valid range and meets minimum threshold."""
        try:
            confidence_value = float(confidence)
            return 0.0 <= confidence_value <= 1.0 and confidence_value >= self.MIN_CONFIDENCE
        except (TypeError, ValueError):
            return False

    def validate_proposal(self, proposal: PersonalityChange) -> tuple[bool, Optional[str]]:
        """Validate a complete personality change proposal."""
        # Check trait name
        if not self.validate_trait_name(proposal.trait):
            return False, f"Invalid trait name: {proposal.trait}"

        # Check change magnitude
        if not self.validate_change_magnitude(proposal.change):
            return False, f"Change magnitude {proposal.change} exceeds maximum {self.MAX_CHANGE_MAGNITUDE}"

        # Check confidence
        if not self.validate_confidence(proposal.confidence):
            return False, f"Confidence {proposal.confidence} below minimum {self.MIN_CONFIDENCE}"

        # Check reason is provided
        if not proposal.reason or not proposal.reason.strip():
            return False, "Reason must be provided for personality change"

        # Check evidence is a list
        if not isinstance(proposal.evidence, list):
            return False, "Evidence must be a list"

        return True, None

    def validate_state(self, state: PersonalityState) -> tuple[bool, Optional[str]]:
        """Validate a complete personality state."""
        # Validate traits
        for trait_name, trait_value in state.traits.to_dict().items():
            if not self.validate_numeric_range(trait_value):
                return False, f"Trait {trait_name} has invalid value: {trait_value}"

        # Validate communication style
        for style_name, style_value in state.communication_style.to_dict().items():
            if not self.validate_numeric_range(style_value):
                return False, f"Communication style {style_name} has invalid value: {style_value}"

        # Validate confidence
        if not self.validate_numeric_range(state.confidence):
            return False, f"Confidence has invalid value: {state.confidence}"

        # Validate version
        if state.version < 1:
            return False, f"Version must be >= 1, got {state.version}"

        return True, None

    def validate_no_identity_modification(self, change_data: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Ensure that personality changes do not attempt to modify protected identity fields."""
        for protected_field in self.PROTECTED_IDENTITY_FIELDS:
            if protected_field in change_data:
                return False, f"Cannot modify protected identity field: {protected_field}"

        return True, None

    def validate_dict_proposal(self, proposal_data: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Validate a personality change proposal from dictionary format."""
        # Check required fields
        required_fields = {"trait", "change", "reason", "confidence"}
        missing_fields = required_fields - set(proposal_data.keys())
        if missing_fields:
            return False, f"Missing required fields: {missing_fields}"

        # Convert to PersonalityChange and validate
        try:
            proposal = PersonalityChange.from_dict(proposal_data)
            return self.validate_proposal(proposal)
        except (TypeError, ValueError, KeyError) as e:
            return False, f"Invalid proposal data: {str(e)}"

    def calculate_resulting_value(self, current_value: float, change: float) -> float:
        """Calculate the resulting value after applying a change, clamped to [0.0, 1.0]."""
        try:
            result = float(current_value) + float(change)
            return max(0.0, min(1.0, result))
        except (TypeError, ValueError):
            return current_value

    def validate_applicable(self, proposal: PersonalityChange, current_state: PersonalityState) -> tuple[bool, Optional[str], float]:
        """Validate that a proposal can be applied to the current state and return the resulting value."""
        # First validate the proposal itself
        is_valid, error = self.validate_proposal(proposal)
        if not is_valid:
            return False, error, 0.0

        # Check if the trait exists in current state
        current_traits = current_state.traits.to_dict()
        current_communication = current_state.communication_style.to_dict()

        # Map communication style traits to their full names
        trait_mapping = {
            "communication_formality": "formality",
            "communication_verbosity": "verbosity", 
            "communication_directness": "directness",
        }

        # Get current value
        if proposal.trait in current_traits:
            current_value = current_traits[proposal.trait]
        elif proposal.trait in trait_mapping:
            current_value = current_communication[trait_mapping[proposal.trait]]
        else:
            return False, f"Trait {proposal.trait} not found in current state", 0.0

        # Calculate resulting value
        new_value = self.calculate_resulting_value(current_value, proposal.change)

        return True, None, new_value
