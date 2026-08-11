from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import PackageValidationError, RepPatchError

SIGNATURE_SALT = b"myao-rep-patch-manifest-v1"
PBKDF2_ROUNDS = 200_000


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
