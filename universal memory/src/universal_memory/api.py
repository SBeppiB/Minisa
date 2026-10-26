"""FastAPI adapter. A deployment serves one fixed namespace; clients cannot override it."""

import hmac
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .engine import MemoryEngine
from .models import MemoryInput, Tier


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=100_000)
    top_k: int = Field(default=10, ge=1, le=1000)
    entity_ids: list[str] = Field(default_factory=list, max_length=100)
    strict_entities: bool = False
    context: str = Field(default="", max_length=100_000)
    include_archived: bool = True
    promote: bool | None = None


class ConsolidateRequest(BaseModel):
    ids: list[str] = Field(min_length=2, max_length=1000)
    content: str = Field(min_length=1, max_length=100_000)


class LinkRequest(BaseModel):
    target: str
    kind: str = "related"
    bidirectional: bool = True


class SupersedeRequest(BaseModel):
    new_id: str


def create_app(engine: MemoryEngine | None = None, *, api_key: str | None = None) -> FastAPI:
    """Pass an engine for embedding; otherwise configure with UMEM_* environment variables."""
    owned = engine is None
    key = api_key if api_key is not None else os.getenv("UMEM_API_KEY")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if engine is not None:
            app.state.engine = engine
        else:
            from .embeddings import SentenceTransformerEmbedder

            model = os.getenv("UMEM_EMBEDDING_MODEL")
            app.state.engine = MemoryEngine(
                os.getenv("UMEM_DATA", "./data"),
                namespace=os.getenv("UMEM_NAMESPACE", "default"),
                embedder=SentenceTransformerEmbedder(model) if model else None,
                qdrant_url=os.getenv("UMEM_QDRANT_URL"),
                qdrant_api_key=os.getenv("UMEM_QDRANT_API_KEY"),
            )
        yield
        if owned:
            app.state.engine.close()

    def authorize(authorization: Annotated[str | None, Header()] = None):
        if key and (
            authorization is None or not hmac.compare_digest(authorization, f"Bearer {key}")
        ):
            raise HTTPException(401, "Invalid API credentials")

    app = FastAPI(
        title="Universal Memory",
        version="0.1.0",
        lifespan=lifespan,
        dependencies=[Depends(authorize)],
    )

    def memory(request: Request) -> MemoryEngine:
        return request.app.state.engine

    Engine = Annotated[MemoryEngine, Depends(memory)]

    @app.exception_handler(KeyError)
    async def not_found(request: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": "Memory not found"})

    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def invalid_model(request: Request, exc: ValidationError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.post("/memories", status_code=201)
    def remember(body: MemoryInput, engine: Engine):
        return engine.remember(**body.model_dump())

    @app.post("/recall")
    def recall(body: RecallRequest, engine: Engine):
        return engine.recall(**body.model_dump())

    @app.post("/memories/batch", status_code=201)
    def remember_batch(body: list[MemoryInput], engine: Engine):
        return engine.remember_many(body)

    @app.get("/memories")
    def listing(
        engine: Engine,
        limit: int = Query(100, ge=1, le=1000),
        after: str = "",
        tier: Tier | None = None,
        entity_id: str | None = None,
        tag: str | None = None,
        category: str | None = None,
    ):
        return engine.list(
            limit=limit, after=after, tier=tier, entity_id=entity_id, tag=tag, category=category
        )

    @app.get("/memories/{id}")
    def get(id: str, engine: Engine):
        return engine.get(id)

    @app.patch("/memories/{id}")
    def edit(id: str, body: dict[str, Any], engine: Engine):
        return engine.edit(id, **body)

    @app.delete("/memories/{id}", status_code=204)
    def delete(id: str, engine: Engine):
        engine.delete(id)

    def lifecycle(action):
        def endpoint(id: str, engine: Engine):
            return getattr(engine, action)(id)

        endpoint.__name__ = action
        return endpoint

    for action in ("archive", "restore", "forget", "pin", "unpin"):
        app.add_api_route(f"/memories/{{id}}/{action}", lifecycle(action), methods=["POST"])

    @app.get("/memories/{id}/history")
    def history(id: str, engine: Engine):
        return engine.history(id)

    @app.post("/memories/{id}/links", status_code=204)
    def link(id: str, body: LinkRequest, engine: Engine):
        engine.link(id, **body.model_dump())

    @app.post("/memories/{id}/supersede")
    def supersede(id: str, body: SupersedeRequest, engine: Engine):
        return engine.supersede(id, body.new_id)

    @app.post("/consolidations", status_code=201)
    def consolidate(body: ConsolidateRequest, engine: Engine):
        return engine.consolidate(body.ids, body.content)

    @app.get("/consolidations/{id}")
    def consolidation(id: str, engine: Engine):
        return engine.consolidation(id)

    @app.post("/consolidations/{id}/reverse", status_code=204)
    def reverse(id: str, engine: Engine):
        engine.reverse_consolidation(id)

    @app.get("/stats")
    def stats(engine: Engine):
        return engine.stats()

    @app.get("/health")
    def health(engine: Engine):
        return engine.health()

    @app.get("/backend-info")
    def backend_info(engine: Engine):
        return engine.backend_info()

    @app.get("/retrieval-logs")
    def logs(engine: Engine, limit: int = Query(100, ge=1, le=1000)):
        return engine.retrieval_logs(limit)

    @app.get("/duplicates")
    def duplicates(engine: Engine, limit: int = Query(100, ge=1, le=1000)):
        return engine.duplicates(limit=limit)

    @app.post("/maintenance/decay")
    def decay(engine: Engine, dry_run: bool = True):
        return {"candidates": engine.decay(dry_run=dry_run), "dry_run": dry_run}

    @app.post("/maintenance/rebuild-index")
    def rebuild(engine: Engine):
        return engine.rebuild_index()

    @app.post("/maintenance/sync-index")
    def sync(engine: Engine):
        return {"processed": engine.sync_index()}

    return app
