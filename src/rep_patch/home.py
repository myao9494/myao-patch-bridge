"""
自宅環境でのリポジトリ検出・登録・管理およびパッチ公開処理を行うモジュール

仕様:
- discover_repositories: apps_rootおよびobsidian_repoからGitリポジトリを自動検出し設定に登録（既存の設定は保持）
- add_repository: 指定されたGitリポジトリのパスや設定を手動で登録（同じIDの既存登録は上書きしない）
- delete_repository: 登録済みリポジトリを設定から削除
- update_repository: リポジトリのブランチや初期導入地点、有効無効状態、検証の除外パターンを更新
- scan_repositories: 登録済みリポジトリの未公開コミット数やクリーン状態を取得（リポジトリごとに並列）
- publish: 有効なリポジトリの差分をパッケージ化してパッチ専用リポジトリへpushする
  1. パッチ専用リポジトリがクリーンであることを確認し、前回push失敗分があればpush
  2. 全リポジトリの差分範囲を確定（設定不備は何も書き出す前に検出）し、事前クリーン検証を実行
  3. 差分パッチの分割、変更・新規ファイル実体の同梱、削除記録、マニフェスト署名
  4. .gitattributesのバイナリ保護、過去パッチの削除（リポジトリ軽量化）、今回分だけのインデックス署名、コミット
     （パッケージ内のファイルは、同梱した .gitignore の対象であっても強制的にコミットへ含める）
  5. 公開前リハーサル: コミットした内容から「Download ZIP」と同じZIPを作り、会社側と同じ検証・適用を
     一時フォルダで実行して、結果が公開先コミットと一致することを確認する（rehearsal.py）
     （ここまでに失敗した場合は作りかけとコミットを破棄し、公開前のコミットの状態へ戻す。pushはしない）
  6. push成功後に公開済みコミットと連番を保存し、今回分軽量ZIPの生成・検証とGitHub Releases自動公開を行う
- reset_repository_patches: 指定リポジトリのパッチ専用リポジトリ内パッケージ削除・インデックス再署名・pushおよび公開済みコミット・連番の初期化
"""
from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    OBSIDIAN_REPO_ID,
    PROJECT_ROOT,
    REPOSITORY_KINDS,
    RepositoryConfig,
    Settings,
    SettingsStore,
    repo_id_for,
)
from .errors import RepPatchError
from .github import create_github_release, get_repo_slug, upload_release_asset
from .git import (
    NETWORK_TIMEOUT_SECONDS,
    changed_paths,
    diff_file_status,
    ensure_repository,
    is_ancestor,
    is_repository,
    read_blobs,
    repository_status,
    resolve_commit,
    run_git,
    tracked_files,
)
from .packages import (
    INDEX_NAME,
    SCHEMA_VERSION,
    PatchArchive,
    build_index,
    create_release_bundle,
    load_index,
    package_dir,
    split_added_file,
    split_patch,
    write_json,
)
from .lfs import parse_lfs_pointer, resolve_lfs_pointer
from .rehearsal import rehearse_commit
from .security import sha256_bytes, sign_document
from .verifier import diff_warnings, unsupported_diff_entries, verify_clean_repository

# パッチ専用リポジトリの .gitattributes に必ず含めるバイナリ保護ルール
PATCH_REPO_BINARY_RULES = ("*.patch.part-* binary", "packages/** binary")
_SCAN_WORKERS = 8


def discover_repositories(settings: Settings, store: SettingsStore) -> list[dict[str, Any]]:
    root = Path(settings.apps_root).expanduser()
    if not root.is_dir():
        raise RepPatchError(f"アプリルートが見つかりません: {root}")
    obsidian = Path(settings.obsidian_repo).expanduser().resolve() if settings.obsidian_repo else None
    discovered: dict[str, RepositoryConfig] = dict(settings.repositories)
    candidates = [item for item in root.iterdir() if item.is_dir()]
    if obsidian:
        candidates.append(Path(settings.obsidian_repo).expanduser())
    for path in candidates:
        if path.resolve() == PROJECT_ROOT.resolve() or not is_repository(path):
            continue
        kind = "obsidian" if obsidian and path.resolve() == obsidian else "app"
        repo_id = OBSIDIAN_REPO_ID if kind == "obsidian" else repo_id_for(path.name)
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


