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
- リポジトリ設定の更新（update_repository, PUT /api/repositories/{repo_id}）:
    - 有効/無効、ブランチ、初期導入地点（完全なコミットIDへ解決）、検証の除外パターンを保存すること
    - 不正な型の値や存在しないコミットは拒否し、保存しないこと
- 自動検出（discover_repositories, POST /api/repositories/discover）:
    - アプリルート直下のGitリポジトリとObsidian設定を登録し、Patch App自身とGit以外のフォルダは除外すること
    - 再検出しても既存の設定（公開済みコミット・連番など）を保持すること
- 一覧（scan_repositories）: 未公開コミット数と状態を返し、壊れたリポジトリはカード上のエラーとして返すこと
"""
from __future__ import annotations

from pathlib import Path
import pytest

from helpers import auth_headers, commit_all, make_client
from rep_patch.config import RepositoryConfig, Settings, SettingsStore
from rep_patch.errors import RepPatchError
from rep_patch.home import (
    add_repository,
    delete_repository,
    discover_repositories,
    scan_repositories,
    update_repository,
)


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

    client = make_client(store)
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


def test_add_repository_rejects_duplicate_id_and_keeps_existing(tmp_path: Path, git_helpers) -> None:
    """同じIDで追加し直しても、既存の公開済みコミット・連番の記録を上書きしない"""
    git, init_repo = git_helpers
    first = init_repo(tmp_path / "one" / "my-app")
    second = init_repo(tmp_path / "two" / "my-app")
    store = SettingsStore(tmp_path / "settings.local.json")
    settings = Settings(mode="home")
    store.save(settings)
    add_repository(settings, store, {"path": str(first)})
    settings.repositories["my-app"].published_commit = "abc123"
    settings.repositories["my-app"].last_sequence = 7
    store.save(settings)

    with pytest.raises(RepPatchError, match="既に登録されています"):
        add_repository(store.load(), store, {"path": str(second)})
    kept = store.load().repositories["my-app"]
    assert (kept.path, kept.published_commit, kept.last_sequence) == (str(first.resolve()), "abc123", 7)

    # 表示名を変えれば別のIDで登録できる
    added = add_repository(store.load(), store, {"path": str(second), "display_name": "my-app-2"})
    assert added["repo_id"] == "my-app-2"
    assert sorted(store.load().repositories) == ["my-app", "my-app-2"]


def test_add_repository_rejects_non_git_directory(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    store = SettingsStore(tmp_path / "settings.local.json")
    with pytest.raises(RepPatchError, match="Gitリポジトリではありません"):
        add_repository(Settings(mode="home"), store, {"path": str(plain)})
    with pytest.raises(RepPatchError, match="パスを指定してください"):
        add_repository(Settings(mode="home"), store, {"path": "  "})


def test_update_repository_saves_all_card_fields(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    repo = init_repo(tmp_path / "my-app")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    head = commit_all(repo, "init")
    git(repo, "tag", "v1")
    store = SettingsStore(tmp_path / "settings.local.json")
    store.save(Settings(mode="home"))
    add_repository(store.load(), store, {"path": str(repo)})
    client = make_client(store)
    headers = auth_headers(client)

    response = client.put(
        "/api/repositories/my-app",
        json={
            "values": {
                "enabled": False,
                "branch": "main",
                "baseline_commit": "v1",
                "clean_verify_ignore_patterns": ["backend/data/*", "  custom.txt  ", ""],
            }
        },
        headers=headers,
    )

    assert response.status_code == 200
    saved = store.load().repositories["my-app"]
    assert saved.enabled is False
    assert saved.baseline_commit == head  # タグ名が完全なコミットIDへ解決される
    assert saved.clean_verify_ignore_patterns == ["backend/data/*", "custom.txt"]

    # 不正な値は拒否され、保存内容は変わらない
    for values in (
        {"enabled": "yes"},
        {"clean_verify_ignore_patterns": "backend/data/*"},
        {"baseline_commit": "no-such-revision"},
        {"kind": "service"},
        {"branch": 5},
    ):
        assert client.put("/api/repositories/my-app", json={"values": values}, headers=headers).status_code == 400
    assert store.load().repositories["my-app"] == saved
    assert client.put("/api/repositories/unknown", json={"values": {}}, headers=headers).status_code == 400

    with pytest.raises(RepPatchError, match="登録されていません"):
        update_repository(store.load(), store, "unknown", {"enabled": True})


def test_discover_registers_repositories_and_keeps_existing_settings(
    tmp_path: Path, git_helpers, monkeypatch: pytest.MonkeyPatch
) -> None:
    git, init_repo = git_helpers
    apps_root = tmp_path / "apps"
    # Patch App自身がアプリルート直下にあっても、公開対象として検出しない
    patch_bridge = init_repo(apps_root / "myao-patch-bridge")
    (patch_bridge / "a.txt").write_text("a\n", encoding="utf-8")
    commit_all(patch_bridge, "init")
    monkeypatch.setattr("rep_patch.home.PROJECT_ROOT", patch_bridge)
    app_one = init_repo(apps_root / "App One")
    (app_one / "a.txt").write_text("a\n", encoding="utf-8")
    commit_all(app_one, "init")
    app_two = init_repo(apps_root / "app-two")
    (app_two / "a.txt").write_text("a\n", encoding="utf-8")
    commit_all(app_two, "init")
    git(app_two, "branch", "-M", "develop")
    (apps_root / "not-a-repo").mkdir()
    (apps_root / "notes.txt").write_text("file\n", encoding="utf-8")
    obsidian = init_repo(tmp_path / "obsidian_setting")
    (obsidian / "app.json").write_text("{}\n", encoding="utf-8")
    commit_all(obsidian, "init")

    store = SettingsStore(tmp_path / "settings.local.json")
    store.save(Settings(mode="home", apps_root=str(apps_root), obsidian_repo=str(obsidian)))
    client = make_client(store)
    headers = auth_headers(client)

    response = client.post("/api/repositories/discover", headers=headers)

    assert response.status_code == 200
    found = {item["repo_id"]: item for item in response.json()["repositories"]}
    assert sorted(found) == ["app-one", "app-two", "obsidian-settings"]
    assert found["app-one"]["display_name"] == "App One"
    assert found["app-two"]["branch"] == "develop"
    assert found["obsidian-settings"]["kind"] == "obsidian"
    assert all(item["error"] == "" for item in found.values())

    # 再検出しても、設定済みの値は保持される
    settings = store.load()
    settings.repositories["app-one"].baseline_commit = found["app-one"]["head"]
    settings.repositories["app-one"].last_sequence = 3
    settings.repositories["app-one"].enabled = False
    store.save(settings)
    discover_repositories(store.load(), store)
    kept = store.load().repositories["app-one"]
    assert (kept.baseline_commit, kept.last_sequence, kept.enabled) == (found["app-one"]["head"], 3, False)

    store.update({"apps_root": str(tmp_path / "missing")})
    assert client.post("/api/repositories/discover", headers=headers).status_code == 400


def test_scan_reports_unpublished_commits_and_errors(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    repo = init_repo(tmp_path / "my-app")
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    base = commit_all(repo, "v1")
    for version in ("v2", "v3"):
        (repo / "a.txt").write_text(version, encoding="utf-8")
        commit_all(repo, version)
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    settings = Settings(
        mode="home",
        repositories={
            "my-app": RepositoryConfig("my-app", "my-app", str(repo), baseline_commit=base),
            "gone": RepositoryConfig("gone", "Gone", str(tmp_path / "missing")),
            "bad-branch": RepositoryConfig("bad-branch", "Bad Branch", str(repo), branch="no-such-branch"),
        },
    )
    scanned = {item["repo_id"]: item for item in scan_repositories(settings)}

    assert [item["repo_id"] for item in scan_repositories(settings)] == ["bad-branch", "gone", "my-app"]
    assert scanned["my-app"]["unpublished_commits"] == 2
    assert scanned["my-app"]["clean"] is False and scanned["my-app"]["changes"] == 1
    assert scanned["my-app"]["error"] == ""
    assert "見つかりません" in scanned["gone"]["error"]
    assert scanned["bad-branch"]["error"] != ""
