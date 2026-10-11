"""
パッチパッケージの署名生成・検証、パス検証、および接続元検証を行うセキュリティモジュール

仕様:
- sha256_bytes: ハッシュ計算
- sign_document / verify_document: HMAC-SHA256 によるマニフェスト・インデックス署名検証
  - 鍵はパスワードからPBKDF2で導出し、同じパスワードの導出結果はプロセス内でキャッシュする
- safe_relative_path / safe_repo_path: パストラバーサル防止および安全な相対パス解決
  - Gitの管理領域（.git 配下）を指すパスは拒否する
- is_allowed_client_ip: 接続元IP（ローカルループバックおよびVPN/プライベートネットワーク）の検証
- is_allowed_host_header: Hostヘッダーの検証（DNSリバインディング対策）
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import PackageValidationError, RepPatchError

SIGNATURE_SALT = b"myao-rep-patch-manifest-v1"
PBKDF2_ROUNDS = 200_000
_TAILSCALE_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_DNS_SUFFIX = ".ts.net"


def is_allowed_client_ip(client_host: str, allow_vpn: bool = False) -> bool:
    """
    接続元クライアントIPが許可されているかを検証する。

    仕様:
    - ループバック（127.0.0.0/8, ::1, localhost）は常に許可
    - allow_vpn が True の場合:
      - プライベートネットワーク（RFC 1918 / IPv6 ULA）
      - Tailscale CGNAT（100.64.0.0/10）
      - リンクローカル（fe80::/10, 169.254.0.0/16）
      からのアクセスを許可
    - パブリックIPや不正なホスト文字列は拒否
    """
    if not client_host:
        return False
    if client_host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    # IPv6ソケットで受けたIPv4接続（::ffff:a.b.c.d）はIPv4として判定する
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped

    if ip.is_loopback:
        return True

    if allow_vpn:
        if ip.is_private or ip.is_link_local:
            return True
        if ip.version == 4 and ip in _TAILSCALE_CGNAT_NETWORK:
            return True

    return False


def _host_without_port(host_header: str) -> str:
    value = host_header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else ""
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def is_allowed_host_header(
    host_header: str,
    *,
    allow_vpn: bool = False,
    configured_host: str = "",
    known_names: Iterable[str] = (),
) -> bool:
    """
    リクエストのHostヘッダーが、このアプリを指す名前として妥当かを検証する。

    悪意あるWebページが自分のドメインをこのPCのアドレスへ向け直し、同一オリジンとして
    APIを呼び出す攻撃（DNSリバインディング）を防ぐ。攻撃者が用意できるのは公開ドメイン名だけ
    なので、次のいずれかに当てはまるHostだけを受け付ける。

    - IPアドレスそのもの（接続元IPは is_allowed_client_ip で別途検証する）
    - localhost / *.localhost
    - ドットを含まない名前（LAN内のマシン名やMagicDNSの短縮名）
    - 待受ホストとして設定された名前、およびこのPC自身のホスト名（known_names）
    - allow_vpn が True の場合の Tailscale MagicDNS 名（*.ts.net）
    """
    host = _host_without_port(host_header)
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if "." not in host:
        return True
    accepted = {name.strip().lower().rstrip(".") for name in known_names if name}
    if configured_host:
        accepted.add(configured_host.strip().lower().rstrip("."))
    if host in accepted:
        return True
    return allow_vpn and host.endswith(_TAILSCALE_DNS_SUFFIX)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: dict[str, Any]) -> bytes:
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    return json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@lru_cache(maxsize=8)
def _derive_hmac_key_cached(password: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), SIGNATURE_SALT, PBKDF2_ROUNDS)


def derive_hmac_key(password: str) -> bytes:
    if not password:
        raise RepPatchError("パッチ検証用パスワードが設定されていません")
    return _derive_hmac_key_cached(password)


def _signature(value: dict[str, Any], password: str) -> str:
    return hmac.new(derive_hmac_key(password), canonical_json(value), hashlib.sha256).hexdigest()


def sign_document(value: dict[str, Any], password: str) -> dict[str, Any]:
    signed = dict(value)
    signed["signature"] = _signature(value, password)
    return signed


def verify_document(value: dict[str, Any], password: str) -> None:
    signature = value.get("signature")
    if not isinstance(signature, str):
        raise PackageValidationError("署名がありません")
    if not hmac.compare_digest(signature, _signature(value, password)):
        raise PackageValidationError("パスワードが異なるか、パッケージが改変されています")


def _is_git_metadata_part(part: str) -> bool:
    # Windowsは末尾の空白・ドットを無視し、8.3形式の短縮名（GIT~1）でも .git を開けてしまう
    normalized = part.rstrip(" .").lower()
    return normalized in {".git", "git~1"}


def safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        raise PackageValidationError(f"安全でないパスです: {value}")
    if ":" in path.parts[0]:
        raise PackageValidationError(f"安全でないパスです: {value}")
    if any(_is_git_metadata_part(part) for part in path.parts):
        raise PackageValidationError(f"Gitの管理領域を指すパスは使用できません: {value}")
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