def _scan_repository(config: RepositoryConfig) -> dict[str, Any]:
    item: dict[str, Any] = asdict(config)
    try:
        repo = Path(config.path)
        status = repository_status(repo)
        target = resolve_commit(repo, config.branch)
        base = config.published_commit or config.baseline_commit
        count = 0
        if base:
            count = int(run_git(repo, ["rev-list", "--count", f"{base}..{target}"]).text)
        item.update(status)
        item.update({"target_commit": target, "unpublished_commits": count, "error": ""})
    except (RepPatchError, ValueError) as exc:
        item.update({"clean": False, "error": str(exc), "unpublished_commits": 0})
    return item


def scan_repositories(settings: Settings) -> list[dict[str, Any]]:
    configs = sorted(settings.repositories.values(), key=lambda item: item.display_name.lower())
    if not configs:
        return []
    with ThreadPoolExecutor(max_workers=min(_SCAN_WORKERS, len(configs))) as executor:
        return list(executor.map(_scan_repository, configs))


def _clean_patterns(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RepPatchError("検証の除外パターンは文字列の一覧で指定してください")
    return [item.strip() for item in value if item.strip()]


def add_repository(
    settings: Settings, store: SettingsStore, payload: dict[str, Any]
) -> dict[str, Any]:
    raw_path = (payload.get("path") or "").strip()
    if not raw_path:
        raise RepPatchError("リポジトリのパスを指定してください")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir():
        raise RepPatchError(f"Gitリポジトリが見つかりません: {raw_path}")
    if not is_repository(path):
        raise RepPatchError(f"指定されたディレクトリはGitリポジトリではありません: {raw_path}")

    kind = payload.get("kind") or ("obsidian" if "obsidian" in path.name.lower() else "app")
    if kind not in REPOSITORY_KINDS:
        raise RepPatchError("種類は app または obsidian で指定してください")
    display_name = (payload.get("display_name") or "").strip() or path.name
    repo_id = (payload.get("repo_id") or "").strip() or (
        OBSIDIAN_REPO_ID if kind == "obsidian" and path.name.lower().startswith("obsidian") else repo_id_for(display_name)
    )
    if repo_id in settings.repositories:
        # 上書きすると公開済みコミットと連番の記録が失われ、次回のパッチが壊れる
        existing = settings.repositories[repo_id]
        raise RepPatchError(
            f"同じID（{repo_id}）のリポジトリが既に登録されています: {existing.path}。"
            "別の表示名を指定するか、既存のカードを削除してから追加してください"
        )

    branch = (payload.get("branch") or "").strip()
    if not branch:
        branch_res = run_git(path, ["symbolic-ref", "--short", "HEAD"], check=False)
        if branch_res.returncode == 0 and branch_res.text:
            branch = branch_res.text
        else:
            abbrev_res = run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
            branch = abbrev_res.text if abbrev_res.returncode == 0 and abbrev_res.text and abbrev_res.text != "HEAD" else "main"

    baseline_commit = (payload.get("baseline_commit") or "").strip()
    if baseline_commit:
        baseline_commit = resolve_commit(path, baseline_commit)

    config = RepositoryConfig(
        repo_id=repo_id,
        display_name=display_name,
        path=str(path),
        kind=kind,
        enabled=bool(payload.get("enabled", True)),
        branch=branch,
        baseline_commit=baseline_commit,
        clean_verify_ignore_patterns=_clean_patterns(payload.get("clean_verify_ignore_patterns") or []),
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
    for key in ("display_name", "path", "kind", "branch", "baseline_commit"):
        if key in updates:
            if not isinstance(updates[key], str):
                raise RepPatchError(f"{key} は文字列で指定してください")
            setattr(config, key, updates[key].strip())
    if config.kind not in REPOSITORY_KINDS:
        raise RepPatchError("種類は app または obsidian で指定してください")
    if "enabled" in updates:
        if not isinstance(updates["enabled"], bool):
            raise RepPatchError("enabled は true または false で指定してください")
        config.enabled = updates["enabled"]
    if "clean_verify_ignore_patterns" in updates:
        config.clean_verify_ignore_patterns = _clean_patterns(updates["clean_verify_ignore_patterns"])
    if config.baseline_commit:
        config.baseline_commit = resolve_commit(Path(config.path), config.baseline_commit)
    settings.repositories[repo_id] = config
    store.save(settings)
    return asdict(config)


def _reconcile_published(settings: Settings, store: SettingsStore, index: dict[str, Any], patch_root: Path) -> None:
    """パッチ専用リポジトリに載っている最新パッチへ、自宅側設定の公開済みコミットを合わせる（push失敗後の復旧用）"""
    latest: dict[str, dict[str, Any]] = {}
    for item in index["packages"]:
        manifest_path = patch_root / item["manifest_path"]
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
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


def _push(patch_root: Path) -> None:
    run_git(patch_root, ["push"], timeout=NETWORK_TIMEOUT_SECONDS)


def _push_pending(patch_root: Path) -> None:
    upstream = run_git(patch_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], check=False)
    if upstream.returncode != 0:
        return
    ahead = int(run_git(patch_root, ["rev-list", "--count", "@{u}..HEAD"]).text or "0")
    if ahead:
        _push(patch_root)


def _ensure_patch_repo_clean(patch_root: Path) -> None:
    if run_git(patch_root, ["status", "--porcelain=v1"]).text:
        raise RepPatchError("パッチ専用リポジトリに未コミットの変更があります")


def _head_commit(patch_root: Path) -> str:
    """現在のコミットID。まだコミットが無ければ空文字列"""
    result = run_git(patch_root, ["rev-parse", "--verify", "--quiet", "HEAD"], check=False)
    return result.text if result.returncode == 0 else ""


def _restore_patch_worktree(patch_root: Path, commit: str | None = None) -> None:
    """
    作りかけのパッケージを破棄し、パッチ専用リポジトリを commit（省略時は現在のコミット）の状態へ戻す。
    commit が空文字列（処理前にコミットが1つも無かった）の場合は、コミットの無い状態へ戻す。

    呼び出し前に作業ツリーがクリーンであることを確認済みなので、消えるのは今回の公開処理が
    書き出したファイルと作ったコミットだけである。これを怠ると次回の公開が
    「未コミットの変更があります」で止まる。
    """
    if commit == "":
        run_git(patch_root, ["update-ref", "-d", "HEAD"], check=False)
        run_git(patch_root, ["read-tree", "--empty"], check=False)
    else:
        run_git(patch_root, ["reset", "--hard", commit or "HEAD"], check=False)
    run_git(patch_root, ["clean", "-fd"], check=False)
    # 同梱した .gitignore の対象になったファイルは「無視されたファイル」として残るので、それも消す
    run_git(patch_root, ["clean", "-fdx", "--", "packages"], check=False)


def _stage_packages(patch_root: Path, created: list[dict[str, Any]]) -> None:
    """
    今回の公開内容をステージする。

    パッケージには各アプリの .gitignore も同梱されるため、通常の `git add` では、その対象になる
    同梱ファイル（追跡されているのに除外対象でもあるファイル）がコミットから漏れ、会社側で
    「ZIP内のファイルがありません」になる。パッケージのフォルダは除外ルールを無視して追加する。
    """
    run_git(patch_root, ["add", "-A"])
    package_dirs = [str(package_dir(str(m["repo_id"]), int(m["sequence"]))) for m in created]
    run_git(patch_root, ["--literal-pathspecs", "add", "-f", "--", *package_dirs])


def _rehearse(patch_root: Path, settings: Settings) -> None:
    """コミットした内容を会社側と同じ手順で検証・適用してみる。問題があれば公開を中止させる"""
    sources = {repo_id: Path(config.path) for repo_id, config in settings.repositories.items()}
    try:
        rehearse_commit(patch_root, "HEAD", settings.patch_password, sources)
    except RepPatchError as exc:
        raise RepPatchError(
            "公開前リハーサル（会社側と同じ検証・適用を自宅で実行）で問題が見つかったため、"
            f"公開を中止しました。GitHubへは送信していません: {exc}"
        ) from exc


def _ensure_gitattributes(patch_root: Path) -> None:
    """Gitの改行コード正規化でパッチ分割・同梱ファイル実体のバイト列が変わらないよう保護する"""
    attributes_path = patch_root / ".gitattributes"
    if attributes_path.exists():
        attributes = attributes_path.read_text(encoding="utf-8")
        missing = [rule for rule in PATCH_REPO_BINARY_RULES if rule not in attributes.splitlines()]
        if missing:
            attributes_path.write_text(
                attributes.rstrip("\n") + "\n" + "\n".join(missing) + "\n",
                encoding="utf-8",
            )
    else:
        attributes_path.write_text(
            "# Managed by Myao Patch Bridge\n"
            "*.json text eol=lf\n"
            + "\n".join(PATCH_REPO_BINARY_RULES)
            + "\n",
            encoding="utf-8",
        )


@dataclass
class _PublishPlan:
    """公開対象1リポジトリ分の差分範囲"""

    config: RepositoryConfig
    repo: Path
    source: str
    target: str


def _plan_publish(settings: Settings) -> list[_PublishPlan]:
    """有効なリポジトリの差分範囲を確定する。ここでは何も書き換えず、設定不備は書き出し前に検出する。"""
    plans: list[_PublishPlan] = []
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
        plans.append(_PublishPlan(config, repo, source, target))
    return plans


def _verify_plans(plans: list[_PublishPlan]) -> None:
    """公開前クリーン環境シミュレーション検証（会社側でのファイル不在エラー防止）"""
    for plan in plans:
        config = plan.config
        verification = verify_clean_repository(
            plan.repo,
            branch=config.branch,
            baseline_commit=plan.source,
            ignore_patterns=config.clean_verify_ignore_patterns,
            repo_id=config.repo_id,
        )
        if not verification["valid"]:
            err_msg = " / ".join(verification["errors"])
            raise RepPatchError(
                f"会社環境検証（ファイル不在・整合性チェック）でエラーが検知されたため、パッチ公開を中断しました [{config.display_name}]: {err_msg}"
            )


def _build_package(
    plan: _PublishPlan,
    patch_root: Path,
    sequence: int,
    timestamp: str,
    settings: Settings,
) -> dict[str, Any] | None:
    """1リポジトリ分のパッケージをパッチ専用リポジトリへ書き出し、署名済みマニフェストを返す。差分が空ならNone。"""
    config, repo, source, target = plan.config, plan.repo, plan.source, plan.target
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
        return None

    target_entries = tracked_files(repo, target)
    problems = unsupported_diff_entries(repo, source, target, target_entries)
    if problems:
        raise RepPatchError(f"{' / '.join(problems)} [{config.display_name}]")

    relative_dir = package_dir(config.repo_id, sequence)
    target_dir = patch_root.joinpath(*relative_dir.parts)
    if target_dir.exists():
        # 以前の失敗で残った実体があると、マニフェストに無いファイルが紛れ込む
        shutil.rmtree(target_dir)
    chunks = split_patch(patch, target_dir, settings.chunk_size_mib * 1024 * 1024)

    # 新規追加・変更されたファイルの実体を同梱する（会社側はこれを直接上書き配置する）
    diff_status = diff_file_status(repo, source, target)
    blob_ids = {item["path"]: item["blob"] for item in target_entries}
    bundle_paths = sorted(set(diff_status["added"] + diff_status["modified"]))
    blobs = read_blobs(repo, [blob_ids[rel_path] for rel_path in bundle_paths])
    added_records: list[dict[str, Any]] = []
    added_files_dir = target_dir / "added_files"
    chunk_bytes = settings.chunk_size_mib * 1024 * 1024
    for rel_path in bundle_paths:
        file_bytes = blobs[blob_ids[rel_path]]
        if parse_lfs_pointer(file_bytes) is not None:
            # 会社PCはオフラインでLFSの実体を取得できない。自宅側で実体を取り出して運ぶ
            file_bytes = resolve_lfs_pointer(repo, file_bytes, rel_path)
        record: dict[str, Any] = {
            "path": rel_path,
            "size": len(file_bytes),
            "sha256": sha256_bytes(file_bytes),
        }
        if len(file_bytes) > chunk_bytes:
            # GitHubの1ファイル上限（100MB）を超えないよう分割して運ぶ。会社側で結合・照合する
            record["parts"] = split_added_file(file_bytes, rel_path, target_dir, chunk_bytes)
        else:
            file_target = added_files_dir / rel_path
            file_target.parent.mkdir(parents=True, exist_ok=True)
            file_target.write_bytes(file_bytes)
        added_records.append(record)

    # 大文字小文字だけを変えた名前変更（claude.md → CLAUDE.md）では、古い名前を削除一覧に載せない。
    # 会社PC（Windows）では新旧の名前が同じファイルを指すので、削除を後から実行する版の会社側アプリが
    # 書き込んだばかりの実体を消してしまう。載せなければ、どの版でも内容は正しく届く。
    delivered_folded = {rel_path.casefold() for rel_path in bundle_paths}
    deleted_files = [
        rel_path for rel_path in diff_status["deleted"] if rel_path.casefold() not in delivered_folded
    ]

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
            "deleted_files": deleted_files,
            "target_files": target_entries,
        },
        settings.patch_password,
    )
    write_json(target_dir / "manifest.json", manifest)
    return manifest


