"""
署名検証、パス安全性判定、接続元IP検証のテスト

仕様:
- test_hmac_signature_detects_changes: HMAC署名による改ざん検知の検証
- test_unsafe_relative_paths_are_rejected: パストラバーサル防止の検証
- test_normal_relative_path_is_allowed: 正常な相対パスの許可検証
- test_is_allowed_client_ip_loopback: ループバックアドレスの接続許可検証
- test_is_allowed_client_ip_vpn_and_private: VPNおよびプライベートネットワークIPの接続許可・拒否検証
- test_is_allowed_client_ip_public_rejected: パブリックIPおよび不正文字列の接続拒否検証
- test_read_json_and_settings_with_utf8_bom: Windows環境等のBOM付きUTF-8 JSONが安全に読み込めることの検証
- test_git_metadata_paths_are_rejected: Gitの管理領域（.git 配下）を指すパスの拒否検証
- test_safe_repo_path_rejects_symlink_escape: シンボリックリンク経由でリポジトリ外へ出るパスの拒否検証
- test_host_header_*: Hostヘッダー検証（DNSリバインディング対策）の許可・拒否検証
- test_hmac_key_derivation_is_cached: 同じパスワードの鍵導出を繰り返さないことの検証
"""
from __future__ import annotations

import copy

import pytest

from rep_patch.errors import PackageValidationError
from rep_patch import security
from rep_patch.security import (
    is_allowed_client_ip,
    is_allowed_host_header,
    safe_relative_path,
    safe_repo_path,
    sign_document,
    verify_document,
)


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


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost", "::ffff:127.0.0.1"])
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
        "testclient",
        "",
        "999.999.999.999",
    ],
)
def test_is_allowed_client_ip_public_rejected(host: str) -> None:
    assert is_allowed_client_ip(host, allow_vpn=False) is False
    assert is_allowed_client_ip(host, allow_vpn=True) is False


def test_read_json_and_settings_with_utf8_bom(tmp_path) -> None:
    """
    Windowsのメモ帳等で保存されたBOM付きUTF-8のJSONファイル（設定ファイルやマニフェスト）が
    エラーにならず安全にデコード・読み込めることの検証
    """
    from rep_patch.config import SettingsStore
    from rep_patch.packages import read_json

    # 1. read_json with BOM
    json_path = tmp_path / "bom_sample.json"
    bom_content = b"\xef\xbb\xbf{\"hello\": \"world\", \"number\": 123}\n"
    json_path.write_bytes(bom_content)
    data = read_json(json_path)
    assert data == {"hello": "world", "number": 123}

    # 2. SettingsStore.load with BOM
    settings_path = tmp_path / "settings.local.json"
    settings_bom = b"\xef\xbb\xbf{\"mode\": \"company\", \"patch_password\": \"abc\"}\n"
    settings_path.write_bytes(settings_bom)
    store = SettingsStore(settings_path)
    settings = store.load()
    assert settings.mode == "company"
    assert settings.patch_password == "abc"


@pytest.mark.parametrize(
    "value",
    [".git/config", ".git/hooks/pre-commit", "sub/.git/config", ".GIT/config", ".git./config", ".git /config", "GIT~1/config"],
)
def test_git_metadata_paths_are_rejected(value: str) -> None:
    with pytest.raises(PackageValidationError):
        safe_relative_path(value)


@pytest.mark.parametrize("value", [".gitignore", ".github/workflows/ci.yml", ".gitattributes", "docs/git.md", "a.git/file"])
def test_git_related_but_ordinary_paths_are_allowed(value: str) -> None:
    assert str(safe_relative_path(value)) == value


def test_safe_repo_path_rejects_symlink_escape(tmp_path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (repo / "link").symlink_to(outside, target_is_directory=True)

    assert safe_repo_path(repo, "dir/file.txt") == repo / "dir" / "file.txt"
    with pytest.raises(PackageValidationError):
        safe_repo_path(repo, "link/file.txt")


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:17345", "localhost:17345", "localhost", "[::1]:17345", "192.168.1.20:17345", "my-pc:17345", "app.localhost"],
)
def test_host_header_for_local_names_and_ip_literals_is_allowed(host: str) -> None:
    assert is_allowed_host_header(host) is True


@pytest.mark.parametrize("host", ["", "attacker.example.com:17345", "localhost.attacker.example.com", "my-pc.tail1234.ts.net"])
def test_host_header_for_foreign_domains_is_rejected_when_local_only(host: str) -> None:
    assert is_allowed_host_header(host, allow_vpn=False) is False


def test_host_header_accepts_configured_and_tailscale_names() -> None:
    # 待受ホストとして設定した名前、このPC自身の名前は受け付ける
    assert is_allowed_host_header("pc123.corp.example.com:17345", configured_host="PC123.corp.example.com") is True
    assert is_allowed_host_header("mymac.local:17345", known_names={"mymac.local"}) is True
    # VPN許可時は Tailscale MagicDNS 名を受け付けるが、無関係な公開ドメインは受け付けない
    assert is_allowed_host_header("my-pc.tail1234.ts.net:17345", allow_vpn=True) is True
    assert is_allowed_host_header("attacker.example.com", allow_vpn=True) is False
    assert is_allowed_host_header("ts.net.attacker.example.com", allow_vpn=True) is False


def test_hmac_key_derivation_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = security.hashlib.pbkdf2_hmac

    def counting(*args, **kwargs):
        calls.append("derive")
        return original(*args, **kwargs)

    monkeypatch.setattr(security.hashlib, "pbkdf2_hmac", counting)
    security._derive_hmac_key_cached.cache_clear()
    signed = [sign_document({"n": n}, "cache-test-password") for n in range(5)]
    for document in signed:
        verify_document(document, "cache-test-password")
    assert calls == ["derive"]
    with pytest.raises(PackageValidationError):
        verify_document(signed[0], "different-password")
