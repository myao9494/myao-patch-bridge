"""
Gitコマンド実行およびリポジトリ状態解析を行うユーティリティモジュール

仕様:
- run_git: Gitサブプロセスの安全な実行と結果取得
  - shellを使わない。標準入力は端末から切り離し、認証プロンプトで待ち続けないようにする
  - timeout（秒）を指定した場合、超過するとGitCommandErrorになる
- is_repository / ensure_repository: 作業ツリーを持つGitリポジトリかの判定
- repository_status: ブランチ・HEAD・クリーン状態・変更件数を1回のgit実行で取得
- resolve_commit: ブランチ名・タグ・HEAD・コミットハッシュの完全ID解決
- is_ancestor: コミット間の祖先関係判定
- tracked_files / changed_paths: 追跡対象ファイル一覧およびコミット間差分ファイル取得
- diff_file_status: コミット間の追加・変更・削除ファイル一覧の分類取得
- read_blobs: 複数のblob実体を1回のgit実行でまとめて取得
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from .errors import GitCommandError, RepPatchError

# リモートとの通信（push）に許す最大時間。大きなパッチを遅い回線で送る場合も考慮して長めにとる。
NETWORK_TIMEOUT_SECONDS = 30 * 60


@dataclass
class GitResult:
    stdout: bytes
    stderr: bytes
    returncode: int

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace").strip()


class RepositoryStatus(TypedDict):
    path: str
    branch: str
    head: str
    clean: bool
    changes: int


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # 認証情報を端末へ問い合わせて待ち続けるのを防ぐ（失敗として即座に返させる）
    environment["GIT_TERMINAL_PROMPT"] = "0"
    # 状態取得のたびにインデックスを書き換えず、エディタ等が実行するgitとロックを奪い合わない
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def run_git(
    repo: Path | None,
    args: list[str],
    *,
    check: bool = True,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
) -> GitResult:
    command = ["git"]
    if repo is not None:
        command.extend(["-C", str(repo)])
    command.extend(args)
    try:
        completed = subprocess.run(
            command,
            input=input_bytes,
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            capture_output=True,
            check=False,
            env=_git_environment(),
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RepPatchError("gitコマンドが見つかりません。Gitをインストールし、PATHへ登録してください") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(args, f"{timeout:.0f}秒以内に完了しなかったため中断しました", -1) from exc
    result = GitResult(completed.stdout, completed.stderr, completed.returncode)
    if check and completed.returncode != 0:
        raise GitCommandError(args, completed.stderr.decode("utf-8", errors="replace"), completed.returncode)
    return result


def is_repository(path: Path) -> bool:
    if not path.is_dir():
        return False
    result = run_git(path, ["rev-parse", "--is-inside-work-tree"], check=False)
    return result.returncode == 0 and result.text == "true"


def ensure_repository(path: Path) -> None:
    if not path.is_dir():
        raise RepPatchError(f"リポジトリフォルダが見つかりません: {path}")
    if not is_repository(path):
        raise RepPatchError(f"Gitリポジトリではありません: {path}")


def repository_status(path: Path) -> RepositoryStatus:
    """ブランチ・HEAD・クリーン状態・変更件数を返す。"""
    if not path.is_dir():
        raise RepPatchError(f"リポジトリフォルダが見つかりません: {path}")
    result = run_git(path, ["status", "--porcelain=v2", "--branch", "-z"], check=False)
    if result.returncode != 0:
        ensure_repository(path)
        raise GitCommandError(
            ["status"], result.stderr.decode("utf-8", errors="replace"), result.returncode
        )
    branch = "HEAD"
    head = ""
    changes = 0
    records = iter(result.stdout.split(b"\0"))
    for record in records:
        if not record:
            continue
        if record.startswith(b"# branch.oid "):
            head = record[len(b"# branch.oid "):].decode("ascii", errors="replace")
        elif record.startswith(b"# branch.head "):
            name = record[len(b"# branch.head "):].decode("utf-8", errors="replace")
            branch = "HEAD" if name == "(detached)" else name
        elif record.startswith(b"# "):
            continue
        else:
            changes += 1
            if record.startswith(b"2 "):
                # リネーム・コピーは変更前のパスが次のレコードとして続く
                next(records, None)
    if head in {"", "(initial)"}:
        raise RepPatchError(f"コミットがまだありません: {path}")
    return {
        "path": str(path),
        "branch": branch,
        "head": head,
        "clean": changes == 0,
        "changes": changes,
    }


def resolve_commit(path: Path, revision: str) -> str:
    if not revision:
        raise RepPatchError("コミットまたはブランチが設定されていません")
    return run_git(path, ["rev-parse", f"{revision}^{{commit}}"]).text


def is_ancestor(path: Path, older: str, newer: str) -> bool:
    return run_git(path, ["merge-base", "--is-ancestor", older, newer], check=False).returncode == 0


def tracked_files(path: Path, revision: str) -> list[dict[str, str]]:
    """revision時点の全追跡エントリ（path / blob / mode / type）をパス順で返す。"""
    raw = run_git(path, ["ls-tree", "-r", "-z", revision]).stdout
    result: list[dict[str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, filename = record.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split(" ")
        result.append(
            {
                "path": filename.decode("utf-8", errors="surrogateescape"),
                "blob": object_id,
                "mode": mode,
                "type": object_type,
            }
        )
    return sorted(result, key=lambda item: item["path"])


def changed_paths(path: Path, older: str, newer: str) -> list[str]:
    raw = run_git(path, ["diff", "--name-only", "-z", older, newer, "--"]).stdout
    return sorted(
        item.decode("utf-8", errors="surrogateescape")
        for item in raw.split(b"\0")
        if item
    )


def diff_file_status(path: Path, older: str, newer: str) -> dict[str, list[str]]:
    raw = run_git(path, ["diff", "--name-status", "-z", older, newer, "--"]).stdout
    parts = raw.split(b"\0")
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    i = 0
    while i < len(parts):
        token = parts[i]
        if not token:
            i += 1
            continue
        status_code = token.decode("ascii", errors="replace")
        if status_code.startswith("R") or status_code.startswith("C"):
            if i + 2 >= len(parts):
                break
            old_path = parts[i + 1].decode("utf-8", errors="surrogateescape")
            new_path = parts[i + 2].decode("utf-8", errors="surrogateescape")
            if status_code.startswith("R"):
                deleted.append(old_path)
            added.append(new_path)
            i += 3
        else:
            if i + 1 >= len(parts):
                break
            file_path = parts[i + 1].decode("utf-8", errors="surrogateescape")
            if status_code == "A":
                added.append(file_path)
            elif status_code == "D":
                deleted.append(file_path)
            else:
                modified.append(file_path)
            i += 2
    return {
        "added": sorted(set(added)),
        "modified": sorted(set(modified)),
        "deleted": sorted(set(deleted)),
    }


def read_blobs(path: Path, object_ids: Iterable[str]) -> dict[str, bytes]:
    """
    blobの実体をまとめて取得する（オブジェクトID → バイト列）。

    ファイルごとにgitを起動せず、`git cat-file --batch` の1プロセスで読み出す。
    """
    wanted = sorted(set(object_ids))
    if not wanted:
        return {}
    output = run_git(
        path, ["cat-file", "--batch"], input_bytes=("\n".join(wanted) + "\n").encode("ascii")
    ).stdout
    result: dict[str, bytes] = {}
    position = 0
    while position < len(output):
        line_end = output.index(b"\n", position)
        header = output[position:line_end].decode("ascii", errors="replace").split(" ")
        position = line_end + 1
        if len(header) != 3:
            raise RepPatchError(f"Gitオブジェクトを読み出せません: {' '.join(header)}")
        object_id, object_type, size_text = header
        size = int(size_text)
        if object_type != "blob":
            raise RepPatchError(f"ファイル実体ではないGitオブジェクトです: {object_id} ({object_type})")
        result[object_id] = output[position : position + size]
        position += size + 1  # 内容の直後に改行が1つ付く
    missing = [object_id for object_id in wanted if object_id not in result]
    if missing:
        raise RepPatchError(f"Gitオブジェクトが見つかりません: {missing[0]}")
    return result
