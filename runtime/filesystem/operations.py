from pathlib import Path
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime
import shutil
import logging

from runtime.filesystem.models import (
    FileOperation,
    OperationResult,
    OperationType,
    OperationStatus,
    ProtectionResult,
    ProtectionViolation
)
from runtime.filesystem.safeguards import ProtectedFolderSafeguards


class FileOperations:
    """Cross-platform file operations with safety and logging."""

    def __init__(self, protected_folders: Optional[List[Path]] = None, safeguards: Optional[ProtectedFolderSafeguards] = None):
        """
        Initialize file operations with optional protected folders and safeguards.

        Args:
            protected_folders: List of folders that should be protected from deletion
            safeguards: Optional ProtectedFolderSafeguards instance. If None, creates one with protected_folders.
        """
        if safeguards is not None:
            self.safeguards = safeguards
        else:
            self.safeguards = ProtectedFolderSafeguards(protected_folders)

        self.operation_log: List[FileOperation] = []
        self.rollback_stack: List[Dict[str, Any]] = []
        self.logger = logging.getLogger(__name__)

    def move_file(self, source: Path, destination: Path) -> OperationResult:
        """
        Move file with cross-platform support and safety checks.

        Args:
            source: Source file path
            destination: Destination file path

        Returns:
            OperationResult with success status and details
        """
        operation = FileOperation(
            operation_type=OperationType.MOVE,
            source=Path(source).resolve(),
            destination=Path(destination).resolve()
        )

        # Check protection before proceeding
        source_protection = self.safeguards.check_recursive_protection(operation.source)
        dest_protection = self.safeguards.check_recursive_protection(operation.destination)

        if source_protection.is_protected:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Protection check failed: {source_protection.reason}"
            self.safeguards.log_violation("move", operation.source, source_protection.reason, was_allowed=False)
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

        if dest_protection.is_protected:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Protection check failed: {dest_protection.reason}"
            self.safeguards.log_violation("move", operation.destination, dest_protection.reason, was_allowed=False)
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

        try:
            # Ensure source exists
            if not operation.source.exists():
                operation.status = OperationStatus.FAILED
                operation.error_message = f"Source file does not exist: {operation.source}"
                self._log_operation(operation)
                return OperationResult(False, operation, operation.error_message)

            # Ensure destination parent exists
            operation.destination.parent.mkdir(parents=True, exist_ok=True)

            # Store rollback info
            rollback_info = {
                "type": "move",
                "original_source": str(operation.source),
                "original_destination": str(operation.destination),
                "timestamp": operation.timestamp
            }

            # Perform the move
            shutil.move(str(operation.source), str(operation.destination))

            operation.rollback_info = rollback_info
            operation.status = OperationStatus.SUCCESS
            self.rollback_stack.append(rollback_info)

            message = f"Successfully moved {operation.source} to {operation.destination}"
            self._log_operation(operation)

            return OperationResult(
                True,
                operation,
                message,
                affected_paths=[operation.source, operation.destination]
            )

        except PermissionError as e:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Permission denied: {str(e)}"
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

        except Exception as e:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Error moving file: {str(e)}"
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

    def delete_file(self, path: Path, confirm: bool = False) -> OperationResult:
        """
        Delete file with protected folder checks and confirmation.

        Args:
            path: File path to delete
            confirm: Whether deletion was confirmed by user

        Returns:
            OperationResult with success status and details
        """
        operation = FileOperation(
            operation_type=OperationType.DELETE,
            source=Path(path).resolve()
        )

        # Check protection before proceeding
        source_protection = self.safeguards.check_recursive_protection(operation.source)

        if source_protection.is_protected:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Protection check failed: {source_protection.reason}"
            self.safeguards.log_violation("delete", operation.source, source_protection.reason, was_allowed=False)
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

        try:
            # Check if confirmation was provided
            if not confirm:
                operation.status = OperationStatus.SKIPPED
                operation.error_message = "Deletion requires confirmation"
                self._log_operation(operation)
                return OperationResult(False, operation, "Deletion requires user confirmation")

            # Ensure file exists
            if not operation.source.exists():
                operation.status = OperationStatus.FAILED
                operation.error_message = f"File does not exist: {operation.source}"
                self._log_operation(operation)
                return OperationResult(False, operation, operation.error_message)

            # Store rollback info (for files, we can't easily rollback deletes,
            # but we log the operation)
            rollback_info = {
                "type": "delete",
                "deleted_path": str(operation.source),
                "was_directory": operation.source.is_dir(),
                "timestamp": operation.timestamp
            }

            # Perform deletion
            if operation.source.is_dir():
                shutil.rmtree(operation.source)
            else:
                operation.source.unlink()

            operation.rollback_info = rollback_info
            operation.status = OperationStatus.SUCCESS
            self.rollback_stack.append(rollback_info)

            message = f"Successfully deleted {operation.source}"
            self._log_operation(operation)

            return OperationResult(
                True,
                operation,
                message,
                affected_paths=[operation.source]
            )

        except PermissionError as e:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Permission denied: {str(e)}"
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

        except Exception as e:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Error deleting file: {str(e)}"
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

    def rename_file(self, old_path: Path, new_path: Path) -> OperationResult:
        """
        Rename file with conflict detection.

        Args:
            old_path: Current file path
            new_path: New file path

        Returns:
            OperationResult with success status and details
        """
        operation = FileOperation(
            operation_type=OperationType.RENAME,
            source=Path(old_path).resolve(),
            destination=Path(new_path).resolve()
        )

        # Check protection before proceeding
        source_protection = self.safeguards.check_recursive_protection(operation.source)
        dest_protection = self.safeguards.check_recursive_protection(operation.destination)

        if source_protection.is_protected:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Protection check failed: {source_protection.reason}"
            self.safeguards.log_violation("rename", operation.source, source_protection.reason, was_allowed=False)
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

        if dest_protection.is_protected:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Protection check failed: {dest_protection.reason}"
            self.safeguards.log_violation("rename", operation.destination, dest_protection.reason, was_allowed=False)
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

        try:
            # Ensure source exists
            if not operation.source.exists():
                operation.status = OperationStatus.FAILED
                operation.error_message = f"Source file does not exist: {operation.source}"
                self._log_operation(operation)
                return OperationResult(False, operation, operation.error_message)

            # Check if destination already exists
            if operation.destination.exists():
                operation.status = OperationStatus.FAILED
                operation.error_message = f"Destination already exists: {operation.destination}"
                self._log_operation(operation)
                return OperationResult(False, operation, operation.error_message)

            # Store rollback info
            rollback_info = {
                "type": "rename",
                "old_name": str(operation.source),
                "new_name": str(operation.destination),
                "timestamp": operation.timestamp
            }

            # Perform rename
            operation.source.rename(operation.destination)

            operation.rollback_info = rollback_info
            operation.status = OperationStatus.SUCCESS
            self.rollback_stack.append(rollback_info)

            message = f"Successfully renamed {operation.source} to {operation.destination}"
            self._log_operation(operation)

            return OperationResult(
                True,
                operation,
                message,
                affected_paths=[operation.source, operation.destination]
            )

        except PermissionError as e:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Permission denied: {str(e)}"
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

        except Exception as e:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Error renaming file: {str(e)}"
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

    def create_folder(self, path: Path) -> OperationResult:
        """
        Create folder with parent validation.

        Args:
            path: Folder path to create

        Returns:
            OperationResult with success status and details
        """
        operation = FileOperation(
            operation_type=OperationType.CREATE_FOLDER,
            destination=Path(path).resolve()
        )

        try:
            # Check if already exists
            if operation.destination.exists():
                operation.status = OperationStatus.SKIPPED
                operation.error_message = f"Folder already exists: {operation.destination}"
                self._log_operation(operation)
                return OperationResult(False, operation, "Folder already exists")

            # Store rollback info
            rollback_info = {
                "type": "create_folder",
                "created_path": str(operation.destination),
                "timestamp": operation.timestamp
            }

            # Create folder with parents
            operation.destination.mkdir(parents=True, exist_ok=True)

            operation.rollback_info = rollback_info
            operation.status = OperationStatus.SUCCESS
            self.rollback_stack.append(rollback_info)

            message = f"Successfully created folder {operation.destination}"
            self._log_operation(operation)

            return OperationResult(
                True,
                operation,
                message,
                affected_paths=[operation.destination]
            )

        except PermissionError as e:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Permission denied: {str(e)}"
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

        except Exception as e:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Error creating folder: {str(e)}"
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

    def deep_nest_structure(self, base_path: Path, structure: Dict[str, Any]) -> OperationResult:
        """
        Create complex nested folder structures.

        Args:
            base_path: Base path for structure
            structure: Dictionary representing folder structure
                {"folder1": {"subfolder1": {}, "file.txt": None}, "folder2": {}}

        Returns:
            OperationResult with success status and details
        """
        operation = FileOperation(
            operation_type=OperationType.DEEP_NEST,
            destination=Path(base_path).resolve()
        )

        try:
            affected_paths = []
            created_paths = []

            def create_structure(current_path: Path, struct: Dict[str, Any]) -> None:
                """Recursively create folder structure."""
                for name, content in struct.items():
                    item_path = current_path / name

                    if content is None or isinstance(content, dict):
                        # It's a folder
                        if not item_path.exists():
                            item_path.mkdir(parents=True, exist_ok=True)
                            created_paths.append(item_path)

                        if isinstance(content, dict):
                            # Recurse into subfolders
                            create_structure(item_path, content)
                    else:
                        # It's a file (content is file content)
                        item_path.parent.mkdir(parents=True, exist_ok=True)
                        if not item_path.exists():
                            item_path.write_text(str(content))
                            created_paths.append(item_path)

            # Create the structure
            create_structure(operation.destination, structure)

            # Store rollback info
            rollback_info = {
                "type": "deep_nest",
                "base_path": str(operation.destination),
                "created_paths": [str(p) for p in created_paths],
                "structure": structure,
                "timestamp": operation.timestamp
            }

            operation.rollback_info = rollback_info
            operation.status = OperationStatus.SUCCESS
            self.rollback_stack.append(rollback_info)

            message = f"Successfully created nested structure at {operation.destination}"
            self._log_operation(operation)

            return OperationResult(
                True,
                operation,
                message,
                affected_paths=created_paths
            )

        except Exception as e:
            operation.status = OperationStatus.FAILED
            operation.error_message = f"Error creating nested structure: {str(e)}"
            self._log_operation(operation)
            return OperationResult(False, operation, operation.error_message)

    def rollback_operation(self, operation_index: int = -1) -> bool:
        """
        Rollback a specific operation (most recent by default).

        Args:
            operation_index: Index of operation to rollback (-1 for most recent)

        Returns:
            True if rollback successful, False otherwise
        """
        if not self.rollback_stack:
            self.logger.warning("No operations to rollback")
            return False

        try:
            # Get the rollback info
            if operation_index == -1:
                rollback_info = self.rollback_stack.pop()
            else:
                if operation_index >= len(self.rollback_stack):
                    self.logger.error(f"Invalid operation index: {operation_index}")
                    return False
                rollback_info = self.rollback_stack.pop(operation_index)

            # Perform rollback based on operation type
            if rollback_info["type"] == "move":
                # Move back to original location
                original_source = Path(rollback_info["original_source"])
                original_dest = Path(rollback_info["original_destination"])

                if original_dest.exists():
                    shutil.move(str(original_dest), str(original_source))

            elif rollback_info["type"] == "rename":
                # Rename back to original name
                old_name = Path(rollback_info["old_name"])
                new_name = Path(rollback_info["new_name"])

                if new_name.exists():
                    new_name.rename(old_name)

            elif rollback_info["type"] == "create_folder":
                # Delete created folder
                created_path = Path(rollback_info["created_path"])
                if created_path.exists():
                    shutil.rmtree(created_path)

            elif rollback_info["type"] == "deep_nest":
                # Delete all created paths in reverse order
                created_paths = [Path(p) for p in rollback_info["created_paths"]]
                for path in reversed(created_paths):
                    if path.exists():
                        if path.is_dir():
                            shutil.rmtree(path)
                        else:
                            path.unlink()

            self.logger.info(f"Successfully rolled back operation: {rollback_info['type']}")
            return True

        except Exception as e:
            self.logger.error(f"Error during rollback: {str(e)}")
            return False

    def get_operation_log(self) -> List[Dict[str, Any]]:
        """Get the complete operation log."""
        return [op.to_dict() for op in self.operation_log]

    def clear_operation_log(self) -> None:
        """Clear the operation log."""
        self.operation_log.clear()
        self.rollback_stack.clear()

    def _log_operation(self, operation: FileOperation) -> None:
        """Log an operation to the operation log."""
        self.operation_log.append(operation)
        self.logger.info(f"Operation logged: {operation.operation_type.value} - {operation.status.value}")