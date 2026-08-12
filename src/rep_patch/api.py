from __future__ import annotations

import secrets
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .company import (
    apply_archive,
    commit_pending,
    inspect_archive,
    list_download_packages,
)
from .config import PROJECT_ROOT, SettingsStore
from .diagnostics import run_diagnostics
from .errors import RepPatchError
from .home import discover_repositories, publish, scan_repositories, update_repository


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class RepositoryUpdate(BaseModel):
    values: dict[str, Any]


class ZipRequest(BaseModel):
    zip_path: str
    correction: bool = False


class RetryRequest(ZipRequest):
    repo_id: str


class CommitRequest(BaseModel):
    repo_id: str | None = None


def create_app(store: SettingsStore | None = None) -> FastAPI:
    settings_store = store or SettingsStore()
    session_token = secrets.token_urlsafe(32)
    app = FastAPI(title="Myao Patch Bridge", version=__version__)

    @app.middleware("http")
    async def local_security(request: Request, call_next):  # type: ignore[no-untyped-def]
        client_host = request.client.host if request.client else ""
        if client_host not in {"127.0.0.1", "::1", "testclient"}:
            return JSONResponse({"detail": "localhostからのみ利用できます"}, status_code=403)
        if (
            request.url.path.startswith("/api/")
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("x-rep-patch-token") != session_token
        ):
            return JSONResponse({"detail": "セッショントークンが不正です"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'"
            )
        return response

    @app.exception_handler(RepPatchError)
    async def rep_patch_error(_: Request, exc: RepPatchError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        settings = settings_store.load()
        return {"status": "ok", "version": __version__, "mode": settings.mode}

    @app.get("/api/session")
    def session() -> dict[str, str]:
        return {"token": session_token}

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return settings_store.load().public_dict()

    @app.put("/api/settings")
    def put_settings(payload: SettingsUpdate) -> dict[str, Any]:
        return settings_store.update(payload.values).public_dict()

    @app.post("/api/repositories/discover")
    def discover() -> dict[str, Any]:
        settings = settings_store.load()
        return {"repositories": discover_repositories(settings, settings_store)}

    @app.get("/api/repositories")
    def repositories() -> dict[str, Any]:
        return {"repositories": scan_repositories(settings_store.load())}

    @app.put("/api/repositories/{repo_id}")
    def put_repository(repo_id: str, payload: RepositoryUpdate) -> dict[str, Any]:
        settings = settings_store.load()
        return update_repository(settings, settings_store, repo_id, payload.values)

    @app.post("/api/home/publish")
    def publish_patches() -> dict[str, Any]:
        settings = settings_store.load()
        return publish(settings, settings_store)

    @app.get("/api/company/downloads")
    def downloads() -> dict[str, Any]:
        return {"packages": list_download_packages(settings_store.load())}

    @app.post("/api/company/inspect")
    def inspect(payload: ZipRequest) -> dict[str, Any]:
        return inspect_archive(settings_store.load(), payload.zip_path)

    @app.post("/api/company/apply-all")
    def apply_all(payload: ZipRequest) -> dict[str, Any]:
        return apply_archive(
            settings_store.load(), payload.zip_path, correction=payload.correction
        )

    @app.post("/api/company/retry")
    def retry(payload: RetryRequest) -> dict[str, Any]:
        return apply_archive(
            settings_store.load(),
            payload.zip_path,
            only_repo_id=payload.repo_id,
            correction=payload.correction,
        )

    @app.post("/api/company/commit-pending")
    def commit(payload: CommitRequest) -> dict[str, Any]:
        return commit_pending(settings_store.load(), payload.repo_id)

    @app.get("/api/diagnostics")
    def diagnostics() -> dict[str, Any]:
        return run_diagnostics(settings_store.load())

    frontend = PROJECT_ROOT / "frontend" / "dist"
    assets = frontend / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):  # type: ignore[no-untyped-def]
        if frontend.is_dir():
            requested = frontend / path
            if path and requested.is_file() and frontend.resolve() in requested.resolve().parents:
                return FileResponse(requested)
            return FileResponse(frontend / "index.html")
        return HTMLResponse(
            "<h1>Myao Patch Bridge</h1><p>frontend/dist がありません。Reactをビルドしてください。</p>",
            status_code=503,
        )

    return app
