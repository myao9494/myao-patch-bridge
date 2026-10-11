"""
Gitユーティリティ（rep_patch/git.py）のテスト

仕様:
- repository_status: ブランチ・HEAD・クリーン状態・変更件数を返す（未追跡・変更・リネーム・detached HEAD）
- repository_status / ensure_repository: リポジトリでないフォルダ、コミットの無いリポジトリは分かるエラーにする
- diff_file_status: 追加・変更・削除・リネームを分類する（リネームは旧パス削除＋新パス追加）
- read_blobs: 複数のファイル実体（空・バイナリ・日本語パスを含む）を1回の実行で取得する
- run_git: gitが認証情報を端末へ問い合わせて待ち続けない／タイムアウトを失敗として返す
"""
from __future__ import annotations

from pathlib import Path

import pytest

from helpers import commit_all, git, init_repo
from rep_patch.errors import GitCommandError, RepPatchError
from rep_patch.git import (
    diff_file_status,
    ensure_repository,
    is_repository,
    read_blobs,
    repository_status,
    run_git,
    tracked_files,
)


def _repo(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n" * 20, encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    commit_all(repo, "init")
    return repo


def test_repository_status_reports_branch_head_and_changes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD").decode().strip()

    status = repository_status(repo)
    assert status == {"path": str(repo), "branch": "main", "head": head, "clean": True, "changes": 0}

    (repo / "b.txt").write_text("changed\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    git(repo, "mv", "a.txt", "renamed.txt")
    status = repository_status(repo)
    assert status["clean"] is False
    assert status["changes"] == 3  # 変更1・未追跡1・リネーム1（リネームを2件と数えない）

    git(repo, "stash", "--include-untracked")
    git(repo, "checkout", "--detach")
    status = repository_status(repo)
    assert status["branch"] == "HEAD"
    assert status["head"] == head


def test_repository_errors_are_explained(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert is_repository(plain) is False
    assert is_repository(tmp_path / "missing") is False
    with pytest.raises(RepPatchError, match="Gitリポジトリではありません"):
        repository_status(plain)
    with pytest.raises(RepPatchError, match="リポジトリフォルダが見つかりません"):
        ensure_repository(tmp_path / "missing")

    empty = init_repo(tmp_path / "empty")
    assert is_repository(empty) is True
    with pytest.raises(RepPatchError, match="コミットがまだありません"):
        repository_status(empty)


def test_diff_file_status_classifies_changes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    git(repo, "mv", "a.txt", "moved.txt")
    (repo / "b.txt").write_text("b changed\n", encoding="utf-8")
    (repo / "追加.txt").write_text("new\n", encoding="utf-8")
    target = commit_all(repo, "change")

    assert diff_file_status(repo, base, target) == {
        "added": ["moved.txt", "追加.txt"],
        "modified": ["b.txt"],
        "deleted": ["a.txt"],
    }


def test_read_blobs_returns_exact_contents(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    contents = {
        "empty.txt": b"",
        "binary.bin": bytes(range(256)) * 3,
        "no-newline.txt": b"no trailing newline",
        "日本語/メモ.txt": "内容\n".encode("utf-8"),
        "copy-of-empty.txt": b"",
    }
    for name, content in contents.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(content)
    head = commit_all(repo, "files")

    entries = {item["path"]: item["blob"] for item in tracked_files(repo, head)}
    blobs = read_blobs(repo, entries.values())
    assert {path: blobs[blob] for path, blob in entries.items()} == contents
    assert read_blobs(repo, []) == {}

    with pytest.raises(RepPatchError, match="読み出せません"):
        read_blobs(repo, ["0" * 40])
    with pytest.raises(RepPatchError, match="ファイル実体ではない"):
        read_blobs(repo, [head])


def test_run_git_does_not_wait_for_terminal_input(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # 標準入力を読むコマンドでも、端末の入力待ちにならず即座に終わる
    result = run_git(repo, ["hash-object", "--stdin"])
    assert result.text == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"  # 空入力のハッシュ
    environment_probe = run_git(repo, ["-c", "alias.env=!env", "env"]).text
    assert "GIT_TERMINAL_PROMPT=0" in environment_probe


def test_run_git_timeout_is_reported_as_failure(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(GitCommandError, match="中断しました"):
        run_git(repo, ["-c", "alias.wait=!sleep 2", "wait"], timeout=0.2)