def _prune_old_packages(patch_root: Path, created: list[dict[str, Any]]) -> None:
    """ワーキングツリーの軽量化: 今回作成されたパッチ以外の過去ディレクトリを削除"""
    packages_dir = patch_root / "packages"
    keep_dirs = {
        patch_root.joinpath(*package_dir(m["repo_id"], m["sequence"]).parts) for m in created
    }
    if not packages_dir.exists():
        return
    for repo_dir in packages_dir.iterdir():
        if not repo_dir.is_dir():
            continue
        for seq_dir in list(repo_dir.iterdir()):
            if seq_dir.is_dir() and seq_dir not in keep_dirs:
                shutil.rmtree(seq_dir)
        if not any(repo_dir.iterdir()):
            repo_dir.rmdir()


def _bundle_output_dir(settings: Settings, patch_root: Path) -> Path:
    if settings.download_dir:
        return Path(settings.download_dir).expanduser()
    # 出力先が未設定ならパッチ専用リポジトリ内の dist/ へ置く。次回公開時に未コミットの変更として
    # 検出されないよう、このリポジトリだけの除外設定（コミットされない）へ登録しておく。
    exclude_path = Path(run_git(patch_root, ["rev-parse", "--git-path", "info/exclude"]).text)
    if not exclude_path.is_absolute():
        exclude_path = patch_root / exclude_path
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    if "/dist/" not in existing.splitlines():
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        prefix = existing.rstrip("\n") + "\n" if existing else ""
        exclude_path.write_text(prefix + "/dist/\n", encoding="utf-8")
    return patch_root / "dist"


