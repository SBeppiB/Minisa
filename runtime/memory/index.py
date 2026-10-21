import json
from copy import deepcopy
from pathlib import Path
from runtime.storage import atomic_write, serialized


class MemoryIndex:
    def __init__(self, root: Path, *, versioned: bool = False):
        self.root = Path(root)
        self.path = self.root / "MEMORY" / ("index-v2.json" if versioned else "index.json")
        self._index = None
        self._baseline = None

    def load(self) -> dict:
        # Reload to observe other cooperating writers; retain the legacy format.
        index = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(index, dict):
            raise ValueError("Invalid memory index; refusing to replace it")
        self._index = index
        self._baseline = deepcopy(index)
        return index

    @serialized
    def save(self, index: dict | None = None) -> dict:
        desired = dict(index if index is not None else (self._index or {}))
        baseline = self._baseline
        if baseline is None and self.path.exists():
            raise ValueError("Load index before saving a replacement")
        current = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(current, dict):
            raise ValueError("Invalid memory index; refusing to replace it")
        # Whole-document API uses compare-and-swap, never silent stale overwrite.
        if baseline is not None and current != baseline:
            raise ValueError("Stale memory index snapshot")
        receipt = atomic_write(self.path, json.dumps(desired, indent=2, allow_nan=False), root=self.root, replace=True)
        self._index = desired
        self._baseline = deepcopy(desired)
        return receipt

    def get(self, memory_id: str) -> list[float] | None:
        return self.load().get(memory_id)

    @serialized
    def set(self, memory_id: str, embedding: list[float]) -> dict:
        index = self.load()
        index[memory_id] = embedding
        return self.save(index)

    @serialized
    def remove(self, memory_id: str) -> dict:
        index = self.load()
        index.pop(memory_id, None)
        return self.save(index)
