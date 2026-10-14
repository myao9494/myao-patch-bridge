"""
Git LFS で管理されているファイルの実体を取り出すモジュール

仕様:
- parse_lfs_pointer: Gitに保存されたバイト列がLFSポインタなら (oid, size) を返す
- resolve_lfs_pointer: ポインタを実体へ置き換える。端末内のLFSキャッシュを先に探し、
  無ければ `git lfs smudge`（git-lfs が必要）で取得する。取得した実体はサイズとSHA-256を必ず照合する

補足:
- 会社PCはオフラインでLFSサーバーへ接続できないため、自宅側で実体を取り出して同梱する。
- 実体を確認できないときは、ポインタを黙って運ばず例外にする。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .errors import RepPatchError
from .git import run_git

_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"
_POINTER_MAX_BYTES = 1024
_FETCH_TIMEOUT_SECONDS = 1800


def parse_lfs_pointer(content: bytes) -> tuple[str, int] | None:
    if len(content) > _POINTER_MAX_BYTES or not content.startswith(_POINTER_PREFIX):
        return None
    # 拡張行（ext-N-...）が付く形式もあるため、必須の2行だけを取り出す
    oid = re.search(rb"^oid sha256:([0-9a-f]{64})$", content, re.MULTILINE)
    size = re.search(rb"^size (\d+)$", content, re.MULTILINE)
    if not oid or not size:
        return None
    return oid.group(1).decode("ascii"), int(size.group(1))


def _cached_object(repo: Path, oid: str) -> Path | None:
    try:
        common = run_git(repo, ["rev-parse", "--git-common-dir"]).text
    except RepPatchError:
        return None
    base = Path(common)
    if not base.is_absolute():
        base = repo / base
    candidate = base / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    return candidate if candidate.is_file() else None


def _matches(content: bytes, oid: str, size: int) -> bool:
    return len(content) == size and hashlib.sha256(content).hexdigest() == oid


def resolve_lfs_pointer(repo: Path, pointer: bytes, rel_path: str) -> bytes:
    """LFSポインタの実体を返す。取り出せない・内容が一致しない場合は RepPatchError"""
    parsed = parse_lfs_pointer(pointer)
    if parsed is None:
        raise RepPatchError(f"Git LFSのポインタではありません: '{rel_path}'")
    oid, size = parsed

    cached = _cached_object(repo, oid)
    if cached is not None:
        content = cached.read_bytes()
        if _matches(content, oid, size):
            return content

    result = run_git(
        repo, ["lfs", "smudge"], input_bytes=pointer, check=False, timeout=_FETCH_TIMEOUT_SECONDS
    )
    if result.returncode == 0 and _matches(result.stdout, oid, size):
        return result.stdout
    detail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
    hint = detail[-1] if detail else "実体を取得できませんでした"
    raise RepPatchError(
        f"Git LFSの実体を取得できないため、会社へ届けられません: '{rel_path}' ({hint})。"
        f"自宅側リポジトリ（{repo}）で git-lfs を導入し、`git lfs pull` で実体を取得してから公開してください"
    )
