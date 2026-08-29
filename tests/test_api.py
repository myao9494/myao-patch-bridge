"""
APIエンドポイントおよびPWA配信セキュリティのテスト

仕様:
- test_api_requires_session_token_for_mutation: 状態変更APIに対するセッショントークン検証
- test_pwa_is_served_with_local_only_security_headers: PWA静的ファイル配信とローカル専用セキュリティヘッダーの検証
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

