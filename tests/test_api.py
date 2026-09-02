"""
APIエンドポイントおよびPWA配信セキュリティのテスト

仕様:
- test_api_requires_session_token_for_mutation: 状態変更APIに対するセッショントークン検証
- test_pwa_is_served_with_local_only_security_headers: PWA静的ファイル配信とローカル専用セキュリティヘッダーの検証
- test_company_api_repositories_and_commit: 会社側リポジトリ一覧・個別コミットAPIの検証
- test_company_api_init_sequence: 会社側リポジトリ適用開始番号指定APIおよびstate.json生成の検証
- test_api_open_vscode: VS Code起動APIの検証（正常系・異常系・VS Code未検出）
- test_company_api_delete_repository: 会社側リポジトリカードの削除（除外）APIおよび一覧からの非表示、実ディレクトリ保持の検証
"""
from __future__ import annotations

from fastapi.testclient import TestClient


from rep_patch.api import create_app
from rep_patch.config import SettingsStore


def test_api_requires_session_token_for_mutation(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.local.json")
    client = TestClient(create_app(store))
    assert client.get("/api/health").status_code == 200
    assert client.put("/api/settings", json={"values": {"mode": "home"}}).status_code == 403
    token = client.get("/api/session").json()["token"]
    response = client.put(
        "/api/settings",
        json={"values": {"mode": "home"}},
        headers={"X-Rep-Patch-Token": token},
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "home"


def test_pwa_is_served_with_local_only_security_headers(tmp_path) -> None:
    client = TestClient(create_app(SettingsStore(tmp_path / "settings.local.json")))
    response = client.get("/")
    assert response.status_code == 200
    assert "Myao Patch Bridge" in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert '"display": "standalone"' in manifest.text


def test_company_api_repositories_and_commit(tmp_path, git_helpers) -> None:
    git, init_repo = git_helpers
    company_root = tmp_path / "company-apps"
    app_repo = init_repo(company_root / "my-app")
    (app_repo / "main.py").write_text("print('hello')\n", encoding="utf-8")
    git(app_repo, "add", "-A")
    git(app_repo, "commit", "-m", "init")

    store = SettingsStore(tmp_path / "settings.local.json")
    store.update({"mode": "company", "company_apps_root": str(company_root)})
    client = TestClient(create_app(store))

    # 一覧取得
    res = client.get("/api/company/repositories")
    assert res.status_code == 200
    repos = res.json()["repositories"]
    assert len(repos) == 1
    assert repos[0]["repo_id"] == "my-app"
    assert repos[0]["clean"] is True

    # 変更を作成
    (app_repo / "main.py").write_text("print('hello updated')\n", encoding="utf-8")

    # 個別コミット実行
    token = client.get("/api/session").json()["token"]
    commit_res = client.post(
        "/api/company/commit-pending",
        json={"repo_id": "my-app"},
        headers={"X-Rep-Patch-Token": token},
    )
    assert commit_res.status_code == 200
    results = commit_res.json()["results"]
    assert len(results) == 1
    assert results[0]["status"] == "committed"


def test_company_api_init_sequence(tmp_path, git_helpers) -> None:
    """会社側リポジトリの適用開始番号指定APIおよびstate.json生成のテスト"""
    git, init_repo = git_helpers
    company_root = tmp_path / "company-apps"
    app_repo = init_repo(company_root / "my-app")
    (app_repo / "main.py").write_text("print('hello')\n", encoding="utf-8")
    git(app_repo, "add", "-A")
    git(app_repo, "commit", "-m", "init")

    store = SettingsStore(tmp_path / "settings.local.json")
    store.update({"mode": "company", "company_apps_root": str(company_root)})
    client = TestClient(create_app(store))

    token = client.get("/api/session").json()["token"]

    # 1. 個別リポジトリの開始番号設定（start_sequence=2）
    res = client.post(
        "/api/company/repositories/my-app/sequence",
        json={"start_sequence": 2},
        headers={"X-Rep-Patch-Token": token},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["repo_id"] == "my-app"
    assert data["start_sequence"] == 2
    assert data["confirmed_sequence"] == 1

    # state.json が実際に生成されていることを確認
    state_file = app_repo / ".git" / "rep-patch" / "state.json"
    assert state_file.is_file()

    # 2. 一覧APIに反映されているか確認
    repos_res = client.get("/api/company/repositories")
    assert repos_res.status_code == 200
    target_repo = next(r for r in repos_res.json()["repositories"] if r["repo_id"] == "my-app")
    assert target_repo["confirmed_sequence"] == 1

    # 3. 全リポジトリ一括の開始番号設定（start_sequence=5）
    res_all = client.post(
        "/api/company/repositories/sequence-all",
        json={"start_sequence": 5},
        headers={"X-Rep-Patch-Token": token},
    )
    assert res_all.status_code == 200
    results_all = res_all.json()["results"]
    assert len(results_all) == 1
    assert results_all[0]["confirmed_sequence"] == 4


def test_api_open_vscode(tmp_path, monkeypatch) -> None:
    """VS Code起動APIのテスト（正常系・存在しないパス・VS Code未検出）"""
    store = SettingsStore(tmp_path / "settings.local.json")
    client = TestClient(create_app(store))
    token = client.get("/api/session").json()["token"]

    target_dir = tmp_path / "sample-app"
    target_dir.mkdir()

    # 1. 正常系: code コマンドが見つかり、Popen が呼び出される
    calls = []

    def fake_which(cmd):
        return "/usr/local/bin/code" if cmd == "code" else None

    def fake_popen(cmd):
        calls.append(cmd)

    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    res = client.post(
        "/api/open-vscode",
        json={"path": str(target_dir)},
        headers={"X-Rep-Patch-Token": token},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert len(calls) == 1
    assert calls[0] == ["/usr/local/bin/code", str(target_dir)]

    # 2. 異常系: 存在しないパス
    res_missing = client.post(
        "/api/open-vscode",
        json={"path": str(tmp_path / "not-exist")},
        headers={"X-Rep-Patch-Token": token},
    )
    assert res_missing.status_code == 400
    assert "見つかりません" in res_missing.json()["detail"] or "存在しません" in res_missing.json()["detail"]

    # 3. 異常系: VS Code がインストールされていない（which が None を返す）
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    res_no_code = client.post(
        "/api/open-vscode",
        json={"path": str(target_dir)},
        headers={"X-Rep-Patch-Token": token},
    )
    assert res_no_code.status_code == 400
    assert "VS Code" in res_no_code.json()["detail"]


def test_company_api_delete_repository(tmp_path, git_helpers) -> None:
    """会社側リポジトリカードの削除（除外）APIのテスト"""
    git, init_repo = git_helpers
    company_root = tmp_path / "company-apps"
    repo1 = init_repo(company_root / "app-one")
    (repo1 / "main.py").write_text("print('one')\n", encoding="utf-8")
    git(repo1, "add", "-A")
    git(repo1, "commit", "-m", "init one")

    repo2 = init_repo(company_root / "app-two")
    (repo2 / "main.py").write_text("print('two')\n", encoding="utf-8")
    git(repo2, "add", "-A")
    git(repo2, "commit", "-m", "init two")

    store = SettingsStore(tmp_path / "settings.local.json")
    store.update({"mode": "company", "company_apps_root": str(company_root)})
    client = TestClient(create_app(store))

    # 初期状態で2つのリポジトリが検出される
    res = client.get("/api/company/repositories")
    assert res.status_code == 200
    repos = res.json()["repositories"]
    assert len(repos) == 2
    repo_ids = [r["repo_id"] for r in repos]
    assert "app-one" in repo_ids
    assert "app-two" in repo_ids

    # トークンなしでの削除は403
    res_no_token = client.delete("/api/company/repositories/app-one")
    assert res_no_token.status_code == 403

    # トークンありで app-one のカードを削除
    token = client.get("/api/session").json()["token"]
    del_res = client.delete(
        "/api/company/repositories/app-one",
        headers={"X-Rep-Patch-Token": token},
    )
    assert del_res.status_code == 200
    assert del_res.json() == {"deleted": True, "repo_id": "app-one"}

    # 削除後、一覧から app-one が除外され app-two のみになる
    res_after = client.get("/api/company/repositories")
    assert res_after.status_code == 200
    repos_after = res_after.json()["repositories"]
    assert len(repos_after) == 1
    assert repos_after[0]["repo_id"] == "app-two"

    # 実ディレクトリおよびファイル・Git履歴は保持されている
    assert (repo1 / "main.py").exists()
    assert (repo1 / ".git").is_dir()
    assert (repo1 / "main.py").read_text(encoding="utf-8") == "print('one')\n"

    # 設定ファイルに除外IDが保存されている
    loaded = store.load()
    assert "app-one" in loaded.company_excluded_repo_ids