def _publish_github_release(
    settings: Settings, patch_root: Path, bundle_path: Path, created: list[dict[str, Any]], label: str
) -> dict[str, str]:
    """GitHub Releases へ今回分ZIPを公開する。失敗しても公開処理全体は成功として扱い、理由を返す。"""
    result = {"release_url": "", "download_url": "", "release_error": ""}
    try:
        repo_slug = get_repo_slug(patch_root, settings.github_repo)
        tag_name = f"v{datetime.now(timezone.utc).strftime('%Y.%m.%d-%H%M%S')}"
        body_lines = [f"- **{m.get('display_name', m['repo_id'])}**: #{m['sequence']:06d}" for m in created]
        release_data = create_github_release(
            token=settings.github_token,
            repo_slug=repo_slug,
            tag_name=tag_name,
            name=f"Patch Release {label}",
            body="Myao Patch Bridge 自動パッチ公開\n\n" + "\n".join(body_lines),
        )
        result["release_url"] = release_data.get("html_url", "")
        upload_url = release_data.get("upload_url", "")
        if upload_url:
            asset_data = upload_release_asset(
                token=settings.github_token,
                upload_url=upload_url,
                file_path=bundle_path,
            )
            result["download_url"] = asset_data.get("browser_download_url", "")
    except Exception as exc:  # noqa: BLE001 - the patch itself is already published
        result["release_error"] = str(exc)
    return result


