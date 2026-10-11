"""
テスト用ヘルパー

仕様:
- git / init_repo / commit_all: テスト用リポジトリの操作
- make_patch_repo: pushできるリモート付きのパッチ専用リポジトリを用意する
- make_client: 接続元IPを指定した API テストクライアントを作る（既定は 127.0.0.1）
- auth_headers: 状態変更APIに必要なセッショントークン付きヘッダーを取得する
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from rep_patch.api import create_app
from rep_patch.config import SettingsStore


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode(errors="replace"))
    return completed.stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    return path


def commit_all(repo: Path, message: str) -> str:
    """作業ツリーの全変更をコミットし、そのコミットIDを返す"""
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").decode().strip()


def make_patch_repo(root: Path) -> Path:
    """pushできるリモート（bareリポジトリ）付きのパッチ専用リポジトリを作る"""
    remote = root / "patch-remote.git"
    remote.mkdir(parents=True)
    git(remote, "init", "--bare")
    patch_repo = init_repo(root / "myao_app_patch")
    (patch_repo / "README.md").write_text("patches\n", encoding="utf-8")
    commit_all(patch_repo, "initial")
    git(patch_repo, "remote", "add", "origin", str(remote))
    git(patch_repo, "push", "-u", "origin", "main")
    return patch_repo


def make_client(store: SettingsStore, client_host: str = "127.0.0.1", **kwargs) -> TestClient:
    """指定した接続元IPからのアクセスとして振る舞うテストクライアントを作る"""
    return TestClient(create_app(store), client=(client_host, 50000), **kwargs)


def auth_headers(client: TestClient) -> dict[str, str]:
    return {"X-Rep-Patch-Token": client.get("/api/session").json()["token"]}
