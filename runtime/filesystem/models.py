from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path
from enum import Enum


class OperationType(Enum):
    """Types of file operations."""
    MOVE = "move"
    DELETE = "delete"
    RENAME = "rename"
    CREATE_FOLDER = "create_folder"
    DEEP_NEST = "deep_nest"


class OperationStatus(Enum):
    """Status of file operations."""
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


@dataclass
class FileOperation:
    """Represents a single file operation."""
    operation_type: OperationType
    source: Optional[Path] = None
    destination: Optional[Path] = None
    timestamp: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    status: OperationStatus = OperationStatus.SUCCESS
    error_message: Optional[str] = None
    rollback_info: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation_type": self.operation_type.value,
            "source": str(self.source) if self.source else None,
            "destination": str(self.destination) if self.destination else None,
            "timestamp": self.timestamp,
            "status": self.status.value,
            "error_message": self.error_message,
            "rollback_info": self.rollback_info
        }


@dataclass
class OperationResult:
    """Result of a file operation."""
    success: bool
    operation: FileOperation
    message: str
    affected_paths: List[Path] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "operation": self.operation.to_dict(),
            "message": self.message,
            "affected_paths": [str(p) for p in self.affected_paths]
        }


@dataclass
class FileChange:
    """Represents a change to the filesystem."""
    change_type: str  # "added", "deleted", "moved", "renamed"
    path: str
    old_path: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "change_type": self.change_type,
            "path": self.path,
            "old_path": self.old_path,
            "timestamp": self.timestamp
        }


@dataclass
class ProtectionResult:
    """Result of a protection check."""
    is_protected: bool
    path: Path
    reason: Optional[str] = None
    can_override: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_protected": self.is_protected,
            "path": str(self.path),
            "reason": self.reason,
            "can_override": self.can_override
        }


@dataclass
class ProtectionViolation:
    """Record of a protection violation."""
    operation: str
    path: Path
    reason: str
    timestamp: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    was_allowed: bool = False
    override_used: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation,
            "path": str(self.path),
            "reason": self.reason,
            "timestamp": self.timestamp,
            "was_allowed": self.was_allowed,
            "override_used": self.override_used
        }
