from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import GitCommandError, RepPatchError


@dataclass
class GitResult:
    stdout: bytes
    stderr: bytes
    returncode: int

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace").strip()


def run_git(
    repo: Path | None,
    args: list[str],
    *,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> GitResult:
    command = ["git"]
    if repo is not None:
        command.extend(["-C", str(repo)])
    command.extend(args)
    completed = subprocess.run(
        command,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    result = GitResult(completed.stdout, completed.stderr, completed.returncode)
    if check and completed.returncode != 0:
        raise GitCommandError(args, completed.stderr.decode("utf-8", errors="replace"), completed.returncode)
    return result


def ensure_repository(path: Path) -> None:
    if not path.is_dir():
        raise RepPatchError(f"リポジトリフォルダが見つかりません: {path}")
    result = run_git(path, ["rev-parse", "--is-inside-work-tree"], check=False)
    if result.returncode != 0 or result.text != "true":
        raise RepPatchError(f"Gitリポジトリではありません: {path}")


def repository_status(path: Path) -> dict[str, object]:
    ensure_repository(path)
    status = run_git(path, ["status", "--porcelain=v1", "-z"]).stdout
    branch = run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"]).text
    head = run_git(path, ["rev-parse", "HEAD"]).text
    return {
        "path": str(path),
        "branch": branch,
        "head": head,
        "clean": not bool(status),
        "changes": len([item for item in status.split(b"\0") if item]),
    }


def resolve_commit(path: Path, revision: str) -> str:
    if not revision:
        raise RepPatchError("コミットまたはブランチが設定されていません")
    return run_git(path, ["rev-parse", f"{revision}^{{commit}}"]).text


def is_ancestor(path: Path, older: str, newer: str) -> bool:
    return run_git(path, ["merge-base", "--is-ancestor", older, newer], check=False).returncode == 0


def tracked_files(path: Path, revision: str | None = None) -> list[dict[str, str]]:
    if revision:
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

    raw = run_git(path, ["ls-files", "-s", "-z"]).stdout
    result = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, filename = record.split(b"\t", 1)
        mode, object_id, stage = metadata.decode("ascii").split(" ")
        if stage != "0":
            raise RepPatchError(f"未解決の競合があります: {path}")
        result.append(
            {
                "path": filename.decode("utf-8", errors="surrogateescape"),
                "blob": object_id,
                "mode": mode,
                "type": "blob" if mode != "160000" else "commit",
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
