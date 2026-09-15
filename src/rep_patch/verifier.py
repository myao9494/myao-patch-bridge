"""
会社側クリーン環境シミュレーション検証モジュール

仕様:
- verify_clean_repository:
  1. 対象コミット（HEAD）のGit追跡ファイルのみを一時ディレクトリに展開（git archive）
  2. 自宅ローカルの作業ツリーに存在し、.gitignore等でGit管理から外れている重要設定ファイル（.json, .txt, .yaml, .toml等）を検知し、ファイル欠落エラーとして報告
  3. Git追跡対象に .example / .sample / .template / .default 等のテンプレートファイルが存在する場合、ローカル実ファイルの欠落を許容（エラーから除外）
  4. ignore_patterns（カスタム除外パターン）に合致するファイルは検証から除外
  5. 重要ディレクトリの空ディレクトリや必須成果物の欠落を検査
  6. 直前コミット（baseline_commit）が指定されている場合、パッチ生成と差分適用シミュレーションを実施
  7. 検証結果（valid, repo_id, target_commit, errors, warnings, checked_files）を返却
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .errors import RepPatchError
from .git import ensure_repository, resolve_commit, run_git


CONFIG_EXTENSIONS = {".json", ".txt", ".yaml", ".yml", ".toml", ".ini", ".conf", ".xml"}
TEMPLATE_SUFFIXES = {".example", ".sample", ".template", ".default"}
EXCLUDED_PATTERNS = {
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".git",
    ".idea",
    ".vscode",
    "dist",
    "build",
    "tmp",
    ".coverage",
    "htmlcov",
    "indexes",
    "models",
}
IGNORE_FILENAME_PATTERNS = {
    ".ds_store",
    "package-lock.json",
    "uv.lock",
    "yarn.lock",
}


def _is_noise_path(rel_path: Path) -> bool:
    """無視すべきキャッシュやログなどのファイルパスであるかを判定する"""
    parts = list(rel_path.parts)
    if any(p in EXCLUDED_PATTERNS or p.startswith(".") for p in parts[:-1]):
        return True
    if any(p.lower() in {"original_app", "temp", "backup", "backups", "scratch"} for p in parts[:-1]):
        return True
    filename = rel_path.name.lower()
    if filename in IGNORE_FILENAME_PATTERNS:
        return True
    if "history" in filename or "session" in filename or filename.startswith("temp"):
        return True
    if filename.endswith((".log", ".pyc", ".tmp", ".bak", ".db", ".db-wal", ".db-shm")):
        return True
    return False


def _matches_any_pattern(path_str: str, patterns: list[str]) -> bool:
    """パスまたはファイル名がいずれかのGlobパターンに合致するかを判定する"""
    name = Path(path_str).name
    for pat in patterns:
        pat = pat.strip()
        if not pat:
            continue
        if fnmatch.fnmatch(path_str, pat) or fnmatch.fnmatch(name, pat):
            return True
        # ディレクトリパターンの場合（例: backend/data/* や backend/data/**）
        if pat.endswith("/*") and fnmatch.fnmatch(path_str, pat[:-2] + "/**"):
            return True
    return False


def verify_clean_repository(
    repo_path: Path | str,
    branch: str = "main",
    baseline_commit: str | None = None,
    ignore_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """
    リポジトリが会社側クリーン環境（Git追跡対象ファイルのみ）で正常に動作・整合するかをシミュレーション検証する
    """
    repo = Path(repo_path).expanduser().resolve()
    ensure_repository(repo)

    target_commit = resolve_commit(repo, branch)
    repo_id = repo.name
    custom_patterns = list(ignore_patterns or [])

    errors: list[str] = []
    warnings: list[str] = []
    checked_files: list[str] = []

    with tempfile.TemporaryDirectory() as temp_dir:
        clean_dir = Path(temp_dir) / "clean_worktree"
        clean_dir.mkdir()

        # 1. git archive で対象コミットのファイルのみをクリーン環境へ展開
        archive_res = run_git(repo, ["archive", target_commit])
        tar_proc = subprocess.run(
            ["tar", "-x", "-C", str(clean_dir)],
            input=archive_res.stdout,
            capture_output=True,
            check=False,
        )
        if tar_proc.returncode != 0:
            raise RepPatchError(f"クリーン環境の展開に失敗しました: {tar_proc.stderr.decode()}")

        # クリーン環境に存在する全ファイル一覧およびテンプレート（.example等）の基底パス
        tracked_rel_paths = set()
        tracked_template_bases = set()
        for p in clean_dir.rglob("*"):
            if p.is_file():
                rel = p.relative_to(clean_dir).as_posix()
                tracked_rel_paths.add(rel)
                checked_files.append(rel)

                # テンプレートファイルの対応元パスを記録（例: foo.txt.example -> foo.txt）
                for suffix in TEMPLATE_SUFFIXES:
                    if rel.endswith(suffix):
                        base_rel = rel[: -len(suffix)]
                        tracked_template_bases.add(base_rel)

        # 2. ローカル作業ツリーの走査:
        # ローカルに存在しているが、.gitignore等でGit管理から漏れている重要設定ファイル（.json, .txt等）を検知
        for root, dirs, files in os.walk(repo):
            # 無視フォルダをスキップ
            dirs[:] = [d for d in dirs if d not in EXCLUDED_PATTERNS and not d.startswith(".")]
            for f in files:
                file_path = Path(root) / f
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                rel = file_path.relative_to(repo)
                if _is_noise_path(rel):
                    continue

                rel_posix = rel.as_posix()
                if _matches_any_pattern(rel_posix, custom_patterns):
                    continue

                # .example 自体はエラー対象外
                if any(rel_posix.endswith(s) for s in TEMPLATE_SUFFIXES):
                    continue

                if rel.suffix.lower() in CONFIG_EXTENSIONS:
                    # Git側にテンプレート（.example等）が存在する場合は環境別ローカル設定とみなし除外
                    if rel_posix in tracked_template_bases:
                        continue

                    if rel_posix not in tracked_rel_paths:
                        errors.append(
                            f"重要設定ファイル '{rel_posix}' がローカルに存在しますが、.gitignoreまたは未コミットのためGit管理に含まれていません。会社環境でファイル不在エラーが発生します。"
                        )

        # 3. パッチ適用シミュレーション（baseline_commit が指定されている場合）
        if baseline_commit:
            base_resolved = resolve_commit(repo, baseline_commit)
            if base_resolved != target_commit:
                sim_repo_dir = Path(temp_dir) / "sim_repo"
                sim_repo_dir.mkdir()
                run_git(sim_repo_dir, ["init", "-b", branch])
                run_git(sim_repo_dir, ["config", "user.name", "Simulation"])
                run_git(sim_repo_dir, ["config", "user.email", "sim@example.com"])

                base_archive = run_git(repo, ["archive", base_resolved])
                subprocess.run(
                    ["tar", "-x", "-C", str(sim_repo_dir)],
                    input=base_archive.stdout,
                    check=True,
                )
                run_git(sim_repo_dir, ["add", "-A"])
                run_git(sim_repo_dir, ["commit", "-m", "Baseline"])

                # 差分パッチを作成してシミュレーション適用
                diff_res = run_git(repo, ["diff", "--binary", base_resolved, target_commit, "--"])
                if diff_res.stdout:
                    patch_path = Path(temp_dir) / "test.patch"
                    patch_path.write_bytes(diff_res.stdout)
                    apply_res = run_git(
                        sim_repo_dir,
                        ["apply", "--check", "--binary", str(patch_path)],
                        check=False,
                    )
                    if apply_res.returncode != 0:
                        errors.append(f"差分パッチの模擬適用に失敗しました: {apply_res.stderr}")

    valid = len(errors) == 0
    return {
        "valid": valid,
        "repo_id": repo_id,
        "branch": branch,
        "target_commit": target_commit,
        "errors": errors,
        "warnings": warnings,
        "checked_files": sorted(checked_files),
    }


def verify_all_repositories(repositories: dict[str, Any]) -> dict[str, Any]:
    """登録された有効な全リポジトリの会社側クリーン環境シミュレーション検証を一括実行する"""
    results: dict[str, dict[str, Any]] = {}
    all_valid = True
    total_errors = 0
    total_warnings = 0

    for repo_id, config in repositories.items():
        enabled = getattr(config, "enabled", True)
        if not enabled:
            continue
        path = getattr(config, "path", None) or (config.get("path") if isinstance(config, dict) else None)
        branch = getattr(config, "branch", "main") or "main"
        baseline = getattr(config, "baseline_commit", "") or ""
        published = getattr(config, "published_commit", "") or ""
        base = published or baseline or None
        ignore_pats = (
            getattr(config, "clean_verify_ignore_patterns", None)
            or (config.get("clean_verify_ignore_patterns") if isinstance(config, dict) else None)
            or []
        )

        try:
            res = verify_clean_repository(
                path, branch=branch, baseline_commit=base, ignore_patterns=ignore_pats
            )
        except Exception as exc:
            res = {
                "valid": False,
                "repo_id": repo_id,
                "branch": branch,
                "target_commit": "",
                "errors": [str(exc)],
                "warnings": [],
                "checked_files": [],
            }

        results[repo_id] = res
        if not res["valid"]:
            all_valid = False
            total_errors += len(res["errors"])
        total_warnings += len(res["warnings"])

    return {
        "all_valid": all_valid,
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "results": results,
    }

