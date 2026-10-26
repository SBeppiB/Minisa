"""Qdrant adapter with filtered dense and sparse candidate retrieval."""

from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from .embeddings import sparse
from .models import Memory


@dataclass
class Hit:
    id: str
    semantic: float = 0
    lexical: float = 0


class VectorIndexBackend(Protocol):
    def upsert(self, memories: list[Memory], vectors: list[list[float]]) -> None: ...
    def delete(self, ids: list[str]) -> None: ...
    def search(
        self, query: str, vector: list[float], namespace: str, tiers: list[str], limit: int
    ) -> list[Hit]: ...
    def close(self) -> None: ...


VectorIndex = VectorIndexBackend  # Backwards-compatible protocol name.


class QdrantIndex:
    def __init__(
        self,
        *,
        dimension: int,
        collection: str,
        path: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
    ):
        self.remote = bool(url)
        self.last_search_profile = {}
        self.client = (
            QdrantClient(url=url, api_key=api_key, timeout=5) if url else QdrantClient(path=path)
        )
        self.collection = collection
        try:
            self._initialize(dimension, url)
        except Exception:
            self.client.close()
            raise

    def _initialize(self, dimension: int, url: str | None) -> None:
        collection = self.collection
        self.created = not self.client.collection_exists(collection)
        if self.created:
            self.client.create_collection(
                collection,
                vectors_config={
                    "dense": qm.VectorParams(size=dimension, distance=qm.Distance.COSINE)
                },
                **(
                    {
                        "hnsw_config": qm.HnswConfigDiff(
                            m=16, ef_construct=100, full_scan_threshold=1000
                        ),
                        "optimizers_config": qm.OptimizersConfigDiff(indexing_threshold=1000),
                    }
                    if url
                    else {}
                ),
                sparse_vectors_config={"lexical": qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
            )
        if url:
            for field in ("namespace", "tier", "entity_ids"):
                self.client.create_payload_index(collection, field, qm.PayloadSchemaType.KEYWORD)

    def upsert(self, memories: list[Memory], vectors: list[list[float]]) -> None:
        points = []
        for memory, vector in zip(memories, vectors, strict=True):
            indices, values = sparse(memory.content)
            points.append(
                qm.PointStruct(
                    id=memory.id,
                    vector={
                        "dense": vector,
                        "lexical": qm.SparseVector(indices=indices, values=values),
                    },
                    payload={
                        "namespace": memory.namespace,
                        "tier": memory.tier.value,
                        "entity_ids": memory.entity_ids,
                    },
                )
            )
        if points:
            self.client.upsert(self.collection, points, wait=True)

    def delete(self, ids: list[str]) -> None:
        if ids:
            self.client.delete(self.collection, qm.PointIdsList(points=ids), wait=True)

    def search(
        self, query: str, vector: list[float], namespace: str, tiers: list[str], limit: int
    ) -> list[Hit]:
        return self.search_filtered(query, vector, namespace, tiers, limit)

    def search_filtered(self, query, vector, namespace, tiers, limit, *, entity_ids=None):
        started = perf_counter()
        filters = qm.Filter(
            must=[
                qm.FieldCondition(key="namespace", match=qm.MatchValue(value=namespace)),
                qm.FieldCondition(key="tier", match=qm.MatchAny(any=tiers)),
            ]
        )
        if entity_ids:
            filters.must.append(
                qm.FieldCondition(key="entity_ids", match=qm.MatchAny(any=entity_ids))
            )
        indices, values = sparse(query)
        dense = self.client.query_points(
            self.collection,
            query=vector,
            using="dense",
            query_filter=filters,
            limit=limit,
            search_params=qm.SearchParams(exact=False, hnsw_ef=128) if self.remote else None,
            with_payload=False,
            with_vectors=False,
        ).points
        dense_done = perf_counter()
        lexical = (
            self.client.query_points(
                self.collection,
                query=qm.SparseVector(indices=indices, values=values),
                using="lexical",
                query_filter=filters,
                limit=limit,
                with_payload=False,
                with_vectors=False,
            ).points
            if indices
            else []
        )
        self.last_search_profile = {
            "dense_ms": (dense_done - started) * 1000,
            "sparse_ms": (perf_counter() - dense_done) * 1000,
        }
        hits = {str(p.id): Hit(str(p.id), max(0, p.score), 0) for p in dense}
        for p in lexical:
            hit = hits.setdefault(str(p.id), Hit(str(p.id)))
            hit.lexical = p.score / (1 + p.score)
        return list(hits.values())

    def close(self) -> None:
        self.client.close()


class LocalQdrantBackend(QdrantIndex):
    """Offline convenience index; embedded dense search is linear, not HNSW."""

    def __init__(self, **kwargs):
        if kwargs.get("url"):
            raise ValueError("LocalQdrantBackend does not accept a server URL")
        if not kwargs.get("path"):
            raise ValueError("LocalQdrantBackend requires a local path")
        super().__init__(**kwargs)


class QdrantServerBackend(QdrantIndex):
    """Server-managed HNSW and sparse indexes, with bounded filtered queries."""

    def __init__(self, **kwargs):
        if not kwargs.get("url"):
            raise ValueError("QdrantServerBackend requires a URL")
        super().__init__(**kwargs)
