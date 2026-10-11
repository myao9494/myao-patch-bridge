"""
テスト共通のフィクスチャとヘルパー

仕様:
- isolated_environment（全テストに自動適用）:
  - HOME / USERPROFILE を一時ディレクトリへ向け、既定のダウンロードフォルダ（~/Downloads）など
    実ユーザーの領域へテストが書き込まないようにする
  - Gitのグローバル設定・システム設定を無効化し、開発者ごとの設定（コミット署名、autocrlf、
    既定ブランチ名、user.name など）にテスト結果が左右されないようにする
- git_helpers: テスト用リポジトリを操作するヘルパー (git, init_repo) を返す（実体は helpers.py）
"""
from __future__ import annotations

import pytest

from helpers import git, init_repo


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path_factory.mktemp("home")
    global_config = home / "gitconfig"
    # user.name / user.email はあえて設定しない（未設定の会社PCでもコミットできることを検証するため）
    global_config.write_text(
        "[init]\n\tdefaultBranch = main\n[core]\n\tautocrlf = false\n[commit]\n\tgpgsign = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def git_helpers():
    return git, init_repo
