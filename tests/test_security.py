"""
署名検証、パス安全性判定、接続元IP検証のテスト

仕様:
- test_hmac_signature_detects_changes: HMAC署名による改ざん検知の検証
- test_unsafe_relative_paths_are_rejected: パストラバーサル防止の検証
- test_normal_relative_path_is_allowed: 正常な相対パスの許可検証
- test_is_allowed_client_ip_loopback: ループバックアドレスの接続許可検証
- test_is_allowed_client_ip_vpn_and_private: VPNおよびプライベートネットワークIPの接続許可・拒否検証
- test_is_allowed_client_ip_public_rejected: パブリックIPおよび不正文字列の接続拒否検証
"""
from __future__ import annotations

import copy


import pytest

from rep_patch.errors import PackageValidationError
from rep_patch.security import is_allowed_client_ip, safe_relative_path, sign_document, verify_document


def test_hmac_signature_detects_changes() -> None:
    value = sign_document({"schema_version": 1, "packages": []}, "test-password")
    verify_document(value, "test-password")
    modified = copy.deepcopy(value)
    modified["packages"].append({"repo_id": "tampered"})
    with pytest.raises(PackageValidationError):
        verify_document(modified, "test-password")


@pytest.mark.parametrize("value", ["../secret", "/absolute", "C:/Windows/file", "safe/../../bad"])
def test_unsafe_relative_paths_are_rejected(value: str) -> None:
    with pytest.raises(PackageValidationError):
        safe_relative_path(value)


def test_normal_relative_path_is_allowed() -> None:
    assert str(safe_relative_path("packages/app/000001/manifest.json")) == "packages/app/000001/manifest.json"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost", "testclient"])
def test_is_allowed_client_ip_loopback(host: str) -> None:
    assert is_allowed_client_ip(host, allow_vpn=False) is True
    assert is_allowed_client_ip(host, allow_vpn=True) is True


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.100",
        "100.64.0.1",  # Tailscale / CGNAT
        "100.100.100.100",  # Tailscale
        "fd7a:115c:a1e0::1",  # Tailscale IPv6 / ULA
        "fe80::1",  # リンクローカル
    ],
)
def test_is_allowed_client_ip_vpn_and_private(host: str) -> None:
    # allow_vpn=False の場合は拒否
    assert is_allowed_client_ip(host, allow_vpn=False) is False
    # allow_vpn=True の場合は許可
    assert is_allowed_client_ip(host, allow_vpn=True) is True


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "1.1.1.1",
        "93.184.216.34",
        "invalid-host",
        "",
        "999.999.999.999",
    ],
)
def test_is_allowed_client_ip_public_rejected(host: str) -> None:
    assert is_allowed_client_ip(host, allow_vpn=False) is False
    assert is_allowed_client_ip(host, allow_vpn=True) is False