def publish(settings: Settings, store: SettingsStore, *, skip_verification: bool = False) -> dict[str, Any]:
    if settings.mode != "home":
        raise RepPatchError("パッチの公開は自宅モードでのみ実行できます")
    if not settings.patch_password:
        raise RepPatchError("パッチ検証用パスワードを設定してください")
    patch_root = Path(settings.patch_repo).expanduser()
    ensure_repository(patch_root)
    _ensure_patch_repo_clean(patch_root)
    _push_pending(patch_root)

    index_path = patch_root / INDEX_NAME
    index = load_index(index_path, settings.patch_password)
    _reconcile_published(settings, store, index, patch_root)

    plans = _plan_publish(settings)
    if not skip_verification:
        _verify_plans(plans)
    warnings = [
        f"[{plan.config.display_name}] {warning}"
        for plan in plans
        for warning in diff_warnings(plan.repo, plan.source, plan.target)
    ]
    nothing_to_publish = {"published": False, "message": "公開する変更はありません", "packages": []}
    if not plans:
        return nothing_to_publish

    # 次の連番: 自宅側設定の記録と、パッチ専用リポジトリに残っている分の大きい方から続ける
    latest_sequence = {repo_id: cfg.last_sequence for repo_id, cfg in settings.repositories.items()}
    for item in index["packages"]:
        repo_id = item["repo_id"]
        latest_sequence[repo_id] = max(latest_sequence.get(repo_id, 0), int(item["sequence"]))

    created: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    label = now.strftime("%Y-%m-%d")
    head_before = _head_commit(patch_root)
    try:
        _ensure_gitattributes(patch_root)
        for plan in plans:
            sequence = latest_sequence.get(plan.config.repo_id, 0) + 1
            manifest = _build_package(plan, patch_root, sequence, now.isoformat(), settings)
            if manifest:
                created.append(manifest)
        if not created:
            _restore_patch_worktree(patch_root, head_before)
            return nothing_to_publish
        _prune_old_packages(patch_root, created)
        # package-index.json を今回作成されたパッチのみで再構築
        write_json(index_path, build_index(created, settings.patch_password))
        _stage_packages(patch_root, created)
        run_git(patch_root, ["commit", "-m", f"Publish patch package {label}"])
        # 会社へ届く内容で、会社側と同じ検証・適用を先に実行する。失敗したらコミットごと取り消す
        _rehearse(patch_root, settings)
    except BaseException:
        _restore_patch_worktree(patch_root, head_before)
        raise
    # ここで失敗してもコミットは残り、次回の公開時に先頭でpushを再試行する
    _push(patch_root)

    # 自宅側設定の published_commit および last_sequence を更新・保存
    for manifest in created:
        config = settings.repositories.get(manifest["repo_id"])
        if config:
            config.published_commit = manifest["source_to_commit"]
            config.last_sequence = int(manifest["sequence"])
    store.save(settings)

    result: dict[str, Any] = {
        "published": True,
        "message": f"{len(created)}件のパッチを公開しました",
        "packages": created,
        "bundle_path": "",
        "bundle_name": "",
        "bundle_size": 0,
        "bundle_error": "",
        "release_url": "",
        "download_url": "",
        "release_error": "",
        "warnings": warnings,
    }

    # 配布用軽量ZIPの生成（今回作成されたパッチのみ同梱）。パッチ自体は公開済みなので失敗しても中断しない
    try:
        bundle_path = create_release_bundle(
            patch_root=patch_root,
            created_manifests=created,
            password=settings.patch_password,
            output_dir=_bundle_output_dir(settings, patch_root),
            bundle_name=f"myao_app_patch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.zip",
        )
        try:
            # 手渡し用のZIPも、会社側と同じ検証を通ることを確かめてから渡す
            with PatchArchive(bundle_path, settings.patch_password):
                pass
        except Exception:
            bundle_path.unlink(missing_ok=True)
            raise
    except Exception as exc:  # noqa: BLE001
        result["bundle_error"] = str(exc)
        return result
    result.update(
        {
            "bundle_path": str(bundle_path),
            "bundle_name": bundle_path.name,
            "bundle_size": bundle_path.stat().st_size,
        }
    )

    # GitHub Releases への自動公開（トークン設定時）
    if settings.github_token:
        result.update(_publish_github_release(settings, patch_root, bundle_path, created, label))
    return result


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
    new_baseline = resolve_commit(Path(config.path), new_baseline_commit) if new_baseline_commit else ""
    _ensure_patch_repo_clean(patch_root)
    _push_pending(patch_root)

    index_path = patch_root / INDEX_NAME
    index = load_index(index_path, settings.patch_password)
    try:
        # 1. パッチ専用リポジトリの該当パッケージディレクトリを削除
        target_repo_dir = patch_root / "packages" / repo_id
        if target_repo_dir.exists():
            shutil.rmtree(target_repo_dir)

        # 2. package-index.json から該当 repo_id を除外して再署名
        index["packages"] = [item for item in index.get("packages", []) if item.get("repo_id") != repo_id]
        unsigned_index = {key: value for key, value in index.items() if key != "signature"}
        write_json(index_path, sign_document(unsigned_index, settings.patch_password))

        # 3. パッチ専用リポジトリでコミット
        run_git(patch_root, ["add", "-A"])
        if run_git(patch_root, ["status", "--porcelain=v1"]).text:
            run_git(patch_root, ["commit", "-m", f"Reset patches for {config.display_name}"])
    except BaseException:
        _restore_patch_worktree(patch_root)
        raise
    _push_pending(patch_root)

    # 4. 自宅側設定の更新（published_commit のクリア、last_sequenceの初期化、および任意で baseline_commit の更新）
    config.published_commit = ""
    config.last_sequence = 0
    if new_baseline:
        config.baseline_commit = new_baseline
    settings.repositories[repo_id] = config
    store.save(settings)

    return {
        "reset": True,
        "repo_id": repo_id,
        "display_name": config.display_name,
        "message": f"{config.display_name} のパッチ履歴をリセットしました（次回パッチ連番: #000001）",
        "repository": asdict(config),
    }
