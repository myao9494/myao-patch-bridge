"""
会社側クリーン環境シミュレーション検証モジュール（verifier.py）のテスト

仕様:
- verify_clean_repository:
  1. 対象コミットのGit追跡ファイル（会社へ届くファイル）だけを基準に検証する
  2. ローカルに存在する重要設定ファイルが .gitignore 等でGit管理から漏れている場合、ファイル欠落エラーを検知
  3. Git管理対象に正しく含まれている場合は検証合格（valid=True）を返却
  4. .example / .sample / .template 等のテンプレートファイルがGit管理に含まれている場合、実ファイル欠落を許容（エラーとしない）
  5. ignore_patterns（除外パターン）に一致するファイルは検証から除外
  6. 直前コミットからの差分が公開・適用できる形（祖先関係、サブモジュールなし）であることを検証
  7. キャッシュ・ログ・ロックファイルなどのノイズは欠落として扱わない
"""
from __future__ import annotations

import subprocess
from pathlib import Path

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


def test_verify_clean_repository_with_baseline_diff_check(tmp_path: Path) -> None:
    """直前コミット（baseline）からの差分が公開できる形であれば合格すること"""
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


def test_verify_clean_repository_ignores_files_with_template_in_git(tmp_path: Path) -> None:
    """Git管理に対象の.example等のテンプレートファイルが存在する場合、ローカル実ファイルの欠落をエラーとしない"""
    repo = tmp_path / "app_repo"
    _init_git_repo(repo)

    # .gitignore に実設定ファイルを除外指定し、.example のみコミット
    (repo / ".gitignore").write_text("data/synonym_groups.txt\n!data/*.example\n", encoding="utf-8")
    (repo / "main.py").write_text("print('ok')", encoding="utf-8")
    data_dir = repo / "data"
    data_dir.mkdir()
    (data_dir / "synonym_groups.txt.example").write_text("template", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add template"], cwd=repo, check=True)

    # ローカルで実ファイルを作成（.gitignoreされている）
    (data_dir / "synonym_groups.txt").write_text("local custom synonyms", encoding="utf-8")

    result = verify_clean_repository(repo, branch="main")

    # .example がGitにあるため、synonym_groups.txt の欠落エラーにはならない
    assert result["valid"] is True
    assert len(result["errors"]) == 0


def test_verify_clean_repository_with_custom_ignore_patterns(tmp_path: Path) -> None:
    """ignore_patterns に指定されたパスやGlobパターンに一致するファイルは検証エラーから除外される"""
    repo = tmp_path / "app_repo"
    _init_git_repo(repo)

    (repo / ".gitignore").write_text("backend/data/\n", encoding="utf-8")
    (repo / "main.py").write_text("print('ok')", encoding="utf-8")
    backend_data = repo / "backend" / "data"
    backend_data.mkdir(parents=True)
    (backend_data / ".gitkeep").write_text("", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init with gitkeep"], cwd=repo, check=True)

    # ローカル専用の設定ファイル群
    (backend_data / "config.json").write_text("{}", encoding="utf-8")
    (backend_data / "custom.txt").write_text("custom", encoding="utf-8")

    # 1. 除外パターンなしではエラー
    res_default = verify_clean_repository(repo, branch="main")
    assert res_default["valid"] is False

    # 2. ignore_patterns に "backend/data/*" を指定した場合は合格
    res_ignored = verify_clean_repository(
        repo,
        branch="main",
        ignore_patterns=["backend/data/*", "backend/data/**"],
    )
    assert res_ignored["valid"] is True
    assert len(res_ignored["errors"]) == 0


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_verify_reports_baseline_that_is_not_an_ancestor(tmp_path: Path) -> None:
    """直前の公開地点が固定ブランチの履歴に無い（リベース等）場合、公開前にエラーとして検知する"""
    repo = tmp_path / "app_repo"
    _init_git_repo(repo)
    (repo / "main.py").write_text("v1", encoding="utf-8")
    _commit(repo, "v1")
    subprocess.run(["git", "checkout", "-b", "side"], cwd=repo, check=True, capture_output=True)
    (repo / "side.py").write_text("side", encoding="utf-8")
    side = _commit(repo, "side")
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "main.py").write_text("v2", encoding="utf-8")
    _commit(repo, "v2")

    result = verify_clean_repository(repo, branch="main", baseline_commit=side)

    assert result["valid"] is False
    assert any("祖先ではない" in err for err in result["errors"])


def test_verify_reports_submodule_in_diff(tmp_path: Path) -> None:
    inner = tmp_path / "inner"
    _init_git_repo(inner)
    (inner / "lib.py").write_text("lib", encoding="utf-8")
    _commit(inner, "lib")

    repo = tmp_path / "app_repo"
    _init_git_repo(repo)
    (repo / "main.py").write_text("v1", encoding="utf-8")
    base = _commit(repo, "v1")
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(inner), "vendor/inner"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    _commit(repo, "add submodule")

    result = verify_clean_repository(repo, branch="main", baseline_commit=base)

    assert result["valid"] is False
    assert any("サブモジュール" in err and "vendor/inner" in err for err in result["errors"])


def test_verify_ignores_noise_and_reports_only_config_files(tmp_path: Path) -> None:
    repo = tmp_path / "app_repo"
    _init_git_repo(repo)
    (repo / "main.py").write_text("print('ok')", encoding="utf-8")
    _commit(repo, "init")

    noise = [
        "node_modules/pkg/package.json",
        ".venv/pyvenv.cfg",
        ".cache/state.json",
        "backup/old.json",
        "app.log",
        "session_state.json",
        "package-lock.json",
        "notes.md",
        "data.bin",
    ]
    for name in noise:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("x", encoding="utf-8")
    assert verify_clean_repository(repo, branch="main")["valid"] is True

    (repo / "conf").mkdir()
    (repo / "conf" / "settings.yaml").write_text("a: 1", encoding="utf-8")
    result = verify_clean_repository(repo, branch="main")
    assert result["valid"] is False
    assert len(result["errors"]) == 1 and "conf/settings.yaml" in result["errors"][0]


def test_verify_uses_fixed_branch_not_the_checked_out_one(tmp_path: Path) -> None:
    """検証対象は固定ブランチの内容であり、作業中の別ブランチにだけあるファイルは「届かない」と判定する"""
    repo = tmp_path / "app_repo"
    _init_git_repo(repo)
    (repo / "main.py").write_text("print('ok')", encoding="utf-8")
    _commit(repo, "init")
    subprocess.run(["git", "checkout", "-b", "wip"], cwd=repo, check=True, capture_output=True)
    (repo / "config.json").write_text("{}", encoding="utf-8")
    _commit(repo, "config on wip only")

    assert verify_clean_repository(repo, branch="wip")["valid"] is True
    assert verify_clean_repository(repo, branch="main")["valid"] is False


def test_verify_all_repositories_reports_broken_repository(tmp_path: Path) -> None:
    from rep_patch.config import RepositoryConfig
    from rep_patch.verifier import verify_all_repositories

    summary = verify_all_repositories(
        {"gone": RepositoryConfig(repo_id="gone", display_name="Gone", path=str(tmp_path / "not_exist"))}
    )
    assert summary["all_valid"] is False
    assert summary["total_errors"] == 1
    assert summary["results"]["gone"]["repo_id"] == "gone"
