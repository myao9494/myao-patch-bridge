"""
パッチパッケージの署名生成・検証、パス検証、および接続元IP検証を行うセキュリティモジュール

仕様:
- sha256_bytes / sha256_file: ハッシュ計算
- sign_document / verify_document: HMAC-SHA256 によるマニフェスト・インデックス署名検証
- safe_repo_path: パストラバーサル防止および安全な相対パス解決
- is_allowed_client_ip: 接続元IP（ローカルループバックおよびVPN/プライベートネットワーク）の検証
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import PackageValidationError, RepPatchError

SIGNATURE_SALT = b"myao-rep-patch-manifest-v1"
PBKDF2_ROUNDS = 200_000
_TAILSCALE_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def is_allowed_client_ip(client_host: str, allow_vpn: bool = False) -> bool:
    """
    接続元クライアントIPが許可されているかを検証する。

    仕様:
    - localhost, 127.0.0.1, ::1, testclient は常に許可
    - allow_vpn が True の場合:
      - プライベートネットワーク（RFC 1918 / IPv6 ULA）
      - Tailscale CGNAT（100.64.0.0/10）
      - リンクローカル（fe80::/10, 169.254.0.0/16）
      からのアクセスを許可
    - パブリックIPや不正なホスト文字列は拒否
    """
    if not client_host:
        return False
    if client_host in {"localhost", "testclient"}:
        return True
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False

    if ip.is_loopback:
        return True

    if allow_vpn:
        if ip.is_private or ip.is_link_local:
            return True
        if ip.version == 4 and ip in _TAILSCALE_CGNAT_NETWORK:
            return True

    return False



def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: dict[str, Any]) -> bytes:
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    return json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def derive_hmac_key(password: str) -> bytes:
    if not password:
        raise RepPatchError("パッチ検証用パスワードが設定されていません")
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), SIGNATURE_SALT, PBKDF2_ROUNDS)


def sign_document(value: dict[str, Any], password: str) -> dict[str, Any]:
    signed = dict(value)
    signed["signature"] = hmac.new(
        derive_hmac_key(password), canonical_json(value), hashlib.sha256
    ).hexdigest()
    return signed


def verify_document(value: dict[str, Any], password: str) -> None:
    signature = value.get("signature")
    if not isinstance(signature, str):
        raise PackageValidationError("署名がありません")
    expected = hmac.new(
        derive_hmac_key(password), canonical_json(value), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise PackageValidationError("パスワードが異なるか、パッケージが改変されています")


def safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        raise PackageValidationError(f"安全でないパスです: {value}")
    if ":" in path.parts[0]:
        raise PackageValidationError(f"安全でないパスです: {value}")
    return path


def safe_repo_path(root: Path, relative: str) -> Path:
    safe = safe_relative_path(relative)
    root_resolved = root.resolve()
    target = root.joinpath(*safe.parts)
    parent = target.parent
    if parent.exists() and any(part.is_symlink() for part in [parent, *parent.parents] if part != root_resolved.parent):
        # A stricter final containment check follows; this message makes symlink failures explicit.
        resolved_parent = parent.resolve()
        if root_resolved not in [resolved_parent, *resolved_parent.parents]:
            raise PackageValidationError(f"リポジトリ外を指すシンボリックリンクです: {relative}")
    resolved = target.resolve(strict=False)
    if root_resolved not in [resolved, *resolved.parents]:
        raise PackageValidationError(f"リポジトリ外のパスです: {relative}")
    return target
