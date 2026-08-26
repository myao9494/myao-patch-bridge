"""
リポジトリ管理（追加・削除・スキャン）のテスト仕様

- リポジトリの追加（add_repository, POST /api/repositories）:
    - 指定されたパスがGitリポジトリであるか検証する
    - display_name や branch の自動補完が行われること
    - 重複するrepo_idまたは同一パスの登録時の整合性を担保すること
    - 不正なパスやGitリポジトリ以外の場合はエラーとなること
- リポジトリの削除（delete_repository, DELETE /api/repositories/{repo_id}）:
    - 登録済みのリポジトリを設定から削除し保存すること
    - 存在しないrepo_idを指定した場合はエラーとなること
- APIエンドポイントの認証・セッショントークン検証が適切に行われること
"""
from __future__ import annotations

from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from rep_patch.api import create_app
from rep_patch.config import RepositoryConfig, Settings, SettingsStore
from rep_patch.errors import RepPatchError
from rep_patch.home import add_repository, delete_repository


def test_add_repository_success(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    repo_path = init_repo(tmp_path / "my-custom-app")
    git(repo_path, "branch", "-M", "feature-main")

    settings_path = tmp_path / "settings.local.json"
    store = SettingsStore(settings_path)
    settings = Settings(mode="home", repositories={})
    store.save(settings)

    result = add_repository(
        settings,
        store,
        {
            "path": str(repo_path),
            "display_name": "My Custom App",
            "kind": "app",
        },
    )

    assert result["repo_id"] == "my-custom-app"
    assert result["display_name"] == "My Custom App"
    assert result["branch"] == "feature-main"
    assert result["enabled"] is True

    loaded = store.load()
    assert "my-custom-app" in loaded.repositories
    assert loaded.repositories["my-custom-app"].path == str(repo_path)


def test_add_repository_invalid_path(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.local.json"
    store = SettingsStore(settings_path)
    settings = Settings(mode="home", repositories={})
    store.save(settings)

    with pytest.raises(RepPatchError, match="Gitリポジトリが見つかりません"):
        add_repository(
            settings,
            store,
            {"path": str(tmp_path / "non-existent")},
        )


def test_delete_repository_success(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    repo_path = init_repo(tmp_path / "sample-app")

    settings_path = tmp_path / "settings.local.json"
    store = SettingsStore(settings_path)
    settings = Settings(
        mode="home",
        repositories={
            "sample-app": RepositoryConfig(
                repo_id="sample-app",
                display_name="sample-app",
                path=str(repo_path),
            )
        },
    )
    store.save(settings)

    result = delete_repository(settings, store, "sample-app")
    assert result["deleted"] is True
    assert result["repo_id"] == "sample-app"

    loaded = store.load()
    assert "sample-app" not in loaded.repositories


def test_delete_repository_not_found(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.local.json"
    store = SettingsStore(settings_path)
    settings = Settings(mode="home", repositories={})
    store.save(settings)

    with pytest.raises(RepPatchError, match="リポジトリが登録されていません"):
        delete_repository(settings, store, "unknown-app")


def test_api_repository_add_and_delete(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    repo_path = init_repo(tmp_path / "api-app")
    git(repo_path, "branch", "-M", "main")

    settings_path = tmp_path / "settings.local.json"
    store = SettingsStore(settings_path)
    store.save(Settings(mode="home", repositories={}))

    client = TestClient(create_app(store))
    token = client.get("/api/session").json()["token"]
    headers = {"X-Rep-Patch-Token": token}

    # 追加 API
    response = client.post(
        "/api/repositories",
        json={"path": str(repo_path), "display_name": "API App"},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["repo_id"] == "api-app"
    assert data["display_name"] == "API App"

    # リポジトリ一覧で確認
    list_res = client.get("/api/repositories")
    assert list_res.status_code == 200
    repos = list_res.json()["repositories"]
    assert any(r["repo_id"] == "api-app" for r in repos)

    # 削除 API
    del_res = client.delete("/api/repositories/api-app", headers=headers)
    assert del_res.status_code == 200
    assert del_res.json()["deleted"] is True

    # 削除後の確認
    list_res_after = client.get("/api/repositories")
    repos_after = list_res_after.json()["repositories"]
    assert not any(r["repo_id"] == "api-app" for r in repos_after)
