"""
会社環境でのパッチZIP検証・適用・バックアップ・コミット処理を行うモジュール

仕様:
- list_download_packages: ダウンロードフォルダからパッチZIP一覧を取得
- list_company_repositories: 会社側で登録・検出されたリポジトリ一覧と状態を取得（除外リポジトリをフィルタ）
- delete_company_repository: 指定リポジトリを会社側の管理対象・一覧から除外
- inspect_archive: ZIP内の署名・SHA-256・メタデータおよび会社側リポジトリ対応状況を検証
- apply_archive: 各アプリへパッチを適用する
  1. 未コミット変更があれば `[myao-patch] アプリ名 update` として事前に自動コミット
  2. 連番の連続性を確認し、対象パスをバックアップ
  3. 同梱ファイル実体を直接上書き配置し、削除対象ファイルを消去
  4. `[myao-patch] アプリ名 patch-連番` として即時コミット
     （内容が既に一致していて差分が無い場合はコミットせず、適用済みとして連番だけ進める）
  5. 失敗時は適用前のコミットとバックアップから復元。リポジトリ単位で独立して処理する
- commit_pending: 未コミットの変更（一括または個別）をGitコミット
- init_repository_sequence / init_all_repositories_sequence: 次回適用開始番号を指定し、state.jsonを作成

適用状態は `<リポジトリ>/.git/rep-patch/state.json` に確定済み連番（confirmed_sequence）として保持する。
旧バージョンが残した「適用済み・未コミット」の連番（pending_sequences）は、次のコミット時に確定済みへ取り込む。
"""
from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import OBSIDIAN_REPO_ID, Settings, SettingsStore, repo_id_for
from .errors import PackageValidationError, RepPatchError
from .git import ensure_repository, repository_status, run_git
from .packages import ArchivePackage, PatchArchive, read_json, write_json
from .security import safe_relative_path, safe_repo_path

STATE_VERSION = 1
GIT_USER_FALLBACK = ["-c", "user.name=Myao Patch Bridge", "-c", "user.email=patch-bridge@local"]
_SCAN_WORKERS = 8


def list_download_packages(settings: Settings) -> list[dict[str, Any]]:
    root = Path(settings.download_dir).expanduser()
    if not root.exists():
        return []
    if not root.is_dir():
        raise RepPatchError(f"ダウンロードフォルダではありません: {root}")
    result = []
    for path in root.glob("myao_app_patch*.zip"):
        try:
            stat = path.stat()
        except OSError:
            continue
        result.append(
            {
                "path": str(path),
                "name": path.name,
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            }
        )
    return sorted(result, key=lambda item: item["modified_at"], reverse=True)


def _allowed_zip(settings: Settings, value: str) -> Path:
    root = Path(settings.download_dir).expanduser().resolve()
    path = Path(value).expanduser().resolve()
    if root not in [path, *path.parents]:
        raise PackageValidationError("ZIPは設定されたダウンロードフォルダ内から選択してください")
    if path.suffix.lower() != ".zip" or not path.is_file():
        raise PackageValidationError(f"ZIPが見つかりません: {path}")
    return path


def inspect_archive(settings: Settings, zip_path: str) -> dict[str, Any]:
    path = _allowed_zip(settings, zip_path)
    with PatchArchive(path, settings.patch_password) as archive:
        summary = archive.summary()
        for repository in summary["repositories"]:
            try:
                repository["company_path"] = str(resolve_company_repo(settings, repository))
            except RepPatchError as exc:
                repository["company_path"] = ""
                repository["mapping_error"] = str(exc)
        return summary


def _has_git_dir(path: Path) -> bool:
    return path.is_dir() and (path / ".git").exists()


