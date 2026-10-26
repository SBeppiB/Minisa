"""Replaceable local embedding providers. Hash embeddings are a lexical baseline only."""

import hashlib
import math
import re
from collections import Counter
from typing import Protocol


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold(), flags=re.UNICODE)


def sparse(text: str) -> tuple[list[int], list[float]]:
    counts: Counter[int] = Counter(
        int.from_bytes(hashlib.blake2s(t.encode(), digest_size=4).digest(), "little")
        for t in tokens(text)
    )
    indices = sorted(counts)
    # Saturating term frequency; Qdrant supplies collection-wide IDF.
    return indices, [counts[i] * 2.2 / (counts[i] + 1.2) for i in indices]


class Embedder(Protocol):
    dimension: int
    fingerprint: str

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Download-free deterministic baseline, NOT a semantic language model."""

    dimension = 384
    fingerprint = "hash-blake2s-384-v1"

    def encode(self, texts: list[str]) -> list[list[float]]:
        result = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in tokens(text):
                digest = hashlib.blake2s(token.encode(), digest_size=8).digest()
                vector[int.from_bytes(digest[:4], "little") % self.dimension] += (
                    1 if digest[4] % 2 else -1
                )
            norm = math.sqrt(sum(v * v for v in vector)) or 1
            result.append([v / norm for v in vector])
        return result


class SentenceTransformerEmbedder:
    def __init__(
        self,
        model: str = "sentence-transformers/all-MiniLM-L6-v2",
        *,
        local_files_only: bool = False,
        batch_size: int = 64,
    ):
        from sentence_transformers import SentenceTransformer

        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size
        self.model = SentenceTransformer(model, local_files_only=local_files_only)
        self.dimension = self.model.get_sentence_embedding_dimension()
        self.fingerprint = f"sentence-transformers:{model}:{self.dimension}"

    def encode(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(
            texts, normalize_embeddings=True, batch_size=self.batch_size
        ).tolist()
