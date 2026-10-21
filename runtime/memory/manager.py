from pathlib import Path
import re
import os
from uuid import uuid4
from runtime.memory.facts import fact_record, fact_identity
from runtime.storage import atomic_write, serialized, sync_directories, check_target


class MemoryManager:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.memory_path = root / "MEMORY"
        self.active_path = self.memory_path / "active"
        self.archive_path = self.memory_path / "archive"
        self.checklist_file = self.memory_path / "checklist.md"
        self.trash_file = self.memory_path / "Minisa Mem Trash.txt"

    def list_active_memories(self) -> list[Path]:
        return sorted(self.active_path.glob("memory_*.md"))

    def list_archived_memories(self) -> list[Path]:
        return sorted(self.archive_path.glob("memory_*.md"))

    def read_memory(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def read_recallable_memory(self, path: Path) -> str:
        """Return the authoritative fact portion of an active note only."""
        if path.parent != self.active_path or path.is_symlink() or not path.is_file():
            raise ValueError("Only an active memory note is recallable.")

        content = self.read_memory(path)
        record = fact_record(content)
        fact = record["fact"] or "[No supported nonempty fact; record requires review]"
        header = []
        for line in content.splitlines():
            if line.startswith("## "):
                break
            if line.startswith("# ") or re.match(
                r"^(?:ID|Type|Subject|Source|Status|Confidence|Created|Scope|Principal|Verified by):", line
            ):
                header.append(line)

        projection = "\n".join([*header, "", "## Fact", "", fact]).strip() + "\n"
        if record["projection_status"] != "supported":
            # JSON prevents unknown Markdown from being parsed as another fact.
            import json
            projection += "\nProjection review: " + json.dumps(record, ensure_ascii=False) + "\n"
        return projection

    def next_memory_id(self) -> str:
        numbers = []

        # Archived IDs are persistent history and must not be reused.
        for path in [*self.list_active_memories(), *self.list_archived_memories()]:
            match = re.match(r"memory_(\d+)(?:\s*-\s*.*)?\.md$", path.name)

            if match:
                numbers.append(int(match.group(1)))

        next_number = max(numbers, default=0) + 1
        return f"memory_{next_number:03d}"

    def memory_exists(self, fact: str) -> bool:
        wanted = fact_identity(fact)
        if not wanted:
            return False
        for path in self.list_active_memories():
            record = fact_record(self.read_memory(path))
            if (record["projection_status"] == "supported"
                    and wanted == fact_identity(record["fact"])):
                return True
        return False

    @serialized
    def create_memory(
        self,
        memory_id: str,
        title: str,
        content: str,
    ) -> Path:
        if not re.fullmatch(r"memory_[0-9]+", memory_id):
            raise ValueError("Invalid memory ID")
        number = int(memory_id.split("_")[1])
        for existing in [*self.list_active_memories(), *self.list_archived_memories()]:
            match = re.match(r"memory_(\d+)", existing.name)
            if match and int(match.group(1)) == number:
                raise FileExistsError("Memory ID already allocated")
        check_target(self.root, self.active_path / "placeholder")
        self.active_path.mkdir(parents=True, exist_ok=True)

        safe_title = re.sub(r'[\\/:*?"<>|]', "", title)
        safe_title = " ".join(safe_title.split()).strip()
        safe_title = safe_title[:80] or "Untitled Memory"

        filename = f"{memory_id} - {safe_title}.md"
        path = self.active_path / filename

        self.last_receipt = atomic_write(path, content, root=self.root)
        return path

    def find_by_title(self, title: str) -> Path | None:
        """Resolve a [[WikiLink]] target to one active memory note."""
        wanted = self._normalize(title)
        for path in self.list_active_memories():
            first_line = self.read_memory(path).splitlines()[0:1]
            if first_line and self._normalize(first_line[0].removeprefix("#")) == wanted:
                return path
        return None

    def linked_memories(self, path: Path, limit: int = 3) -> list[Path]:
        """Return directly linked active notes only; never recurse."""
        titles = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", self.read_memory(path))
        matches = []
        seen = {path}
        for title in titles:
            target = self.find_by_title(title.strip())
            if target is not None and target not in seen:
                matches.append(target)
                seen.add(target)
            if len(matches) >= limit:
                break
        return matches

    def matching_titles(self, text: str, exclude: str = "") -> list[str]:
        """Return existing note titles explicitly mentioned in text."""
        normalized_text = self._normalize(text)
        excluded = self._normalize(exclude)
        matches = []

        for path in self.list_active_memories():
            first_line = self.read_memory(path).splitlines()[0:1]
            if not first_line:
                continue

            title = first_line[0].removeprefix("#").strip()
            normalized_title = self._normalize(title)

            if (
                normalized_title
                and normalized_title != excluded
                and normalized_title in normalized_text
                and title not in matches
            ):
                matches.append(title)

        return matches

    def search_titles(self, query: str, limit: int = 3) -> list[Path]:
        """Find notes whose meaningful title words occur in a normal question."""
        query_words = set(re.findall(r"[a-z0-9']+", query.lower()))
        ignored = {"about", "created", "what", "know", "tell", "me", "is", "a", "the", "of", "by"}
        query_words -= ignored
        scored = []

        for path in self.list_active_memories():
            first_line = self.read_memory(path).splitlines()[0:1]
            if not first_line:
                continue
            title_words = set(re.findall(r"[a-z0-9']+", first_line[0].lower())) - ignored
            score = len(query_words & title_words)
            if score:
                scored.append((score, path))

        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [path for _, path in scored[:limit]]

    def find_forget_candidates(self, query: str) -> list[Path]:
        """Fail closed: identity or a complete fact, never generic word overlap.

        The only paraphrase admitted is first-person like/enjoy with an identical
        complete subject. Negation, qualifiers and word order are preserved.
        All plausible exact matches are returned; uniqueness belongs to actions.
        """
        def tokens(value):
            return tuple(re.findall(r"[+-]?\d+(?:[.,]\d+)*|\w+(?:['’]\w+)*|[^\w\s.,!?;:\[\]]", value.casefold()))

        def fact_key(value):
            words = tokens(value)
            if len(words) > 2 and words[:2] in (("i", "like"), ("i", "enjoy")):
                return ("i", "like/enjoy", *words[2:])
            return words

        wanted = tokens(query)
        if not wanted:
            return []
        candidates = []
        for path in self.list_active_memories():
            content = self.read_recallable_memory(path)
            lines = content.splitlines()
            title = lines[0].removeprefix("#").strip() if lines else ""
            identifier = re.match(r"memory_\d+", path.stem)
            record = fact_record(self.read_memory(path))
            fact = record["fact"] if record["projection_status"] == "supported" else ""
            if (wanted == tokens(title)
                    or (identifier and wanted == tokens(identifier.group()))
                    or (fact and fact_key(query) == fact_key(fact))):
                candidates.append(path)
        return candidates

    @serialized
    def archive_memory(self, path: Path, query: str) -> dict:
        """Atomically move one active memory into the archive."""
        if path.parent != self.active_path or path.is_symlink() or not path.is_file():
            raise ValueError("Only an active memory note can be archived.")

        check_target(self.root, path)
        check_target(self.root, self.archive_path / "placeholder")
        check_target(self.root, self.trash_file)
        self.archive_path.mkdir(parents=True, exist_ok=True)
        timestamp = __import__("datetime").datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        destination = self.archive_path / f"{path.stem} - archived_{timestamp}_{uuid4().hex}{path.suffix}"
        # Sync source bytes before the primary rename commit point.
        with path.open("rb") as source:
            os.fsync(source.fileno())
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("Archive destination already exists")
        path.replace(destination)

        warnings = sync_directories(self.active_path, self.archive_path, self.memory_path)
        durable = not warnings
        try:
            with self.trash_file.open("a", encoding="utf-8") as log:
                log.write(f"{timestamp} | archived: {path.name} | query: {query.strip()}\n")
                log.flush()
                os.fsync(log.fileno())
        except Exception as error:
            warnings.append(f"Archive log maintenance failed: {error}")

        # Embeddings are derived state; an archived note is no longer eligible.
        from runtime.memory.index import MemoryIndex

        for versioned in (False, True):
            try:
                index = MemoryIndex(self.memory_path.parent, versioned=versioned)
                if versioned and not index.path.exists():
                    continue
                maintenance = index.remove(path.stem)
                if not maintenance["durable"]:
                    warnings.extend(maintenance["warnings"])
            except Exception as error:
                warnings.append(f"Index maintenance failed: {error}")

        return {"status": "committed", "committed": True, "durable": durable, "path": str(destination), "warnings": warnings}

    def _normalize(self, text: str) -> str:
        return " ".join(text.lower().split())