def resolve_company_repo_by_id(settings: Settings, repo_id: str, display_name: str = "") -> tuple[str, Path]:
    explicit = settings.company_repo_paths.get(repo_id)
    if explicit:
        path = Path(explicit).expanduser()
        ensure_repository(path)
        return path.name, path
    if repo_id == OBSIDIAN_REPO_ID:
        if not settings.company_obsidian_repo:
            raise RepPatchError("会社側のObsidian設定リポジトリが未設定です")
        path = Path(settings.company_obsidian_repo).expanduser()
        ensure_repository(path)
        return path.name, path
    if settings.company_apps_root:
        root = Path(settings.company_apps_root).expanduser()
        if root.is_dir():
            if display_name:
                candidate = root / display_name
                if _has_git_dir(candidate):
                    return candidate.name, candidate
            for child in root.iterdir():
                if _has_git_dir(child):
                    if child.name.lower() == repo_id.lower() or repo_id_for(child.name) == repo_id:
                        return child.name, child
    raise RepPatchError(f"会社側リポジトリが見つかりません: {repo_id}")


def resolve_company_repo(settings: Settings, manifest: dict[str, Any]) -> Path:
    repo_id = str(manifest["repo_id"])
    display_name = str(manifest.get("display_name", ""))
    _, path = resolve_company_repo_by_id(settings, repo_id, display_name)
    return path


def _git_dir(repo: Path) -> Path:
    dot_git = repo / ".git"
    if dot_git.is_dir():
        return dot_git.resolve()
    # ワークツリーやサブモジュールでは .git がファイルなので、gitに実際の場所を問い合わせる
    value = run_git(repo, ["rev-parse", "--git-dir"]).text
    path = Path(value)
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def _state_path(repo: Path) -> Path:
    return _git_dir(repo) / "rep-patch" / "state.json"


def _new_state(repo_id: str, confirmed_sequence: int = 0) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "repo_id": repo_id,
        "confirmed_sequence": confirmed_sequence,
    }


def load_state(repo: Path, repo_id: str) -> dict[str, Any]:
    path = _state_path(repo)
    if not path.exists():
        return _new_state(repo_id)
    state = read_json(path)
    if state.get("version") != STATE_VERSION or state.get("repo_id") != repo_id:
        raise RepPatchError(f"適用状態ファイルが不正です: {path}")
    try:
        state["confirmed_sequence"] = int(state.get("confirmed_sequence", 0))
        [int(item) for item in state.get("pending_sequences", [])]
    except (TypeError, ValueError) as exc:
        raise RepPatchError(f"適用状態ファイルが不正です: {path}") from exc
    return state


def save_state(repo: Path, state: dict[str, Any]) -> None:
    write_json(_state_path(repo), state)


def _legacy_pending(state: dict[str, Any]) -> list[int]:
    """旧バージョンが残した「適用済み・未コミット」のパッチ連番"""
    return [int(item) for item in state.get("pending_sequences", [])]


def _applied_sequence(state: dict[str, Any]) -> int:
    """作業ツリーへ反映済みの最大連番（確定済みと、旧バージョンの未コミット適用分の大きい方）"""
    return max([int(state["confirmed_sequence"]), *_legacy_pending(state)])


def _save_confirmed(repo: Path, repo_id: str, confirmed_sequence: int) -> dict[str, Any]:
    state = _new_state(repo_id, confirmed_sequence)
    state["confirmed_at"] = datetime.now(timezone.utc).isoformat()
    save_state(repo, state)
    return state


def _sequence_label(first: int, last: int) -> str:
    return f"{first:06d}" if first == last else f"{first:06d}-{last:06d}"


