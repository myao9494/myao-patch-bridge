"""
会社環境でのパッチZIP検証・適用・バックアップ・コミット処理を行うモジュール

仕様:
- list_download_packages: ダウンロードフォルダからパッチZIP一覧を取得
- list_company_repositories: 会社側で登録・検出されたリポジトリ一覧および未コミット・保留状態を取得（除外リポジトリをフィルタ）
- delete_company_repository: 指定リポジトリを会社側の管理対象・一覧から除外
- inspect_archive: ZIP内の署名・SHA-256・メタデータおよび会社側リポジトリ対応状況を検証
- apply_archive: 各アプリへパッチを適用（変更・新規ファイル直接上書き配置・削除ファイル消去・事前自動コミット・即時コミット確定・安全なバックアップ保持）
- commit_pending: 動作確認済みの保留中パッチ（一括または個別）をGitコミット
- init_repository_sequence: 単一リポジトリの次回適用開始番号を指定し、state.jsonを即時作成・初期化
- init_all_repositories_sequence: 会社側全リポジトリの次回適用開始番号を一括初期化
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings, SettingsStore
from .errors import PackageValidationError, RepPatchError
from .git import ensure_repository, repository_status, run_git
from .home import repo_id_for
from .packages import ArchivePackage, PatchArchive, read_json, write_json
from .security import safe_repo_path

STATE_VERSION = 1
GIT_USER_FALLBACK = ["-c", "user.name=Myao Patch Bridge", "-c", "user.email=patch-bridge@local"]


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


def resolve_company_repo_by_id(settings: Settings, repo_id: str, display_name: str = "") -> tuple[str, Path]:
    explicit = settings.company_repo_paths.get(repo_id)
    if explicit:
        path = Path(explicit).expanduser()
        ensure_repository(path)
        return path.name, path
    if repo_id == "obsidian-settings":
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
                if candidate.is_dir() and (candidate / ".git").exists():
                    return candidate.name, candidate
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    if child.name.lower() == repo_id.lower() or repo_id_for(child.name) == repo_id:
                        return child.name, child
    raise RepPatchError(f"会社側リポジトリが見つかりません: {repo_id}")


def resolve_company_repo(settings: Settings, manifest: dict[str, Any]) -> Path:
    repo_id = str(manifest["repo_id"])
    display_name = str(manifest.get("display_name", ""))
    _, path = resolve_company_repo_by_id(settings, repo_id, display_name)
    return path


def _git_dir(repo: Path) -> Path:
    value = run_git(repo, ["rev-parse", "--git-dir"]).text
    path = Path(value)
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def _state_path(repo: Path) -> Path:
    return _git_dir(repo) / "rep-patch" / "state.json"


def load_state(repo: Path, repo_id: str) -> dict[str, Any]:
    path = _state_path(repo)
    if not path.exists():
        return {
            "version": STATE_VERSION,
            "repo_id": repo_id,
            "confirmed_sequence": 0,
            "pending_sequences": [],
            "pending_target_files": [],
            "pending_changed_paths": [],
            "pre_apply_head": "",
            "backup_dir": "",
        }
    state = read_json(path)
    if state.get("version") != STATE_VERSION or state.get("repo_id") != repo_id:
        raise RepPatchError(f"適用状態ファイルが不正です: {path}")
    return state


def save_state(repo: Path, state: dict[str, Any]) -> None:
    write_json(_state_path(repo), state)



def _backup_paths(repo: Path, paths: list[str]) -> Path:
    backup_root = _git_dir(repo) / "rep-patch" / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    session = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = backup_root / session
    files_dir = target / "files"
    records: list[dict[str, Any]] = []
    for relative in sorted(set(paths)):
        source = safe_repo_path(repo, relative)
        if source.is_symlink():
            raise RepPatchError(f"シンボリックリンクはパッチ対象にできません: {relative}")
        exists = source.exists()
        records.append({"path": relative, "existed": exists})
        if exists:
            if not source.is_file():
                raise RepPatchError(f"通常ファイルではありません: {relative}")
            destination = safe_repo_path(files_dir, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    write_json(target / "backup.json", {"paths": records})
    return target


def _restore_backup(repo: Path, backup: Path) -> None:
    metadata = read_json(backup / "backup.json")
    run_git(repo, ["reset", "--hard", "HEAD"])
    for record in metadata["paths"]:
        relative = record["path"]
        destination = safe_repo_path(repo, relative)
        if record["existed"]:
            source = safe_repo_path(backup / "files", relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        else:
            destination.unlink(missing_ok=True)
    run_git(repo, ["reset", "--mixed", "HEAD"])


def _remove_backup(value: str) -> None:
    if not value:
        return
    path = Path(value)
    if path.is_dir() and path.name and path.parent.name == "backups":
        shutil.rmtree(path)


def _ensure_clean(repo: Path) -> None:
    status = repository_status(repo)
    if not status["clean"]:
        raise RepPatchError("未コミットの変更があります")


def _commit_pending(repo: Path, state: dict[str, Any], display_name: str) -> dict[str, Any]:
    pending = state.get("pending_sequences", [])
    status = repository_status(repo)
    if not pending and status["clean"]:
        return state
    run_git(repo, ["add", "-A"])
    if pending:
        first, last = pending[0], pending[-1]
        suffix = f"{first:06d}" if first == last else f"{first:06d}-{last:06d}"
        msg = f"[myao-patch] {display_name} patch-{suffix}"
        confirmed_seq = last
    else:
        msg = f"[myao-patch] {display_name} update"
        confirmed_seq = state.get("confirmed_sequence", 0)

    run_git(repo, [*GIT_USER_FALLBACK, "commit", "-m", msg])
    _ensure_clean(repo)
    _remove_backup(state.get("backup_dir", ""))
    state.update(
        {
            "confirmed_sequence": confirmed_seq,
            "pending_sequences": [],
            "pending_target_files": [],
            "pending_changed_paths": [],
            "pre_apply_head": "",
            "backup_dir": "",
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    save_state(repo, state)
    return state


def _check_sequence(packages: list[ArchivePackage], expected: int) -> None:
    for package in packages:
        sequence = int(package.manifest["sequence"])
        if sequence != expected:
            raise RepPatchError(f"パッチ連番が欠けています。必要: {expected:06d}、検出: {sequence:06d}")
        expected += 1


def _apply_packages(
    archive: PatchArchive,
    repo: Path,
    state: dict[str, Any],
    packages: list[ArchivePackage],
    *,
    correction: bool,
) -> dict[str, Any]:
    if not packages:
        return state
    pending_before = list(state.get("pending_sequences", []))
    if pending_before:
        if not correction:
            raise RepPatchError("前回パッチが未コミットです")
        run_git(repo, ["add", "-A"])
    else:
        _ensure_clean(repo)

    expected = (pending_before[-1] if pending_before else int(state["confirmed_sequence"])) + 1
    _check_sequence(packages, expected)
    changed = list(state.get("pending_changed_paths", []))
    for package in packages:
        changed.extend(package.manifest.get("changed_paths", []))
        for item in package.manifest.get("added_files", []):
            if isinstance(item, dict) and "path" in item:
                changed.append(item["path"])
        changed.extend(package.manifest.get("deleted_files", []))
    session_backup = _backup_paths(repo, changed)
    original_backup = state.get("backup_dir", "")
    try:
        for package in packages:
            # 変更・新規追加ファイルの実体を直接上書き配置
            for record in package.manifest.get("added_files", []):
                file_rel = record["path"]
                content = archive.read_added_file(package, file_rel)
                target_file = safe_repo_path(repo, file_rel)
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_bytes(content)

            # 削除対象ファイルを確実に消去
            for del_rel in package.manifest.get("deleted_files", []):
                del_target = safe_repo_path(repo, del_rel)
                del_target.unlink(missing_ok=True)

        # ワークツリーの全変更をインデックスへ反映
        run_git(repo, ["add", "-A"])

        # パッチ適用と同時にGitコミットを実行し、クリーンな最新状態にする
        first_seq = int(packages[0].manifest["sequence"])
        last_seq = int(packages[-1].manifest["sequence"])
        suffix = f"{first_seq:06d}" if first_seq == last_seq else f"{first_seq:06d}-{last_seq:06d}"
        disp_name = packages[-1].manifest.get("display_name") or repo.name
        msg = f"[myao-patch] {disp_name} patch-{suffix}"
        run_git(repo, [*GIT_USER_FALLBACK, "commit", "-m", msg])
        _ensure_clean(repo)
        _remove_backup(str(session_backup))
        if original_backup:
            _remove_backup(original_backup)
    except Exception:
        _restore_backup(repo, session_backup)
        shutil.rmtree(session_backup, ignore_errors=True)
        raise

    state.update(
        {
            "confirmed_sequence": last_seq,
            "pending_sequences": [],
            "pending_target_files": [],
            "pending_changed_paths": [],
            "pre_apply_head": "",
            "backup_dir": "",
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    save_state(repo, state)
    return state


def _result(repo_id: str, display_name: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "repo_id": repo_id,
        "display_name": display_name,
        "status": status,
        "message": message,
        **extra,
    }


def apply_archive(
    settings: Settings,
    zip_path: str,
    *,
    only_repo_id: str | None = None,
    correction: bool = False,
) -> dict[str, Any]:
    path = _allowed_zip(settings, zip_path)
    results: list[dict[str, Any]] = []
    with PatchArchive(path, settings.patch_password) as archive:
        for repo_id, all_packages in archive.package_groups().items():
            if only_repo_id and repo_id != only_repo_id:
                continue
            latest_manifest = all_packages[-1].manifest
            display_name = latest_manifest["display_name"]
            if not only_repo_id and repo_id in settings.company_excluded_repo_ids:
                results.append(_result(repo_id, display_name, "unchanged", "会社側の管理対象外（除外設定中）のためスキップしました"))
                continue
            try:
                repo = resolve_company_repo(settings, latest_manifest)
                state = load_state(repo, repo_id)
                last_pending = state["pending_sequences"][-1] if state["pending_sequences"] else 0
                current = max(int(state["confirmed_sequence"]), int(last_pending))
                new_packages = [item for item in all_packages if int(item.manifest["sequence"]) > current]
                if not new_packages:
                    results.append(_result(repo_id, display_name, "unchanged", "新しいパッチはありません"))
                    continue
                # 未コミットの変更がある場合、または保留中パッチがある場合は事前にコミットしてクリーンにする
                repo_stat = repository_status(repo)
                if (state.get("pending_sequences") or not repo_stat["clean"]) and not correction:
                    state = _commit_pending(repo, state, display_name)
                state = _apply_packages(
                    archive, repo, state, new_packages, correction=correction
                )
                results.append(
                    _result(
                        repo_id,
                        display_name,
                        "applied",
                        "パッチを適用し、最新コミットとして反映しました。",
                        confirmed_sequence=state.get("confirmed_sequence", 0),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - isolate failures per repository
                results.append(_result(repo_id, display_name, "failed", str(exc)))
    failed = sum(item["status"] == "failed" for item in results)
    return {"results": results, "failed": failed, "succeeded": len(results) - failed}


def list_company_repositories(settings: Settings) -> list[dict[str, Any]]:
    candidates: dict[str, tuple[str, Path, str]] = {}
    if settings.company_apps_root:
        root = Path(settings.company_apps_root).expanduser()
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / ".git").exists():
                    r_id = repo_id_for(child.name)
                    candidates[r_id] = (child.name, child, "app")
    if settings.company_obsidian_repo:
        obsidian = Path(settings.company_obsidian_repo).expanduser()
        if obsidian.is_dir() and (obsidian / ".git").exists():
            candidates["obsidian-settings"] = (obsidian.name, obsidian, "obsidian")
    for r_id, p_str in settings.company_repo_paths.items():
        p = Path(p_str).expanduser()
        if p.is_dir() and (p / ".git").exists():
            kind = "obsidian" if "obsidian" in p.name.lower() else "app"
            candidates[r_id] = (p.name, p, kind)

    results: list[dict[str, Any]] = []
    excluded = set(settings.company_excluded_repo_ids)
    for repo_id, (display_name, path, kind) in sorted(candidates.items(), key=lambda item: item[1][0].lower()):
        if repo_id in excluded:
            continue
        try:
            status = repository_status(path)
            state = load_state(path, repo_id)
            results.append({
                "repo_id": repo_id,
                "display_name": display_name,
                "path": str(path),
                "kind": kind,
                "clean": bool(status["clean"]),
                "changes": int(status["changes"]),
                "head": str(status["head"]),
                "branch": str(status["branch"]),
                "confirmed_sequence": int(state.get("confirmed_sequence", 0)),
                "pending_sequences": list(state.get("pending_sequences", [])),
                "error": "",
            })
        except Exception as exc:  # noqa: BLE001
            results.append({
                "repo_id": repo_id,
                "display_name": display_name,
                "path": str(path),
                "kind": kind,
                "clean": False,
                "changes": 0,
                "head": "",
                "branch": "",
                "confirmed_sequence": 0,
                "pending_sequences": [],
                "error": str(exc),
            })
    return results


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
        repo_list = list_company_repositories(settings)
        for item in repo_list:
            if not item.get("error"):
                candidates.append((item["repo_id"], item["display_name"], Path(item["path"])))

    for candidate_id, display_name, repo in candidates:
        try:
            state = load_state(repo, candidate_id)
            status = repository_status(repo)
            if not state.get("pending_sequences") and status["clean"]:
                if repo_id:
                    results.append(_result(candidate_id, display_name, "unchanged", "コミットする変更はありません"))
                continue
            _commit_pending(repo, state, display_name)
            results.append(_result(candidate_id, display_name, "committed", "確認済みパッチをコミットしました"))
        except Exception as exc:  # noqa: BLE001 - continue committing other repositories
            results.append(_result(candidate_id, display_name, "failed", str(exc)))
    return {"results": results}


def company_status(settings: Settings, archive: PatchArchive | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if archive:
        manifests = [packages[-1].manifest for packages in archive.package_groups().values()]
    else:
        manifests = []
    for manifest in manifests:
        repo_id = manifest["repo_id"]
        try:
            repo = resolve_company_repo(settings, manifest)
            state = load_state(repo, repo_id)
            status = repository_status(repo)
            result.append({**manifest, **status, "state": state, "company_path": str(repo), "error": ""})
        except Exception as exc:  # noqa: BLE001 - return status for every repository
            result.append({**manifest, "state": {}, "company_path": "", "error": str(exc)})
    return result


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
    if start_sequence < 1:
        raise RepPatchError("適用開始番号は1以上の整数を指定してください")

    display_name, repo = resolve_company_repo_by_id(settings, repo_id)
    confirmed_seq = start_sequence - 1

    # 既存のstate.jsonがあれば読み込み、なければ初期構造を作成
    try:
        state = load_state(repo, repo_id)
    except RepPatchError:
        state = {
            "version": STATE_VERSION,
            "repo_id": repo_id,
        }

    state.update(
        {
            "version": STATE_VERSION,
            "repo_id": repo_id,
            "confirmed_sequence": confirmed_seq,
            "pending_sequences": [],
            "pending_target_files": [],
            "pending_changed_paths": [],
            "pre_apply_head": "",
            "backup_dir": "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    save_state(repo, state)

    return {
        "repo_id": repo_id,
        "display_name": display_name,
        "start_sequence": start_sequence,
        "confirmed_sequence": confirmed_seq,
    }


def init_all_repositories_sequence(
    settings: Settings,
    start_sequence: int,
) -> list[dict[str, Any]]:
    """
    会社側の全検出リポジトリに対して一括で次回適用開始連番を設定し、state.jsonを作成する
    """
    if start_sequence < 1:
        raise RepPatchError("適用開始番号は1以上の整数を指定してください")

    repo_list = list_company_repositories(settings)
    results: list[dict[str, Any]] = []
    for item in repo_list:
        if not item.get("error"):
            res = init_repository_sequence(settings, item["repo_id"], start_sequence)
            results.append(res)
    return results



