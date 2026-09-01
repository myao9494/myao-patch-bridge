"""
APIエンドポイントおよびPWA配信セキュリティのテスト

仕様:
- test_api_requires_session_token_for_mutation: 状態変更APIに対するセッショントークン検証
- test_pwa_is_served_with_local_only_security_headers: PWA静的ファイル配信とローカル専用セキュリティヘッダーの検証
- test_company_api_repositories_and_commit: 会社側リポジトリ一覧・個別コミットAPIの検証
- test_company_api_init_sequence: 会社側リポジトリ適用開始番号指定APIおよびstate.json生成の検証
- test_api_open_vscode: VS Code起動APIの検証（正常系・異常系・VS Code未検出）
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