def _backup_paths(repo: Path, paths: list[str]) -> Path:
    backup_root = _git_dir(repo) / "rep-patch" / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    session = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = backup_root / session
    files_dir = target / "files"
    records: list[dict[str, Any]] = []
    try:
        for relative in sorted(set(paths)):
            source = safe_repo_path(repo, relative)
            if source.is_symlink():
                raise RepPatchError(f"シンボリックリンクはパッチ対象にできません: {relative}")
            exists = source.exists()
            records.append({"path": relative, "existed": exists})
            if exists:
                if not source.is_file():
                    raise RepPatchError(f"通常ファイルではありません: {relative}")
                destination = files_dir.joinpath(*safe_relative_path(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        write_json(target / "backup.json", {"paths": records})
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _restore_backup(repo: Path, backup: Path, pre_apply_head: str) -> None:
    """適用前のコミットへ戻し、Git管理外のファイルも含めて対象パスを適用前の内容へ復元する"""
    run_git(repo, ["reset", "--hard", pre_apply_head])
    metadata = read_json(backup / "backup.json")
    for record in metadata["paths"]:
        relative = record["path"]
        destination = safe_repo_path(repo, relative)
        if record["existed"]:
            source = (backup / "files").joinpath(*safe_relative_path(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        else:
            destination.unlink(missing_ok=True)


def _remove_backup(value: str) -> None:
    if not value:
        return
    path = Path(value)
    if path.is_dir() and path.name and path.parent.name == "backups":
        shutil.rmtree(path, ignore_errors=True)


def _ensure_clean(repo: Path) -> None:
    status = repository_status(repo)
    if not status["clean"]:
        raise RepPatchError("未コミットの変更があります")


def _commit_staged(repo: Path, message: str) -> bool:
    """ステージ済みの変更をコミットする。変更が無ければ何もせず False を返す。"""
    if run_git(repo, ["diff", "--cached", "--quiet"], check=False).returncode == 0:
        return False
    run_git(repo, [*GIT_USER_FALLBACK, "commit", "-m", message])
    return True


def _commit_worktree(repo: Path, state: dict[str, Any], display_name: str) -> dict[str, Any]:
    """未コミットの変更をコミットして作業ツリーをクリーンにする。"""
    pending = _legacy_pending(state)
    if not pending and repository_status(repo)["clean"]:
        return state
    run_git(repo, ["add", "-A"])
    if pending:
        message = f"[myao-patch] {display_name} patch-{_sequence_label(pending[0], pending[-1])}"
    else:
        message = f"[myao-patch] {display_name} update"
    _commit_staged(repo, message)
    _ensure_clean(repo)
    _remove_backup(str(state.get("backup_dir", "")))
    return _save_confirmed(repo, str(state["repo_id"]), _applied_sequence(state))


def _check_sequence(packages: list[ArchivePackage], expected: int) -> None:
    for package in packages:
        if package.sequence != expected:
            raise RepPatchError(
                f"パッチ連番が欠けています。必要: {expected:06d}、検出: {package.sequence:06d}"
            )
        expected += 1


def _affected_paths(packages: list[ArchivePackage]) -> list[str]:
    paths: list[str] = []
    for package in packages:
        manifest = package.manifest
        paths.extend(manifest.get("changed_paths", []))
        paths.extend(record["path"] for record in manifest.get("added_files") or [])
        paths.extend(manifest.get("deleted_files", []))
    return paths


def _apply_packages(
    archive: PatchArchive,
    repo: Path,
    state: dict[str, Any],
    packages: list[ArchivePackage],
) -> tuple[dict[str, Any], bool]:
    """クリーンな作業ツリーへパッチを順に適用してコミットする。(新しい状態, コミットしたか) を返す。"""
    _ensure_clean(repo)
    _check_sequence(packages, int(state["confirmed_sequence"]) + 1)
    first, last = packages[0].sequence, packages[-1].sequence
    message = f"[myao-patch] {packages[-1].display_name} patch-{_sequence_label(first, last)}"

    pre_apply_head = run_git(repo, ["rev-parse", "HEAD"]).text
    backup = _backup_paths(repo, _affected_paths(packages))
    try:
        for package in packages:
            # 変更・新規追加ファイルの実体を直接上書き配置
            for record in package.manifest.get("added_files") or []:
                target_file = safe_repo_path(repo, record["path"])
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_bytes(archive.read_added_file(package, record["path"]))
            # 削除対象ファイルを消去
            for relative in package.manifest.get("deleted_files", []):
                safe_repo_path(repo, relative).unlink(missing_ok=True)

        run_git(repo, ["add", "-A"])
        committed = _commit_staged(repo, message)
        _ensure_clean(repo)
    except Exception:
        _restore_backup(repo, backup, pre_apply_head)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)

    return _save_confirmed(repo, str(state["repo_id"]), last), committed


def _result(repo_id: str, display_name: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "repo_id": repo_id,
        "display_name": display_name,
        "status": status,
        "message": message,
        **extra,
    }


def _apply_repository(
    settings: Settings, archive: PatchArchive, repo_id: str, packages: list[ArchivePackage]
) -> dict[str, Any]:
    latest = packages[-1]
    display_name = latest.display_name
    repo = resolve_company_repo(settings, latest.manifest)
    state = load_state(repo, repo_id)
    new_packages = [item for item in packages if item.sequence > _applied_sequence(state)]
    if not new_packages:
        return _result(repo_id, display_name, "unchanged", "新しいパッチはありません")
    # 未コミットの変更（手動編集や旧バージョンの適用残り）を先にコミットしてクリーンにする
    state = _commit_worktree(repo, state, display_name)
    state, committed = _apply_packages(archive, repo, state, new_packages)
    message = (
        "パッチを適用し、最新コミットとして反映しました。"
        if committed
        else "内容は既に一致していたため、コミットは作らず適用済みとして記録しました。"
    )
    return _result(
        repo_id, display_name, "applied", message, confirmed_sequence=state["confirmed_sequence"]
    )


def apply_archive(
    settings: Settings,
    zip_path: str,
    *,
    only_repo_id: str | None = None,
) -> dict[str, Any]:
    path = _allowed_zip(settings, zip_path)
    results: list[dict[str, Any]] = []
    with PatchArchive(path, settings.patch_password) as archive:
        groups = archive.package_groups()
        if only_repo_id and only_repo_id not in groups:
            results.append(
                _result(only_repo_id, only_repo_id, "unchanged", "このZIPには対象アプリのパッチが含まれていません")
            )
        for repo_id, packages in groups.items():
            if only_repo_id and repo_id != only_repo_id:
                continue
            display_name = packages[-1].display_name
            if not only_repo_id and repo_id in settings.company_excluded_repo_ids:
                results.append(
                    _result(repo_id, display_name, "unchanged", "会社側の管理対象外（除外設定中）のためスキップしました")
                )
                continue
            try:
                results.append(_apply_repository(settings, archive, repo_id, packages))
            except Exception as exc:  # noqa: BLE001 - isolate failures per repository
                results.append(_result(repo_id, display_name, "failed", str(exc)))
    failed = sum(item["status"] == "failed" for item in results)
    return {"results": results, "failed": failed, "succeeded": len(results) - failed}


def _company_candidates(settings: Settings) -> dict[str, tuple[str, Path, str]]:
    """会社側で管理対象になり得るリポジトリ（repo_id → 表示名・パス・種類）"""
    candidates: dict[str, tuple[str, Path, str]] = {}
    if settings.company_apps_root:
        root = Path(settings.company_apps_root).expanduser()
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if _has_git_dir(child):
                    candidates[repo_id_for(child.name)] = (child.name, child, "app")
    if settings.company_obsidian_repo:
        obsidian = Path(settings.company_obsidian_repo).expanduser()
        if _has_git_dir(obsidian):
            candidates[OBSIDIAN_REPO_ID] = (obsidian.name, obsidian, "obsidian")
    for repo_id, value in settings.company_repo_paths.items():
        path = Path(value).expanduser()
        if _has_git_dir(path):
            kind = "obsidian" if "obsidian" in path.name.lower() else "app"
            candidates[repo_id] = (path.name, path, kind)
    return candidates


def _describe_repository(repo_id: str, display_name: str, path: Path, kind: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "repo_id": repo_id,
        "display_name": display_name,
        "path": str(path),
        "kind": kind,
        "clean": False,
        "changes": 0,
        "head": "",
        "branch": "",
        "confirmed_sequence": 0,
        "error": "",
    }
    try:
        status = repository_status(path)
        state = load_state(path, repo_id)
        item.update(
            {
                "clean": status["clean"],
                "changes": status["changes"],
                "head": status["head"],
                "branch": status["branch"],
                "confirmed_sequence": int(state["confirmed_sequence"]),
            }
        )
    except Exception as exc:  # noqa: BLE001 - report the error on the repository card
        item["error"] = str(exc)
    return item


def list_company_repositories(settings: Settings) -> list[dict[str, Any]]:
    excluded = set(settings.company_excluded_repo_ids)
    targets = [
        (repo_id, display_name, path, kind)
        for repo_id, (display_name, path, kind) in sorted(
            _company_candidates(settings).items(), key=lambda item: item[1][0].lower()
        )
        if repo_id not in excluded
    ]
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(_SCAN_WORKERS, len(targets))) as executor:
        return list(executor.map(lambda target: _describe_repository(*target), targets))


def delete_company_repository(
    settings: Settings, store: SettingsStore, repo_id: str
) -> dict[str, Any]:
    """
    指定されたリポジトリを会社側の管理対象・一覧から除外する
    ※ローカルのGitリポジトリ実体やファイルは削除しない
    """
    if repo_id not in settings.company_excluded_repo_ids:
        settings.company_excluded_repo_ids.append(repo_id)
    if repo_id in settings.company_repo_paths:
        del settings.company_repo_paths[repo_id]
    store.save(settings)
    return {"deleted": True, "repo_id": repo_id}


def commit_pending(settings: Settings, repo_id: str | None = None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    candidates: list[tuple[str, str, Path]] = []
    if repo_id:
        display_name, path = resolve_company_repo_by_id(settings, repo_id)
        candidates.append((repo_id, display_name, path))
    else:
        for item in list_company_repositories(settings):
            if not item.get("error"):
                candidates.append((item["repo_id"], item["display_name"], Path(item["path"])))

    for candidate_id, display_name, repo in candidates:
        try:
            state = load_state(repo, candidate_id)
            if not _legacy_pending(state) and repository_status(repo)["clean"]:
                if repo_id:
                    results.append(_result(candidate_id, display_name, "unchanged", "コミットする変更はありません"))
                continue
            _commit_worktree(repo, state, display_name)
            results.append(_result(candidate_id, display_name, "committed", "未コミットの変更をコミットしました"))
        except Exception as exc:  # noqa: BLE001 - continue committing other repositories
            results.append(_result(candidate_id, display_name, "failed", str(exc)))
    return {"results": results}


def _require_start_sequence(start_sequence: int) -> None:
    if start_sequence < 1:
        raise RepPatchError("適用開始番号は1以上の整数を指定してください")


def init_repository_sequence(
    settings: Settings,
    repo_id: str,
    start_sequence: int,
) -> dict[str, Any]:
    """
    指定リポジトリの次回適用開始連番を設定し、state.jsonを即時作成・初期化する

    start_sequence: 次に適用したいパッチ連番（1以上）。
    内部的には直前まで確定した扱いとして confirmed_sequence = start_sequence - 1 を記録する。
    """
    _require_start_sequence(start_sequence)
    display_name, repo = resolve_company_repo_by_id(settings, repo_id)
    confirmed_sequence = start_sequence - 1
    state = _new_state(repo_id, confirmed_sequence)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_state(repo, state)
    return {
        "repo_id": repo_id,
        "display_name": display_name,
        "start_sequence": start_sequence,
        "confirmed_sequence": confirmed_sequence,
    }


def init_all_repositories_sequence(
    settings: Settings,
    start_sequence: int,
) -> list[dict[str, Any]]:
    """
    会社側の全検出リポジトリに対して一括で次回適用開始連番を設定し、state.jsonを作成する
    """
    _require_start_sequence(start_sequence)
    results: list[dict[str, Any]] = []
    for item in list_company_repositories(settings):
        if not item.get("error"):
            results.append(init_repository_sequence(settings, item["repo_id"], start_sequence))
    return results
