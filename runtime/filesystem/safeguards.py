from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from runtime.filesystem.models import ProtectionResult, ProtectionViolation


class ProtectedFolderSafeguards:
    """Protect critical folders from accidental deletion, move, or rename."""

    def __init__(self, protected_folders: Optional[List[Path]] = None):
        """
        Initialize safeguards with protected folders.

        Args:
            protected_folders: List of folders to protect. If None, uses defaults.
        """
        if protected_folders is None:
            protected_folders = self._get_default_protected_folders()

        self.protected_folders: List[Path] = [Path(p).resolve() for p in protected_folders]
        self.violation_log: List[ProtectionViolation] = []

    def _get_default_protected_folders(self) -> List[Path]:
        """Get the default set of protected folders."""
        defaults: List[Path] = []

        # Minisa root directory
        minisa_root = Path("C:\\Minisa")
        if minisa_root.exists():
            defaults.append(minisa_root)

        # System-critical directories (Windows)
        system_dirs = [
            Path("C:\\Windows"),
            Path("C:\\Program Files"),
            Path("C:\\Program Files (x86)"),
            Path("C:\\Users\\Public"),
        ]

        for sys_dir in system_dirs:
            if sys_dir.exists():
                defaults.append(sys_dir)

        # User home directory
        home = Path.home()
        if home.exists():
            defaults.append(home)

        return defaults

    def check_protection(self, target_path: Path) -> ProtectionResult:
        """
        Check if a path is protected or contains protected folders.

        Args:
            target_path: Path to check

        Returns:
            ProtectionResult with protection status and details
        """
        resolved = Path(target_path).resolve()

        # Check if target itself is a protected folder
        for protected in self.protected_folders:
            if resolved == protected:
                return ProtectionResult(
                    is_protected=True,
                    path=resolved,
                    reason=f"Path is a protected folder: {protected}",
                    can_override=False
                )

        # Check if target is inside a protected folder
        for protected in self.protected_folders:
            try:
                resolved.relative_to(protected)
                return ProtectionResult(
                    is_protected=True,
                    path=resolved,
                    reason=f"Path is inside protected folder: {protected}",
                    can_override=False
                )
            except ValueError:
                continue

        return ProtectionResult(
            is_protected=False,
            path=resolved,
            reason=None,
            can_override=False
        )

    def check_recursive_protection(self, target_path: Path) -> ProtectionResult:
        """
        Check if deleting/moving/renaming target would affect protected folders.

        This checks if target is a parent of any protected folder, which would
        cause the protected folder to be deleted or moved along with it.

        Args:
            target_path: Path to check

        Returns:
            ProtectionResult with recursive protection status
        """
        resolved = Path(target_path).resolve()

        # First check direct protection
        direct_result = self.check_protection(resolved)
        if direct_result.is_protected:
            return direct_result

        # Check if target is a parent of any protected folder
        for protected in self.protected_folders:
            try:
                protected.relative_to(resolved)
                return ProtectionResult(
                    is_protected=True,
                    path=resolved,
                    reason=f"Path contains protected folder: {protected}",
                    can_override=False
                )
            except ValueError:
                continue

        return ProtectionResult(
            is_protected=False,
            path=resolved,
            reason=None,
            can_override=False
        )

    def allow_operation(self, target_path: Path, override: bool = False) -> bool:
        """
        Check if an operation is allowed on the target path.

        Args:
            target_path: Path to check
            override: Whether an explicit override was provided

        Returns:
            True if operation is allowed, False otherwise
        """
        result = self.check_recursive_protection(target_path)

        if not result.is_protected:
            return True

        # Protected path - only allow with explicit override
        return override

    def log_violation(
        self,
        operation: str,
        path: Path,
        reason: str,
        was_allowed: bool = False,
        override_used: bool = False
    ) -> None:
        """
        Log a protection violation.

        Args:
            operation: Type of operation (delete, move, rename, etc.)
            path: Path that was targeted
            reason: Why it was a violation
            was_allowed: Whether the operation was ultimately allowed
            override_used: Whether an override was used
        """
        violation = ProtectionViolation(
            operation=operation,
            path=Path(path).resolve(),
            reason=reason,
            timestamp=datetime.now().astimezone().isoformat(),
            was_allowed=was_allowed,
            override_used=override_used
        )
        self.violation_log.append(violation)

    def get_violations(self, limit: int = 50) -> List[ProtectionViolation]:
        """Get recent protection violations."""
        return list(reversed(self.violation_log[-limit:]))

    def clear_violations(self) -> None:
        """Clear the violation log."""
        self.violation_log.clear()

    def add_protected_folder(self, folder: Path) -> None:
        """Add a folder to the protected list."""
        resolved = Path(folder).resolve()
        if resolved not in self.protected_folders:
            self.protected_folders.append(resolved)

    def remove_protected_folder(self, folder: Path) -> bool:
        """Remove a folder from the protected list. Returns True if removed."""
        resolved = Path(folder).resolve()
        if resolved in self.protected_folders:
            self.protected_folders.remove(resolved)
            return True
        return False

    def get_protected_folders(self) -> List[str]:
        """Get list of protected folder paths as strings."""
        return [str(p) for p in self.protected_folders]

    def is_protected(self, target_path: Path) -> bool:
        """Quick check if a path is protected (direct or recursive)."""
        return self.check_recursive_protection(target_path).is_protected