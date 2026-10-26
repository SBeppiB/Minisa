from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime


@dataclass
class PersonalityTraits:
    """Core personality traits that define behavioural tendencies."""
    curiosity: float = 0.50
    openness: float = 0.50
    honesty: float = 0.80
    uncertainty_awareness: float = 0.80
    patience: float = 0.70
    assertiveness: float = 0.50
    humour: float = 0.50
    warmth: float = 0.50
    formality: float = 0.50
    skepticism: float = 0.60
    creativity: float = 0.50
    adaptability: float = 0.60

    def to_dict(self) -> Dict[str, float]:
        return {
            "curiosity": self.curiosity,
            "openness": self.openness,
            "honesty": self.honesty,
            "uncertainty_awareness": self.uncertainty_awareness,
            "patience": self.patience,
            "assertiveness": self.assertiveness,
            "humour": self.humour,
            "warmth": self.warmth,
            "formality": self.formality,
            "skepticism": self.skepticism,
            "creativity": self.creativity,
            "adaptability": self.adaptability,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PersonalityTraits':
        return cls(
            curiosity=float(data.get("curiosity", 0.50)),
            openness=float(data.get("openness", 0.50)),
            honesty=float(data.get("honesty", 0.80)),
            uncertainty_awareness=float(data.get("uncertainty_awareness", 0.80)),
            patience=float(data.get("patience", 0.70)),
            assertiveness=float(data.get("assertiveness", 0.50)),
            humour=float(data.get("humour", 0.50)),
            warmth=float(data.get("warmth", 0.50)),
            formality=float(data.get("formality", 0.50)),
            skepticism=float(data.get("skepticism", 0.60)),
            creativity=float(data.get("creativity", 0.50)),
            adaptability=float(data.get("adaptability", 0.60)),
        )


@dataclass
class CommunicationStyle:
    """Communication preferences and tendencies."""
    formality: float = 0.50
    verbosity: float = 0.50
    directness: float = 0.50

    def to_dict(self) -> Dict[str, float]:
        return {
            "formality": self.formality,
            "verbosity": self.verbosity,
            "directness": self.directness,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CommunicationStyle':
        return cls(
            formality=float(data.get("formality", 0.50)),
            verbosity=float(data.get("verbosity", 0.50)),
            directness=float(data.get("directness", 0.50)),
        )


@dataclass
class PersonalityState:
    """Complete personality state including traits, communication style, and metadata."""
    traits: PersonalityTraits = field(default_factory=PersonalityTraits)
    communication_style: CommunicationStyle = field(default_factory=CommunicationStyle)
    confidence: float = 0.50
    version: int = 1
    last_updated: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "traits": self.traits.to_dict(),
            "communication_style": self.communication_style.to_dict(),
            "confidence": self.confidence,
            "version": self.version,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PersonalityState':
        return cls(
            traits=PersonalityTraits.from_dict(data.get("traits", {})),
            communication_style=CommunicationStyle.from_dict(data.get("communication_style", {})),
            confidence=float(data.get("confidence", 0.50)),
            version=int(data.get("version", 1)),
            last_updated=data.get("last_updated"),
        )


@dataclass
class PersonalityChange:
    """A proposed change to a personality trait."""
    trait: str
    change: float
    reason: str
    evidence: List[str] = field(default_factory=list)
    confidence: float = 0.50

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trait": self.trait,
            "change": self.change,
            "reason": self.reason,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PersonalityChange':
        return cls(
            trait=str(data.get("trait", "")),
            change=float(data.get("change", 0.0)),
            reason=str(data.get("reason", "")),
            evidence=list(data.get("evidence", [])),
            confidence=float(data.get("confidence", 0.50)),
        )


@dataclass
class PersonalityHistoryEntry:
    """Historical record of a personality change."""
    timestamp: str
    trait: str
    old_value: float
    new_value: float
    reason: str
    evidence: List[str] = field(default_factory=list)
    confidence: float = 0.50
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "trait": self.trait,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "reason": self.reason,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PersonalityHistoryEntry':
        return cls(
            timestamp=str(data.get("timestamp", "")),
            trait=str(data.get("trait", "")),
            old_value=float(data.get("old_value", 0.0)),
            new_value=float(data.get("new_value", 0.0)),
            reason=str(data.get("reason", "")),
            evidence=list(data.get("evidence", [])),
            confidence=float(data.get("confidence", 0.50)),
            version=int(data.get("version", 1)),
        )
