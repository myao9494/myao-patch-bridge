"""
Myao Patch Bridge のローカル REST API サーバー

仕様:
- アクセス制御（全リクエスト共通のミドルウェア）
  - 接続元IP: ローカルループバック、および待受ホスト設定で許可した場合のVPN/プライベートネットワークのみ
  - Hostヘッダー: このアプリを指す名前だけを受け付ける（DNSリバインディング対策）
  - 状態を変更するAPI（GET/HEAD/OPTIONS以外）: 起動ごとに発行するセッショントークン（X-Rep-Patch-Token）が必要
- 設定やリポジトリを書き換える操作は1つずつ順番に実行する（同時に届いても直列化する）
- 設定管理（GET/PUT /api/settings）
- リポジトリ管理（GET /api/repositories, POST /api/repositories, PUT /api/repositories/{repo_id}, DELETE /api/repositories/{repo_id}, POST /api/repositories/{repo_id}/reset, POST /api/repositories/discover）
- 会社側クリーン環境シミュレーション検証（POST /api/repositories/{repo_id}/verify, POST /api/home/verify-all）
- 自宅パッチ公開（POST /api/home/publish）
- 会社パッチ適用・検証・コミット・一覧・カード削除・適用開始番号初期化（GET /api/company/repositories, DELETE /api/company/repositories/{repo_id}, GET /api/company/downloads, POST /api/company/inspect, POST /api/company/apply-all, POST /api/company/retry, POST /api/company/commit-pending, POST /api/company/repositories/{repo_id}/sequence, POST /api/company/repositories/sequence-all）
- エディタ起動（POST /api/open-vscode, POST /api/open-antigravity）
- 診断機能（GET /api/diagnostics）
- フロントエンドSPA配信
"""
from __future__ import annotations

import functools
import secrets
import socket
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .company import (
    apply_archive,
    commit_pending,
    delete_company_repository,
    init_all_repositories_sequence,
    init_repository_sequence,
    inspect_archive,
    list_company_repositories,
    list_download_packages,
)
from .config import PROJECT_ROOT, SettingsStore, allows_remote_clients
from .diagnostics import run_diagnostics
from .editors import open_in_antigravity, open_in_vscode
from .errors import RepPatchError
from .home import (
    add_repository,
    delete_repository,
    discover_repositories,
    publish,
    reset_repository_patches,
    scan_repositories,
    update_repository,
)
from .security import is_allowed_client_ip, is_allowed_host_header
from .verifier import verify_all_repositories, verify_repository_config

Handler = TypeVar("Handler", bound=Callable[..., Any])


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class RepositoryCreate(BaseModel):
    path: str
    display_name: str = ""
    kind: str = "app"
    branch: str = ""
    baseline_commit: str = ""
    enabled: bool = True
    clean_verify_ignore_patterns: list[str] = []


class RepositoryUpdate(BaseModel):
    values: dict[str, Any]


class RepositoryReset(BaseModel):
    new_baseline_commit: str | None = None


class PublishRequest(BaseModel):
    skip_verification: bool = False


class ZipRequest(BaseModel):
    zip_path: str
    # 旧バージョンの画面が送っていた「修正パッチを重ねる」指定。現在は常に即時コミットするため無視する。
    correction: bool = False


class RetryRequest(ZipRequest):
    repo_id: str


class CommitRequest(BaseModel):
    repo_id: str | None = None


class SequenceInitRequest(BaseModel):
    start_sequence: int


class OpenEditorRequest(BaseModel):
    path: str


def _local_host_names() -> set[str]:
    """このPC自身を指すホスト名（Hostヘッダーとして受け付ける名前）"""
    name = socket.gethostname().strip().lower().rstrip(".")
    if not name:
        return set()
    return {name, f"{name.removesuffix('.local')}.local"}


