"""Public, validated data contracts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


def now() -> datetime:
    return datetime.now(UTC)


class Tier(StrEnum):
    PRIORITY = "PRIORITY"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    DELETED = "DELETED"


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=100_000)
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=1, ge=0, le=1)
    tags: list[str] = Field(default_factory=list, max_length=100)
    category: str = "experience"
    source: str = "sdk"
    entity_ids: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Memory(MemoryInput):
    id: str
    namespace: str
    created_at: AwareDatetime
    last_accessed_at: AwareDatetime
    updated_at: AwareDatetime
    tier: Tier = Tier.ACTIVE
    pinned: bool = False
    access_count: int = Field(default=0, ge=0)
    reinforcement_count: int = Field(default=0, ge=0)
    index_status: str = "pending"
    version: int = Field(default=1, ge=1)
    superseded_by: str | None = None
    related_ids: list[str] = Field(default_factory=list)
    consolidation_history: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        from uuid import UUID

        return str(UUID(value))


class RecallResult(BaseModel):
    memory: Memory
    score: float
    explanation: dict[str, Any]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_limit: int = Field(default=80, ge=1, le=1000)
    duplicate_threshold: float = Field(default=0.97, ge=0, le=1)
    archive_semantic_threshold: float = Field(default=0.88, ge=0, le=1)
    archive_lexical_threshold: float = Field(default=0.65, ge=0, le=1)
    archive_candidates: int = Field(default=8, ge=0, le=100)
    promote_resurfaced: bool = False
    decay_half_life_days: float = Field(default=180, gt=0)
    archive_decay_threshold: float = Field(default=0.12, ge=0, le=1)
    chain_limit: int = Field(default=5, ge=0, le=100)
    backup_interval_seconds: int = Field(default=86400, ge=0)
    backup_keep: int = Field(default=7, ge=1)
    retrieval_log_keep: int = Field(default=1000, ge=0)
