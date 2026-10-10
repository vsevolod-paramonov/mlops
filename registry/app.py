"""HTTP layer. Run: python -m uvicorn registry.app:app --host 127.0.0.1."""

from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import sqlite3
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from registry.db import Conflict, NotFound, RegistryDB
from registry.schemas import AliasInfo, AliasName, AliasSet, ModelCreate, ModelInfo, VersionInfo, VersionMetadata
from registry.storage import ArtifactStore, ArtifactTooLarge, CorruptArtifact, EmptyArtifact

logger = logging.getLogger(__name__)
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0)]


def create_app(data_dir: Path | None = None, max_artifact_bytes: int = 10 * 1024 * 1024) -> FastAPI:
    root = Path(data_dir or os.environ.get("MODEL_REGISTRY_DATA_DIR", "data")).resolve()
    db = RegistryDB(root / "registry.sqlite3")
    storage = ArtifactStore(root / "artifacts", max_artifact_bytes)

    @asynccontextmanager
    async def lifespan(app):
        storage.initialize()
        db.initialize()
        yield

    app = FastAPI(
        title="Учебный Model Registry", version="1.0.0", lifespan=lifespan,
        description="Модели, неизменяемые версии и указатели staging/production. Только доверенный localhost.",
    )
    app.state.db = db
    app.state.storage = storage

    @app.exception_handler(NotFound)
    async def not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(Conflict)
    @app.exception_handler(CorruptArtifact)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(sqlite3.OperationalError)
    async def database_unavailable(request, exc):
        logger.error("SQLite operation failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Database unavailable; retry later"})

    @app.exception_handler(OSError)
    async def storage_unavailable(request, exc):
        logger.error("Storage operation failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Storage unavailable"})

    # Regular def handlers run in FastAPI's threadpool: SQLite and file I/O block.
    @app.get("/health", tags=["Service"])
    def health():
        with db.connection() as conn:
            conn.execute("SELECT count(*) FROM models").fetchone()
        return {"status": "ok"}

    @app.post("/models", response_model=ModelInfo, status_code=201, tags=["Models"])
    def create_model(body: ModelCreate):
        return db.create_model(body.model_dump())

    @app.get("/models", response_model=list[ModelInfo], tags=["Models"])
    def list_models(team: str | None = None, name: str | None = None,
                    limit: PageLimit = 50, offset: PageOffset = 0):
        return db.list_models(team, name, limit, offset)

    @app.get("/models/{model_id}", response_model=ModelInfo, tags=["Models"])
    def get_model(model_id: int):
        return db.get_model(model_id)

    @app.post("/models/{model_id}/versions", response_model=VersionInfo, status_code=201, tags=["Versions"])
    def upload_version(model_id: int, artifact: UploadFile,
                       metadata: Annotated[str, Form(max_length=65536, description="JSON matching VersionMetadata; see docs/api.md")]):
        db.get_model(model_id)
        try:
            parsed = VersionMetadata.model_validate_json(metadata)
        except ValidationError as exc:
            # Avoid echoing arbitrary file content or large invalid user input.
            errors = [{"loc": list(error["loc"]), "msg": error["msg"]} for error in exc.errors()]
            raise HTTPException(422, detail=errors) from exc
        try:
            saved = storage.save(artifact.file)
        except EmptyArtifact as exc:
            raise HTTPException(400, str(exc)) from exc
        except ArtifactTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        try:
            return db.register_version(model_id, parsed.model_dump(), saved)
        except Exception:
            # This file belongs only to this request. No other version can use it.
            storage.remove(saved["artifact_key"])
            raise

    @app.get("/models/{model_id}/versions", response_model=list[VersionInfo], tags=["Versions"])
    def list_versions(model_id: int, limit: PageLimit = 50, offset: PageOffset = 0):
        return db.list_versions(model_id, limit, offset)

    @app.get("/models/{model_id}/versions/{version}", response_model=VersionInfo, tags=["Versions"])
    def get_version(model_id: int, version: int):
        return db.get_version(model_id, version)

    @app.get("/models/{model_id}/versions/{version}/artifact", tags=["Versions"],
             responses={200: {"content": {"application/octet-stream": {}}, "description": "Verified model bytes"}},
             response_class=Response)
    def download(model_id: int, version: int):
        record = db.get_version(model_id, version)
        return Response(
            content=storage.read_verified(record), media_type="application/octet-stream",
            headers={"X-Artifact-SHA256": record["sha256"],
                     "Content-Disposition": f'attachment; filename="model-{model_id}-v{version}.bin"'},
        )

    @app.put("/models/{model_id}/aliases/{alias}", response_model=AliasInfo, tags=["Aliases"])
    def set_alias(model_id: int, alias: AliasName, body: AliasSet):
        return db.set_alias(model_id, alias, body.version)

    @app.get("/models/{model_id}/aliases/{alias}", response_model=VersionInfo, tags=["Aliases"])
    def resolve_alias(model_id: int, alias: AliasName):
        return db.resolve_alias(model_id, alias)

    return app


app = create_app()