def create_app(store: SettingsStore | None = None) -> FastAPI:
    settings_store = store or SettingsStore()
    session_token = secrets.token_urlsafe(32)
    local_host_names = _local_host_names()
    # 設定やリポジトリを書き換える操作を直列化する。同時実行すると設定の更新が失われたり、
    # 同じリポジトリへ二重に適用・公開したりするおそれがある。
    operation_lock = threading.Lock()
    app = FastAPI(title="Myao Patch Bridge", version=__version__)

    def exclusive(handler: Handler) -> Handler:
        @functools.wraps(handler)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with operation_lock:
                return handler(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    def forbidden(detail: str) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=403)

    @app.middleware("http")
    async def local_security(request: Request, call_next):  # type: ignore[no-untyped-def]
        try:
            listen_host = settings_store.load_effective().listen_host
        except RepPatchError:
            # 設定ファイルが壊れていても画面は開けるようにし、最も厳しい条件（ローカルのみ）で判定する
            listen_host = "127.0.0.1"
        allow_vpn = allows_remote_clients(listen_host)
        client_host = request.client.host if request.client else ""
        if not is_allowed_client_ip(client_host, allow_vpn=allow_vpn):
            return forbidden("localhostまたは許可されたVPN/プライベートネットワークからのみ利用できます")
        if not is_allowed_host_header(
            request.headers.get("host", ""),
            allow_vpn=allow_vpn,
            configured_host=listen_host,
            known_names=local_host_names,
        ):
            return forbidden(
                "このホスト名ではアクセスできません。IPアドレスで開くか、設定の待受ホストへこのホスト名を指定してください"
            )
        if (
            request.url.path.startswith("/api/")
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and not secrets.compare_digest(
                request.headers.get("x-rep-patch-token", "").encode(), session_token.encode()
            )
        ):
            return forbidden("セッショントークンが不正です")
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

    def settings_payload() -> dict[str, Any]:
        """
        設定画面向けの設定値。保存済みの値に加え、起動引数（--host / --port / --allow-vpn）を
        反映した実際の待受設定を effective_* として返す（effective_* は保存対象ではない）。
        """
        payload = settings_store.load().public_dict()
        effective = settings_store.load_effective()
        payload["effective_listen_host"] = effective.listen_host
        payload["effective_listen_port"] = effective.listen_port
        return payload

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        settings = settings_store.load()
        return {"status": "ok", "version": __version__, "mode": settings.mode}

    @app.get("/api/session")
    def session() -> dict[str, str]:
        return {"token": session_token}

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return settings_payload()

    @app.put("/api/settings")
    @exclusive
    def put_settings(payload: SettingsUpdate) -> dict[str, Any]:
        settings_store.update(payload.values)
        return settings_payload()

    @app.post("/api/repositories/discover")
    @exclusive
    def discover() -> dict[str, Any]:
        settings = settings_store.load()
        return {"repositories": discover_repositories(settings, settings_store)}

    @app.get("/api/repositories")
    def repositories() -> dict[str, Any]:
        return {"repositories": scan_repositories(settings_store.load())}

    @app.post("/api/repositories")
    @exclusive
    def post_repository(payload: RepositoryCreate) -> dict[str, Any]:
        settings = settings_store.load()
        return add_repository(settings, settings_store, payload.model_dump())

    @app.put("/api/repositories/{repo_id}")
    @exclusive
    def put_repository(repo_id: str, payload: RepositoryUpdate) -> dict[str, Any]:
        settings = settings_store.load()
        return update_repository(settings, settings_store, repo_id, payload.values)

    @app.delete("/api/repositories/{repo_id}")
    @exclusive
    def delete_repo(repo_id: str) -> dict[str, Any]:
        settings = settings_store.load()
        return delete_repository(settings, settings_store, repo_id)

    @app.post("/api/repositories/{repo_id}/reset")
    @exclusive
    def reset_repo(
        repo_id: str, payload: RepositoryReset | None = None
    ) -> dict[str, Any]:
        settings = settings_store.load()
        new_baseline = payload.new_baseline_commit if payload else None
        return reset_repository_patches(settings, settings_store, repo_id, new_baseline)

    @app.post("/api/repositories/{repo_id}/verify")
    def verify_repo(repo_id: str) -> dict[str, Any]:
        settings = settings_store.load()
        if repo_id not in settings.repositories:
            raise RepPatchError(f"リポジトリが登録されていません: {repo_id}")
        return verify_repository_config(settings.repositories[repo_id])

    @app.post("/api/home/verify-all")
    def verify_all() -> dict[str, Any]:
        settings = settings_store.load()
        return verify_all_repositories(settings.repositories)

    @app.post("/api/home/publish")
    @exclusive
    def publish_patches(payload: PublishRequest | None = None) -> dict[str, Any]:
        settings = settings_store.load()
        skip = payload.skip_verification if payload else False
        return publish(settings, settings_store, skip_verification=skip)

    @app.get("/api/company/repositories")
    def company_repositories() -> dict[str, Any]:
        return {"repositories": list_company_repositories(settings_store.load())}

    @app.delete("/api/company/repositories/{repo_id}")
    @exclusive
    def delete_company_repo(repo_id: str) -> dict[str, Any]:
        settings = settings_store.load()
        return delete_company_repository(settings, settings_store, repo_id)

    @app.get("/api/company/downloads")
    def downloads() -> dict[str, Any]:
        return {"packages": list_download_packages(settings_store.load())}

    @app.post("/api/company/inspect")
    def inspect(payload: ZipRequest) -> dict[str, Any]:
        return inspect_archive(settings_store.load(), payload.zip_path)

    @app.post("/api/company/apply-all")
    @exclusive
    def apply_all(payload: ZipRequest) -> dict[str, Any]:
        return apply_archive(settings_store.load(), payload.zip_path)

    @app.post("/api/company/retry")
    @exclusive
    def retry(payload: RetryRequest) -> dict[str, Any]:
        return apply_archive(
            settings_store.load(), payload.zip_path, only_repo_id=payload.repo_id
        )

    @app.post("/api/company/commit-pending")
    @exclusive
    def commit(payload: CommitRequest) -> dict[str, Any]:
        return commit_pending(settings_store.load(), payload.repo_id)

    @app.post("/api/company/repositories/{repo_id}/sequence")
    @exclusive
    def set_repo_sequence(repo_id: str, payload: SequenceInitRequest) -> dict[str, Any]:
        return init_repository_sequence(
            settings_store.load(), repo_id, payload.start_sequence
        )

    @app.post("/api/company/repositories/sequence-all")
    @exclusive
    def set_repo_sequence_all(payload: SequenceInitRequest) -> dict[str, Any]:
        return {
            "results": init_all_repositories_sequence(
                settings_store.load(), payload.start_sequence
            )
        }

    @app.post("/api/open-vscode")
    def open_vscode(payload: OpenEditorRequest) -> dict[str, Any]:
        return open_in_vscode(payload.path)

    @app.post("/api/open-antigravity")
    def open_antigravity(payload: OpenEditorRequest) -> dict[str, Any]:
        return open_in_antigravity(payload.path)

    @app.get("/api/diagnostics")
    def diagnostics() -> dict[str, Any]:
        return run_diagnostics(settings_store.load_effective())

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
