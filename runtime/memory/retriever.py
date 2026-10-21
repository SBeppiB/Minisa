from pathlib import Path
import re

from runtime.memory.manager import MemoryManager


class MemoryRetriever:
    def __init__(self, memory: MemoryManager):
        self.memory = memory

    def retrieve(
        self,
        query: str,
        limit: int = 5,
    ) -> list[str]:

        query_words = self._words(query)

        if not query_words:
            return []

        scored = []

        for path in self.memory.list_active_memories():
            content = self.memory.read_memory(path)
            words = self._words(content)

            score = len(query_words & words)

            if score > 0:
                scored.append((score, path, content))

        scored.sort(
            key=lambda item: (-item[0], item[1].name)
        )

        return [
            content
            for _, _, content in scored[:limit]
        ]

    def _words(self, text: str) -> set[str]:
        return set(
            re.findall(
                r"\b[a-zA-Z0-9']+\b",
                text.lower()
            )
        )
