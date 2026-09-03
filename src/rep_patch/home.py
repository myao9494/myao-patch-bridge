"""
自宅環境でのリポジトリ検出・登録・管理およびパッチ公開処理を行うモジュール

仕様:
- discover_repositories: apps_rootおよびobsidian_repoからGitリポジトリを自動検出し設定に登録
- add_repository: 指定されたGitリポジトリのパスや設定を手動で登録
- delete_repository: 登録済みリポジトリを設定から削除
- update_repository: リポジトリのブランチや初期導入地点、有効無効状態を更新
- scan_repositories: 登録済みリポジトリの未公開コミット数やクリーン状態を取得
- publish: 登録済みで有効なリポジトリの差分パッチ作成・新規ファイル実体同梱・削除記録・署名・分割し、パッチリポジトリへpush
- reset_repository_patches: 指定リポジトリのパッチ専用リポジトリ内パッケージ削除・インデックス再署名・pushおよび公開済みコミットの初期化
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, RepositoryConfig, Settings, SettingsStore
from .errors import RepPatchError
from .git import (
    changed_paths,
    diff_file_status,
    ensure_repository,
    is_ancestor,
    repository_status,
    resolve_commit,
    run_git,
    tracked_files,
)
from .packages import SCHEMA_VERSION, load_index, split_patch, write_json
from .security import sha256_bytes, sign_document


def repo_id_for(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-.").lower()
    if not slug:
        raise RepPatchError(f"リポジトリIDを作成できません: {name}")
    return slug


def discover_repositories(settings: Settings, store: SettingsStore) -> list[dict[str, Any]]:
    root = Path(settings.apps_root).expanduser()
    if not root.is_dir():
        raise RepPatchError(f"アプリルートが見つかりません: {root}")
    existing = settings.repositories
    discovered: dict[str, RepositoryConfig] = dict(existing)
    candidates = [item for item in root.iterdir() if item.is_dir()]
    if settings.obsidian_repo:
        candidates.append(Path(settings.obsidian_repo).expanduser())
    for path in candidates:
        if path.resolve() == PROJECT_ROOT.resolve():
            continue
        result = run_git(path, ["rev-parse", "--is-inside-work-tree"], check=False)
        if result.returncode != 0 or result.text != "true":
            continue
        kind = "obsidian" if settings.obsidian_repo and path.resolve() == Path(settings.obsidian_repo).expanduser().resolve() else "app"
        repo_id = "obsidian-settings" if kind == "obsidian" else repo_id_for(path.name)
        if repo_id not in discovered:
            branch = run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"]).text
            discovered[repo_id] = RepositoryConfig(
                repo_id=repo_id,
                display_name=path.name,
                path=str(path),
                kind=kind,
                branch=branch,
            )
        else:
            discovered[repo_id].path = str(path)
    settings.repositories = discovered
    store.save(settings)
    return scan_repositories(settings)


def scan_repositories(settings: Settings) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for config in sorted(settings.repositories.values(), key=lambda item: item.display_name.lower()):
        item: dict[str, Any] = asdict(config)
        try:
            status = repository_status(Path(config.path))
            target = resolve_commit(Path(config.path), config.branch)
            base = config.published_commit or config.baseline_commit
            count = 0
            if base:
                count_text = run_git(Path(config.path), ["rev-list", "--count", f"{base}..{target}"]).text
                count = int(count_text)
            item.update(status)
            item.update({"target_commit": target, "unpublished_commits": count, "error": ""})
        except (RepPatchError, ValueError) as exc:
            item.update({"clean": False, "error": str(exc), "unpublished_commits": 0})
        result.append(item)
    return result


def add_repository(
    settings: Settings, store: SettingsStore, payload: dict[str, Any]
) -> dict[str, Any]:
    raw_path = payload.get("path", "").strip()
    if not raw_path:
        raise RepPatchError("リポジトリのパスを指定してください")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir():
        raise RepPatchError(f"Gitリポジトリが見つかりません: {raw_path}")
    result = run_git(path, ["rev-parse", "--is-inside-work-tree"], check=False)
    if result.returncode != 0 or result.text != "true":
        raise RepPatchError(f"指定されたディレクトリはGitリポジトリではありません: {raw_path}")

    kind = payload.get("kind") or ("obsidian" if "obsidian" in path.name.lower() else "app")
    display_name = payload.get("display_name", "").strip() or path.name
    repo_id = payload.get("repo_id", "").strip() or (
        "obsidian-settings" if kind == "obsidian" and path.name.lower().startswith("obsidian") else repo_id_for(display_name)
    )

    branch = payload.get("branch", "").strip()
    if not branch:
        branch_res = run_git(path, ["symbolic-ref", "--short", "HEAD"], check=False)
        if branch_res.returncode == 0 and branch_res.text:
            branch = branch_res.text
        else:
            abbrev_res = run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
            branch = abbrev_res.text if abbrev_res.returncode == 0 and abbrev_res.text and abbrev_res.text != "HEAD" else "main"

    baseline_commit = payload.get("baseline_commit", "").strip()
    if baseline_commit:
        baseline_commit = resolve_commit(path, baseline_commit)

    enabled = bool(payload.get("enabled", True))

    config = RepositoryConfig(
        repo_id=repo_id,
        display_name=display_name,
        path=str(path),
        kind=kind,
        enabled=enabled,
        branch=branch,
        baseline_commit=baseline_commit,
    )
    settings.repositories[repo_id] = config
    store.save(settings)
    return asdict(config)


def delete_repository(
    settings: Settings, store: SettingsStore, repo_id: str
) -> dict[str, Any]:
    if repo_id not in settings.repositories:
        raise RepPatchError(f"リポジトリが登録されていません: {repo_id}")
    del settings.repositories[repo_id]
    store.save(settings)
    return {"deleted": True, "repo_id": repo_id}


def update_repository(
    settings: Settings, store: SettingsStore, repo_id: str, updates: dict[str, Any]
) -> dict[str, Any]:
    if repo_id not in settings.repositories:
        raise RepPatchError(f"リポジトリが登録されていません: {repo_id}")
    config = settings.repositories[repo_id]
    for key in ["display_name", "path", "kind", "enabled", "branch", "baseline_commit"]:
        if key in updates:
            setattr(config, key, updates[key])
    if config.baseline_commit:
        config.baseline_commit = resolve_commit(Path(config.path), config.baseline_commit)
    settings.repositories[repo_id] = config
    store.save(settings)
    return asdict(config)


def _reconcile_published(settings: Settings, store: SettingsStore, index: dict[str, Any], patch_root: Path) -> None:
    latest: dict[str, dict[str, Any]] = {}
    for item in index["packages"]:
        manifest_path = patch_root / item["manifest_path"]
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        repo_id = manifest["repo_id"]
        if repo_id not in latest or manifest["sequence"] > latest[repo_id]["sequence"]:
            latest[repo_id] = manifest
    changed = False
    for repo_id, manifest in latest.items():
        config = settings.repositories.get(repo_id)
        if config and config.published_commit != manifest["source_to_commit"]:
            config.published_commit = manifest["source_to_commit"]
            changed = True
    if changed:
        store.save(settings)


def _push_pending(patch_root: Path) -> None:
    upstream = run_git(patch_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], check=False)
    if upstream.returncode != 0:
        return
    ahead = int(run_git(patch_root, ["rev-list", "--count", "@{u}..HEAD"]).text or "0")
    if ahead:
        run_git(patch_root, ["push"])


def publish(settings: Settings, store: SettingsStore) -> dict[str, Any]:
    if settings.mode != "home":
        raise RepPatchError("パッチの公開は自宅モードでのみ実行できます")
    if not settings.patch_password:
        raise RepPatchError("パッチ検証用パスワードを設定してください")
    patch_root = Path(settings.patch_repo).expanduser()
    ensure_repository(patch_root)
    if run_git(patch_root, ["status", "--porcelain=v1"]).text:
        raise RepPatchError("パッチ専用リポジトリに未コミットの変更があります")
    _push_pending(patch_root)
    attributes_path = patch_root / ".gitattributes"
    binary_rule = "*.patch.part-* binary"
    if attributes_path.exists():
        attributes = attributes_path.read_text(encoding="utf-8")
        if binary_rule not in attributes.splitlines():
            attributes_path.write_text(
                attributes.rstrip("\n") + f"\n{binary_rule}\n", encoding="utf-8"
            )
    else:
        attributes_path.write_text(
            "# Managed by Myao Patch Bridge\n"
            f"{binary_rule}\n"
            "*.json text eol=lf\n",
            encoding="utf-8",
        )
    index_path = patch_root / "package-index.json"
    index = load_index(index_path, settings.patch_password)
    _reconcile_published(settings, store, index, patch_root)
    latest_sequence: dict[str, int] = {}
    for item in index["packages"]:
        repo_id = item["repo_id"]
        latest_sequence[repo_id] = max(latest_sequence.get(repo_id, 0), int(item["sequence"]))

    created: list[dict[str, Any]] = []
    timestamp = datetime.now(timezone.utc).isoformat()
    for config in settings.repositories.values():
        if not config.enabled:
            continue
        repo = Path(config.path)
        ensure_repository(repo)
        if not config.baseline_commit:
            raise RepPatchError(f"初期導入コミットが未設定です: {config.display_name}")
        target = resolve_commit(repo, config.branch)
        source = config.published_commit or resolve_commit(repo, config.baseline_commit)
        if source == target:
            continue
        if not is_ancestor(repo, source, target):
            raise RepPatchError(
                f"公開済みコミットが {config.branch} の祖先ではありません: {config.display_name}"
            )
        patch = run_git(
            repo,
            [
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                source,
                target,
                "--",
            ],
        ).stdout
        if not patch:
            continue
        sequence = latest_sequence.get(config.repo_id, 0) + 1
        relative_dir = Path("packages") / config.repo_id / f"{sequence:06d}"
        target_dir = patch_root / relative_dir
        chunks = split_patch(patch, target_dir, settings.chunk_size_mib * 1024 * 1024)

        diff_status = diff_file_status(repo, source, target)
        added_records: list[dict[str, Any]] = []
        added_files_dir = target_dir / "added_files"
        files_to_bundle = sorted(set(diff_status["added"] + diff_status["modified"]))
        for rel_path in files_to_bundle:
            file_bytes = run_git(repo, ["show", f"{target}:{rel_path}"]).stdout
            file_target = added_files_dir / rel_path
            file_target.parent.mkdir(parents=True, exist_ok=True)
            file_target.write_bytes(file_bytes)
            added_records.append(
                {
                    "path": rel_path,
                    "size": len(file_bytes),
                    "sha256": sha256_bytes(file_bytes),
                }
            )


        manifest = sign_document(
            {
                "schema_version": SCHEMA_VERSION,
                "repo_id": config.repo_id,
                "display_name": config.display_name,
                "kind": config.kind,
                "sequence": sequence,
                "created_at": timestamp,
                "source_branch": config.branch,
                "source_from_commit": source,
                "source_to_commit": target,
                "patch_size": len(patch),
                "patch_sha256": sha256_bytes(patch),
                "chunks": chunks,
                "changed_paths": changed_paths(repo, source, target),
                "added_files": added_records,
                "deleted_files": diff_status["deleted"],
                "target_files": tracked_files(repo, target),
            },
            settings.patch_password,
        )
        manifest_path = relative_dir / "manifest.json"
        write_json(patch_root / manifest_path, manifest)
        index["packages"].append(
            {
                "repo_id": config.repo_id,
                "sequence": sequence,
                "manifest_path": manifest_path.as_posix(),
            }
        )
        latest_sequence[config.repo_id] = sequence
        created.append(manifest)

    if not created:
        return {"published": False, "message": "公開する変更はありません", "packages": []}
    unsigned_index = {key: value for key, value in index.items() if key != "signature"}
    signed_index = sign_document(unsigned_index, settings.patch_password)
    write_json(index_path, signed_index)
    run_git(patch_root, ["add", "--", ".gitattributes", "package-index.json", "packages"])
    label = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_git(patch_root, ["commit", "-m", f"Publish patch package {label}"])
    run_git(patch_root, ["push"])
    _reconcile_published(settings, store, signed_index, patch_root)
    return {
        "published": True,
        "message": f"{len(created)}件のパッチを公開しました",
        "packages": created,
    }


def reset_repository_patches(
    settings: Settings,
    store: SettingsStore,
    repo_id: str,
    new_baseline_commit: str | None = None,
) -> dict[str, Any]:
    """指定リポジトリのパッチ履歴をリセットし次回000001からパッチ再作成可能にする"""
    if repo_id not in settings.repositories:
        raise RepPatchError(f"リポジトリが登録されていません: {repo_id}")
    config = settings.repositories[repo_id]
    patch_root = Path(settings.patch_repo).expanduser()
    ensure_repository(patch_root)
    if not settings.patch_password:
        raise RepPatchError("パッチ検証用パスワードを設定してください")

    _push_pending(patch_root)

    # 1. パッチ専用リポジトリの該当パッケージディレクトリを削除
    target_repo_dir = patch_root / "packages" / repo_id
    if target_repo_dir.exists():
        shutil.rmtree(target_repo_dir)

    # 2. package-index.json から該当 repo_id を除外して再署名
    index_path = patch_root / "package-index.json"
    index = load_index(index_path, settings.patch_password)
    remaining_packages = [
        item for item in index.get("packages", []) if item.get("repo_id") != repo_id
    ]
    index["packages"] = remaining_packages
    unsigned_index = {key: value for key, value in index.items() if key != "signature"}
    signed_index = sign_document(unsigned_index, settings.patch_password)
    write_json(index_path, signed_index)

    # 3. パッチ専用リポジトリでコミット＆push
    run_git(patch_root, ["add", "-A"])
    diff_status = run_git(patch_root, ["status", "--porcelain=v1"]).text.strip()
    if diff_status:
        run_git(patch_root, ["commit", "-m", f"Reset patches for {config.display_name}"])
        run_git(patch_root, ["push"])
    else:
        _push_pending(patch_root)

    # 4. 自宅側設定の更新（published_commit のクリア、および任意で baseline_commit の更新）
    config.published_commit = ""
    if new_baseline_commit:
        config.baseline_commit = resolve_commit(Path(config.path), new_baseline_commit)
    settings.repositories[repo_id] = config
    store.save(settings)

    return {
        "reset": True,
        "repo_id": repo_id,
        "display_name": config.display_name,
        "message": f"{config.display_name} のパッチ履歴をリセットしました（次回パッチ連番: #000001）",
        "repository": asdict(config),
    }


