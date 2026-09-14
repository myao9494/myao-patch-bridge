"""
会社側クリーン環境シミュレーション検証モジュール（verifier.py）のテスト

仕様:
- verify_clean_repository:
  1. Git管理対象ファイルのみを展開したクリーン環境（一時ディレクトリ）を構築
  2. ローカルに存在する重要設定ファイルが .gitignore 等でGit管理から漏れている場合、ファイル欠落エラーを検知
  3. Git管理対象に正しく含まれている場合は検証合格（valid=True）を返却
  4. 直前コミットからのパッチ生成および模擬適用がクリーン環境で正常に完了することを検証
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from rep_patch.verifier import verify_clean_repository


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)


def test_verify_clean_repository_detects_missing_ignored_config(tmp_path: Path) -> None:
    """ローカルに存在する重要設定ファイルが.gitignoreで除外されてコミット漏れしている場合、エラーを検知する"""
    repo = tmp_path / "app_repo"
    _init_git_repo(repo)

    # .gitignore に data/ を含める（以前の不具合パターン）
    (repo / ".gitignore").write_text("data/\n__pycache__/\n", encoding="utf-8")
    (repo / "main.py").write_text("from pathlib import Path\nconfig = Path('data/config.json').read_text()\n", encoding="utf-8")

    # Gitコミット（data/ は除外されているためコミットされない）
    subprocess.run(["git", "add", ".gitignore", "main.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)

    # ローカルには data/config.json を作成（自宅ローカルでは動くがGitに入っていない）
    data_dir = repo / "data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text('{"key": "value"}', encoding="utf-8")

    # クリーン環境シミュレーション検証を実行
    result = verify_clean_repository(repo, branch="main")

    assert result["valid"] is False
    assert len(result["errors"]) > 0
    # data/config.json がGit管理外で欠落している旨のエラーが含まれること
    assert any("config.json" in err for err in result["errors"])


def test_verify_clean_repository_success_when_tracked(tmp_path: Path) -> None:
    """設定ファイルがGit追跡対象に含まれている場合、検証合格（valid=True）となる"""
    repo = tmp_path / "app_repo"
    _init_git_repo(repo)

    # .gitignore では *.db のみを除外し、設定ファイルはコミットする（修正後パターン）
    (repo / ".gitignore").write_text("data/*.db\n", encoding="utf-8")
    (repo / "main.py").write_text("print('hello')", encoding="utf-8")
    data_dir = repo / "data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text('{"key": "value"}', encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Commit with config"], cwd=repo, check=True)

    # 端末固有DBのみローカルに存在（除外対象）
    (data_dir / "local.db").write_text("db binary", encoding="utf-8")

    result = verify_clean_repository(repo, branch="main")

    assert result["valid"] is True
    assert len(result["errors"]) == 0
    assert "data/config.json" in result["checked_files"]


def test_verify_clean_repository_with_baseline_patch_simulation(tmp_path: Path) -> None:
    """直前コミット（baseline）からの差分適用シミュレーションが成功すること"""
    repo = tmp_path / "app_repo"
    _init_git_repo(repo)

    (repo / "file1.txt").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=repo, check=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    (repo / "file1.txt").write_text("v2", encoding="utf-8")
    (repo / "file2.txt").write_text("new file", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=repo, check=True)

    result = verify_clean_repository(repo, branch="main", baseline_commit=c1)

    assert result["valid"] is True
    assert len(result["errors"]) == 0
    assert "file2.txt" in result["checked_files"]


def test_verify_all_repositories(tmp_path: Path) -> None:
    """登録された複数リポジトリの一括クリーン検証が正しく集計されること"""
    from rep_patch.config import RepositoryConfig
    from rep_patch.verifier import verify_all_repositories

    repo1 = tmp_path / "repo1"
    _init_git_repo(repo1)
    (repo1 / "main.py").write_text("print('ok')", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo1, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo1, check=True)

    repo2 = tmp_path / "repo2"
    _init_git_repo(repo2)
    (repo2 / ".gitignore").write_text("config.json\n", encoding="utf-8")
    (repo2 / "main.py").write_text("print('ok')", encoding="utf-8")
    (repo2 / "config.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "main.py"], cwd=repo2, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo2, check=True)

    configs = {
        "repo1": RepositoryConfig(repo_id="repo1", display_name="Repo 1", path=str(repo1), enabled=True),
        "repo2": RepositoryConfig(repo_id="repo2", display_name="Repo 2", path=str(repo2), enabled=True),
        "repo3": RepositoryConfig(repo_id="repo3", display_name="Repo 3", path=str(tmp_path / "not_exist"), enabled=False),
    }

    summary = verify_all_repositories(configs)
    assert summary["all_valid"] is False
    assert summary["results"]["repo1"]["valid"] is True
    assert summary["results"]["repo2"]["valid"] is False
    assert "repo3" not in summary["results"]  # enabled=False はスキップ

