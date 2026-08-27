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
