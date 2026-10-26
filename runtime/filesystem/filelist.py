from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from datetime import datetime
import hashlib

from runtime.filesystem.models import FileChange


class FilelistTracker:
    """Track filesystem state in filelist.md format."""
    
    def __init__(self):
        """Initialize the filelist tracker."""
        self.deleted_items: Set[str] = set()
        
    def generate_filelist(self, root_path: Path) -> str:
        """
        Generate filelist.md from current filesystem state.
        
        Args:
            root_path: Root directory to scan
            
        Returns:
            String content of filelist.md
        """
        root_path = Path(root_path).resolve()
        
        if not root_path.exists():
            return f"# Error: Root path does not exist: {root_path}"
        
        lines = []
        lines.append(f"# Filelist for {root_path}")
        lines.append(f"# Generated: {datetime.now().astimezone().isoformat()}")
        lines.append("")
        
        # Generate tree structure
        tree_lines = self._generate_tree(root_path, root_path, "", True)
        lines.extend(tree_lines)
        
        # Add deleted items section
        if self.deleted_items:
            lines.append("")
            lines.append("# Deleted items (marked with x):")
            for deleted_item in sorted(self.deleted_items):
                lines.append(f"# x {deleted_item}")
        
        return "\n".join(lines)
    
    def _generate_tree(self, root_path: Path, current_path: Path, prefix: str, is_last: bool) -> List[str]:
        """
        Generate tree structure for a directory.
        
        Args:
            root_path: Root directory for relative paths
            current_path: Current directory being processed
            prefix: Prefix for tree formatting
            is_last: Whether this is the last item in its parent
            
        Returns:
            List of strings representing the tree structure
        """
        lines = []
        
        try:
            # Get relative path from root
            try:
                rel_path = current_path.relative_to(root_path)
            except ValueError:
                rel_path = current_path
            
            # Get display name
            if current_path == root_path:
                # The root is a display-only tree node. Keep it to one path
                # component so snapshots are portable between directories.
                display_name = root_path.name
            else:
                display_name = rel_path.name
            
            # Add current item
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{display_name}")
            
            # If it's a directory, add children
            if current_path.is_dir():
                try:
                    children = sorted(current_path.iterdir(), key=lambda x: (not x.is_dir(), x.name))
                    
                    # Update prefix for children
                    child_prefix = prefix + ("    " if is_last else "│   ")
                    
                    for i, child in enumerate(children):
                        is_last_child = (i == len(children) - 1)
                        child_lines = self._generate_tree(root_path, child, child_prefix, is_last_child)
                        lines.extend(child_lines)
                        
                except PermissionError:
                    lines.append(f"{prefix}    (Permission denied)")
                    
        except Exception as e:
            lines.append(f"{prefix}    (Error: {str(e)})")
        
        return lines
    
    def mark_deleted(self, filelist_path: Path, item_path: str) -> None:
        """
        Mark an item as deleted with 'x' prefix.
        
        Args:
            filelist_path: Path to filelist.md
            item_path: Relative path of deleted item
        """
        self.deleted_items.add(item_path)
        
        # Update the filelist.md file if it exists
        if filelist_path.exists():
            self._update_deleted_section(filelist_path)
    
    def _update_deleted_section(self, filelist_path: Path) -> None:
        """Update the deleted section in filelist.md."""
        try:
            content = filelist_path.read_text(encoding="utf-8")
            lines = content.splitlines()
            
            # Find or create deleted section
            deleted_section_start = -1
            deleted_section_end = -1
            
            for i, line in enumerate(lines):
                if line.strip().startswith("# Deleted items"):
                    deleted_section_start = i
                elif deleted_section_start != -1 and line.strip() and not line.strip().startswith("#"):
                    deleted_section_end = i
                    break
            
            # Rebuild deleted section
            if deleted_section_start != -1:
                # Remove old deleted section
                if deleted_section_end != -1:
                    lines = lines[:deleted_section_start] + lines[deleted_section_end:]
                else:
                    lines = lines[:deleted_section_start]
            
            # Add new deleted section
            if self.deleted_items:
                lines.append("")
                lines.append("# Deleted items (marked with x):")
                for deleted_item in sorted(self.deleted_items):
                    lines.append(f"# x {deleted_item}")
            
            # Write back
            filelist_path.write_text("\n".join(lines), encoding="utf-8")
            
        except Exception as e:
            print(f"Error updating deleted section: {str(e)}")
    
    def verify_integrity(self, filelist_path: Path, root_path: Path) -> Dict[str, Any]:
        """
        Verify filelist matches actual filesystem.
        
        Args:
            filelist_path: Path to filelist.md
            root_path: Root directory to verify against
            
        Returns:
            Dictionary with verification results
        """
        root_path = Path(root_path).resolve()
        results = {
            "valid": True,
            "missing_files": [],
            "extra_files": [],
            "deleted_items_mismatch": [],
            "timestamp": datetime.now().astimezone().isoformat()
        }
        
        if not filelist_path.exists():
            results["valid"] = False
            results["error"] = "filelist.md does not exist"
            return results
        
        try:
            # Get current filesystem state
            current_files = set()
            for item in root_path.rglob("*"):
                try:
                    rel_path = item.relative_to(root_path)
                    current_files.add(str(rel_path).replace("\\", "/"))
                except ValueError:
                    continue
            
            # Parse filelist to get expected files using the improved parser
            content = filelist_path.read_text(encoding="utf-8")
            expected_files = self._parse_filelist(content)
            
            # Convert to consistent format
            expected_files = {f.replace("\\", "/") for f in expected_files}
            
            # Compare
            results["missing_files"] = list(expected_files - current_files)
            results["extra_files"] = list(current_files - expected_files)
            
            # Check deleted items
            if self.deleted_items:
                still_exists = []
                for deleted_item in self.deleted_items:
                    if (root_path / deleted_item).exists():
                        still_exists.append(deleted_item)
                results["deleted_items_mismatch"] = still_exists
            
            # Determine if valid (allow filelist.md itself as extra)
            extra_files_filtered = [f for f in results["extra_files"] if f != "filelist.md"]
            results["valid"] = (
                len(results["missing_files"]) == 0 and
                len(extra_files_filtered) == 0 and
                len(results["deleted_items_mismatch"]) == 0
            )
            
        except Exception as e:
            results["valid"] = False
            results["error"] = str(e)
        
        return results
    
    def compare_states(self, before: str, after: str) -> List[FileChange]:
        """
        Compare two filelist states and identify changes.
        
        Args:
            before: Previous filelist content
            after: New filelist content
            
        Returns:
            List of FileChange objects
        """
        changes = []
        
        # Parse both filelists
        before_files = self._parse_filelist(before)
        after_files = self._parse_filelist(after)
        
        # Normalize paths
        before_files = {f.replace("\\", "/") for f in before_files}
        after_files = {f.replace("\\", "/") for f in after_files}
        
        # Find added files
        added = after_files - before_files
        for file_path in added:
            changes.append(FileChange(
                change_type="added",
                path=file_path,
                timestamp=datetime.now().astimezone().isoformat()
            ))
        
        # Find deleted files
        deleted = before_files - after_files
        for file_path in deleted:
            changes.append(FileChange(
                change_type="deleted",
                path=file_path,
                timestamp=datetime.now().astimezone().isoformat()
            ))
        
        return changes
    
    def _parse_filelist(self, content: str) -> Set[str]:
        """Parse filelist content into paths relative to the snapshot root."""
        files = set()
        path_stack = []
        root_seen = False
        
        for line in content.splitlines():
            line = line.rstrip()
            
            # Skip comments, empty lines, and deleted items
            if line.startswith("#") or not line or "x " in line:
                continue
            
            # Calculate depth based on indentation
            depth = 0
            temp_line = line
            while temp_line.startswith("│   ") or temp_line.startswith("    "):
                depth += 1
                temp_line = temp_line[4:]
            
            # Extract the filename
            clean_line = temp_line.replace("├── ", "").replace("└── ", "").strip()
            
            # Skip error messages and non-file entries
            if not clean_line or clean_line.startswith("(Error") or clean_line.startswith("(Permission"):
                continue

            # The first depth-zero item is the display-only root emitted by
            # generate_filelist. It must never become part of a relative path.
            if not root_seen and depth == 0:
                root_seen = True
                continue

            if not root_seen:
                # Be tolerant of older hand-written snapshots without a root
                # node by treating their first item as a relative entry.
                relative_depth = depth
            else:
                relative_depth = max(0, depth - 1)
            
            # Adjust path stack based on depth
            while len(path_stack) > relative_depth:
                path_stack.pop()
            
            # Add current path
            path_stack.append(clean_line)
            
            # Add full path to files set
            full_path = "/".join(path_stack)
            files.add(full_path)
        
        return files
    
    def update_filelist(self, filelist_path: Path, operation: Any) -> None:
        """
        Update filelist after a file operation.
        
        Args:
            filelist_path: Path to filelist.md
            operation: FileOperation object
        """
        if not filelist_path.exists():
            return
        
        try:
            # Regenerate the filelist with current state
            # (In a real implementation, we'd be smarter about this)
            # For now, we'll just mark deleted items appropriately
            if hasattr(operation, 'operation_type'):
                from runtime.filesystem.models import OperationType
                
                if operation.operation_type == OperationType.DELETE and operation.source:
                    # Mark as deleted
                    rel_path = operation.source.name  # Simplified
                    self.mark_deleted(filelist_path, rel_path)
                    
        except Exception as e:
            print(f"Error updating filelist: {str(e)}")
    
    def calculate_checksum(self, file_path: Path) -> str:
        """
        Calculate checksum for a file for integrity verification.
        
        Args:
            file_path: Path to file
            
        Returns:
            SHA256 checksum string
        """
        if not file_path.exists() or file_path.is_dir():
            return ""
        
        try:
            sha256_hash = hashlib.sha256()
            with open(file_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
        except Exception:
            return ""
    
    def load_deleted_items(self, filelist_path: Path) -> None:
        """Load deleted items from existing filelist.md."""
        if not filelist_path.exists():
            return
        
        try:
            content = filelist_path.read_text(encoding="utf-8")
            in_deleted_section = False
            
            for line in content.splitlines():
                line = line.strip()
                
                if line.startswith("# Deleted items"):
                    in_deleted_section = True
                    continue
                    
                if in_deleted_section:
                    if line.startswith("# x "):
                        item = line[4:]  # Remove "# x "
                        self.deleted_items.add(item)
                    elif line and not line.startswith("#"):
                        # End of deleted section
                        break
                        
        except Exception as e:
            print(f"Error loading deleted items: {str(e)}")
