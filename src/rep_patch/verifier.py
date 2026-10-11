"""
会社側クリーン環境シミュレーション検証モジュール

会社へ届くのはGitにコミットされたファイルだけである。自宅の作業ツリーにだけ存在するファイルへ
依存していないか、また今回の差分が会社側で適用できる形かを、公開前に検査する。

仕様:
- verify_clean_repository:
  1. 対象コミット（固定ブランチの先頭）のGit追跡ファイル一覧を取得（会社へ届くファイルの全体）
  2. 自宅ローカルの作業ツリーに存在し、.gitignore等でGit管理から外れている重要設定ファイル（.json, .txt, .yaml, .toml等）を検知し、ファイル欠落エラーとして報告
  3. Git追跡対象に .example / .sample / .template / .default 等のテンプレートファイルが存在する場合、ローカル実ファイルの欠落を許容（エラーから除外）
  4. ignore_patterns（カスタム除外パターン）に合致するファイルは検証から除外
  5. 直前コミット（baseline_commit）が指定されている場合、そこからの差分が公開・適用できる形かを検査
     - 直前コミットが対象コミットの祖先であること
     - 差分に、会社側が受け付けないパスやサブモジュールが含まれないこと
  6. 検証結果（valid, repo_id, branch, target_commit, errors, warnings, checked_files）を返却
- verify_repository_config: リポジトリ設定（RepositoryConfig）から検証条件を組み立てて検証
- verify_all_repositories: 有効な全リポジトリを一括検証して集計
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

from .config import RepositoryConfig
from .errors import PackageValidationError
from .git import diff_file_status, ensure_repository, is_ancestor, resolve_commit, tracked_files
from .security import safe_relative_path


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


def unsupported_diff_entries(
    repo: Path, base: str, target: str, target_entries: list[dict[str, str]]
) -> list[str]:
    """base..target の差分のうち、会社側で適用できないものを説明文の一覧で返す。"""
    problems: list[str] = []
    entry_types = {item["path"]: item["type"] for item in target_entries}
    status = diff_file_status(repo, base, target)
    for rel_path in sorted(set(status["added"] + status["modified"] + status["deleted"])):
        try:
            safe_relative_path(rel_path)
        except PackageValidationError:
            problems.append(f"会社側で適用できないパスが差分に含まれています: '{rel_path}'")
    for rel_path in sorted(set(status["added"] + status["modified"])):
        if entry_types.get(rel_path, "blob") != "blob":
            problems.append(f"サブモジュールはパッチに含められません: '{rel_path}'")
    return problems


def verify_clean_repository(
    repo_path: Path | str,
    branch: str = "main",
    baseline_commit: str | None = None,
    ignore_patterns: list[str] | None = None,
    *,
    repo_id: str | None = None,
) -> dict[str, Any]:
    """
    リポジトリが会社側クリーン環境（Git追跡対象ファイルのみ）で正常に動作・整合するかをシミュレーション検証する
    """
    repo = Path(repo_path).expanduser().resolve()
    ensure_repository(repo)

    target_commit = resolve_commit(repo, branch)
    custom_patterns = list(ignore_patterns or [])

    errors: list[str] = []
    warnings: list[str] = []

    # 1. 対象コミットで会社へ届くファイル一覧、およびテンプレート（.example等）の基底パス
    target_entries = tracked_files(repo, target_commit)
    tracked_rel_paths = {item["path"] for item in target_entries}
    tracked_template_bases = {
        rel[: -len(suffix)]
        for rel in tracked_rel_paths
        for suffix in TEMPLATE_SUFFIXES
        if rel.endswith(suffix)
    }

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
            if rel.suffix.lower() not in CONFIG_EXTENSIONS or _is_noise_path(rel):
                continue

            rel_posix = rel.as_posix()
            if _matches_any_pattern(rel_posix, custom_patterns):
                continue
            # Git側にテンプレート（.example等）が存在する場合は環境別ローカル設定とみなし除外
            if rel_posix in tracked_template_bases:
                continue
            if rel_posix not in tracked_rel_paths:
                errors.append(
                    f"重要設定ファイル '{rel_posix}' がローカルに存在しますが、.gitignoreまたは未コミットのためGit管理に含まれていません。会社環境でファイル不在エラーが発生します。"
                )

    # 3. 直前コミットからの差分が公開・適用できる形かの検査
    if baseline_commit:
        base_resolved = resolve_commit(repo, baseline_commit)
        if base_resolved != target_commit:
            if not is_ancestor(repo, base_resolved, target_commit):
                errors.append(
                    f"直前の公開地点 {base_resolved[:8]} が {branch} の祖先ではないため、差分パッチを作成できません。"
                )
            else:
                errors.extend(unsupported_diff_entries(repo, base_resolved, target_commit, target_entries))

    return {
        "valid": not errors,
        "repo_id": repo_id or repo.name,
        "branch": branch,
        "target_commit": target_commit,
        "errors": errors,
        "warnings": warnings,
        "checked_files": sorted(tracked_rel_paths),
    }


def verify_repository_config(config: RepositoryConfig) -> dict[str, Any]:
    """リポジトリ設定の固定ブランチ・直前の公開地点・除外パターンで検証する"""
    return verify_clean_repository(
        config.path,
        branch=config.branch or "main",
        baseline_commit=config.published_commit or config.baseline_commit or None,
        ignore_patterns=config.clean_verify_ignore_patterns,
        repo_id=config.repo_id,
    )


def verify_all_repositories(repositories: dict[str, RepositoryConfig]) -> dict[str, Any]:
    """登録された有効な全リポジトリの会社側クリーン環境シミュレーション検証を一括実行する"""
    results: dict[str, dict[str, Any]] = {}
    for repo_id, config in repositories.items():
        if not config.enabled:
            continue
        try:
            results[repo_id] = verify_repository_config(config)
        except Exception as exc:  # noqa: BLE001 - report the failure as this repository's result
            results[repo_id] = {
                "valid": False,
                "repo_id": repo_id,
                "branch": config.branch,
                "target_commit": "",
                "errors": [str(exc)],
                "warnings": [],
                "checked_files": [],
            }
    return {
        "all_valid": all(item["valid"] for item in results.values()),
        "total_errors": sum(len(item["errors"]) for item in results.values()),
        "total_warnings": sum(len(item["warnings"]) for item in results.values()),
        "results": results,
    }
