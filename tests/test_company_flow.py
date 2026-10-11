"""
会社環境でのパッチZIP検証・適用・バックアップ・コミットフローのテスト

仕様:
- test_independent_git_history_apply_then_auto_commit: 独立Git履歴へのパッチ適用・新規ファイル自動配置・削除ファイル反映・自動コミット検証
- test_apply_succeeds_even_with_company_specific_files: 会社側独自ファイル存在時でも安全にパッチ適用・コミットされ独自ファイルも保持される検証
- test_apply_with_added_and_deleted_files_and_rollback: 新規ファイル自動配置と削除ファイル処理および失敗時の完全復元検証
- test_force_overwrite_and_delete_applies_directly: git applyを使わずファイル直接上書き配置と削除で同期するテスト
- test_single_repository_commit_pending: リポジトリID指定での個別コミット検証
- test_list_company_repositories: 会社側リポジトリ一覧および状態取得の検証
- test_init_repository_sequence_and_apply_from_sequence_two: 適用開始番号指定によるstate.json生成と連番スキップ適用の検証
- test_init_all_repositories_sequence: 全リポジトリ一括での適用開始番号初期化・state.json生成検証
- test_apply_auto_commits_uncommitted_changes_and_immediately_commits_patch: 未コミット変更の自動事前コミットとパッチ適用の即時確定・クリーン化検証
- test_apply_added_files_with_crlf_lf_conversion: Git改行コード変換（CRLF↔LF）が生じた追加ファイルでも破損と誤判定されず安全に検証・復元適用される検証
- test_apply_patch_whose_content_already_matches_advances_sequence: 内容が既に一致しているパッチはコミットを作らず適用済みとして連番を進める検証
- test_failed_apply_restores_pre_apply_commit_and_files: 適用途中・コミット後の失敗でも、適用前のコミットとファイル（Git管理外を含む）へ復元される検証
- test_apply_reports_missing_sequence: 連番が欠けたZIPは適用せず、必要な連番を示して失敗する検証
- test_legacy_pending_state_is_committed_as_confirmed: 旧バージョンが残した未コミット適用済み連番を確定済みへ取り込む検証
- test_excluded_repository_is_skipped_but_can_be_applied_individually: 除外中リポジトリの一括適用スキップと個別適用の検証
- test_retry_for_repository_not_in_archive_reports_unchanged: ZIPに含まれないアプリへの個別適用が結果として返る検証
- test_download_listing_and_zip_location_guard: ZIP一覧取得と、ダウンロードフォルダ外のZIPを拒否する検証
- test_inspect_reports_unmapped_repository: 会社側に対応リポジトリが無い場合の検証結果の検証
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import rep_patch.company as company_module
from rep_patch.company import (
    apply_archive,
    commit_pending,
    init_all_repositories_sequence,
    init_repository_sequence,
    inspect_archive,
    list_company_repositories,
    list_download_packages,
    load_state,
    save_state,
)
from rep_patch.config import Settings
from rep_patch.errors import PackageValidationError, RepPatchError
from rep_patch.git import changed_paths, diff_file_status, tracked_files
from rep_patch.packages import PACKAGE_TYPE, SCHEMA_VERSION, split_patch
from rep_patch.security import sha256_bytes, sign_document

PASSWORD = "test-password"


def make_package_repo(
    root: Path,
    source: Path,
    git,
    revisions: list[tuple[str, str]],
) -> Path:
    package_root = root / "myao_app_patch-main"
    entries = []
    for sequence, (older, newer) in enumerate(revisions, start=1):
        patch = git(
            source,
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            older,
            newer,
            "--",
        )
        relative = Path("packages") / "sample-app" / f"{sequence:06d}"
        package_dir = package_root / relative
        chunks = split_patch(patch, package_dir, 80)
        
        status = diff_file_status(source, older, newer)
        added_records = []
        added_files_dir = package_dir / "added_files"
        for rel_path in sorted(set(status["added"] + status["modified"])):
            content = git(source, "show", f"{newer}:{rel_path}")
            target_file = added_files_dir / rel_path
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_bytes(content)
            added_records.append(
                {
                    "path": rel_path,
                    "size": len(content),
                    "sha256": sha256_bytes(content),
                }
            )


        manifest = sign_document(
            {
                "schema_version": SCHEMA_VERSION,
                "repo_id": "sample-app",
                "display_name": "sample-app",
                "kind": "app",
                "sequence": sequence,
                "created_at": "2026-08-11T00:00:00+00:00",
                "source_branch": "main",
                "source_from_commit": older,
                "source_to_commit": newer,
                "patch_size": len(patch),
                "patch_sha256": sha256_bytes(patch),
                "chunks": chunks,
                "changed_paths": changed_paths(source, older, newer),
                "added_files": added_records,
                "deleted_files": status["deleted"],
                "target_files": tracked_files(source, newer),
            },
            PASSWORD,
        )
        manifest_path = relative / "manifest.json"
        (package_root / manifest_path).write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        entries.append(
            {
                "repo_id": "sample-app",
                "sequence": sequence,
                "manifest_path": manifest_path.as_posix(),
            }
        )
    index = sign_document(
        {
            "schema_version": SCHEMA_VERSION,
            "package_type": PACKAGE_TYPE,
            "packages": entries,
        },
        PASSWORD,
    )
    (package_root / "package-index.json").write_text(json.dumps(index), encoding="utf-8")
    zip_path = root / "myao_app_patch-main.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in package_root.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())
    return zip_path


def test_independent_git_history_apply_then_auto_commit(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    source = init_repo(tmp_path / "source")
    (source / "app.txt").write_text("base\n", encoding="utf-8")
    (source / "binary.bin").write_bytes(bytes(range(64)))
    git(source, "add", "-A")
    git(source, "commit", "-m", "source base")
    base = git(source, "rev-parse", "HEAD").decode().strip()

    (source / "app.txt").write_text("patch one\n", encoding="utf-8")
    (source / "binary.bin").write_bytes(bytes(reversed(range(64))))
    (source / "日本語.txt").write_text("追加\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "source patch one")
    patch_one = git(source, "rev-parse", "HEAD").decode().strip()

    company_root = tmp_path / "company-apps"
    company = init_repo(company_root / "sample-app")
    (company / "app.txt").write_text("base\n", encoding="utf-8")
    (company / "binary.bin").write_bytes(bytes(range(64)))
    git(company, "add", "-A")
    git(company, "commit", "-m", "completely independent company history")
    company_base = git(company, "rev-parse", "HEAD").decode().strip()
    assert company_base != base

    first_zip = make_package_repo(tmp_path / "first", source, git, [(base, patch_one)])
    settings = Settings(
        mode="company",
        company_apps_root=str(company_root),
        download_dir=str(first_zip.parent),
        patch_password=PASSWORD,
    )
    result = apply_archive(settings, str(first_zip))
    assert result["failed"] == 0
    assert (company / "app.txt").read_text(encoding="utf-8") == "patch one\n"
    assert (company / "日本語.txt").read_text(encoding="utf-8") == "追加\n"
    assert git(company, "status", "--porcelain").decode().strip() == ""
    assert git(company, "log", "-1", "--pretty=%s").decode().strip() == "[myao-patch] sample-app patch-000001"
    state = load_state(company, "sample-app")
    assert state["confirmed_sequence"] == 1

    (source / "app.txt").write_text("patch two\n", encoding="utf-8")
    (source / "日本語.txt").write_text("追加と修正\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "source patch two")
    patch_two = git(source, "rev-parse", "HEAD").decode().strip()
    second_zip = make_package_repo(
        tmp_path / "second", source, git, [(base, patch_one), (patch_one, patch_two)]
    )
    settings.download_dir = str(second_zip.parent)
    result = apply_archive(settings, str(second_zip))
    assert result["failed"] == 0
    assert (company / "app.txt").read_text(encoding="utf-8") == "patch two\n"
    assert git(company, "status", "--porcelain").decode().strip() == ""
    log = git(company, "log", "-1", "--pretty=%s").decode().strip()
    assert log == "[myao-patch] sample-app patch-000002"
    state = load_state(company, "sample-app")
    assert state["confirmed_sequence"] == 2


def test_apply_succeeds_even_with_company_specific_files(tmp_path: Path, git_helpers) -> None:
    """会社側リポジトリに独自ファイルが存在していても、パッチ適用と即時コミットが成功し、独自ファイルも保持されるテスト"""
    git, init_repo = git_helpers
    source = init_repo(tmp_path / "source")
    (source / "app.txt").write_text("expected base\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "base")
    base = git(source, "rev-parse", "HEAD").decode().strip()
    (source / "app.txt").write_text("new value\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "change")
    target = git(source, "rev-parse", "HEAD").decode().strip()

    company_root = tmp_path / "company-apps"
    company = init_repo(company_root / "sample-app")
    (company / "app.txt").write_text("expected base\n", encoding="utf-8")
    (company / "company_extra.txt").write_text("company specific\n", encoding="utf-8")
    git(company, "add", "-A")
    git(company, "commit", "-m", "company base with extra file")

    package = make_package_repo(tmp_path / "package", source, git, [(base, target)])
    settings = Settings(
        mode="company",
        company_apps_root=str(company_root),
        download_dir=str(package.parent),
        patch_password=PASSWORD,
    )
    result = apply_archive(settings, str(package))
    assert result["failed"] == 0
    assert result["succeeded"] == 1
    # パッチ対象ファイルが正しく上書き更新されていること
    assert (company / "app.txt").read_text(encoding="utf-8") == "new value\n"
    # 会社側独自ファイルも消えずに保持されていること
    assert (company / "company_extra.txt").read_text(encoding="utf-8") == "company specific\n"
    # 即時コミットされ、作業ツリーが完全にクリーンであること
    assert git(company, "status", "--porcelain").decode().strip() == ""
    # コミットログに patch-000001 が記録されていること
    log = git(company, "log", "-1", "--pretty=%s").decode().strip()
    assert log == "[myao-patch] sample-app patch-000001"


def test_apply_with_added_and_deleted_files_and_rollback(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    source = init_repo(tmp_path / "source")
    (source / "app.txt").write_text("base content\n", encoding="utf-8")
    (source / "delete_me.txt").write_text("old file\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "base")
    base = git(source, "rev-parse", "HEAD").decode().strip()

    # 変更: delete_me.txtを削除、nested/new_item.txtを追加
    (source / "delete_me.txt").unlink()
    nested_dir = source / "nested"
    nested_dir.mkdir()
    (nested_dir / "new_item.txt").write_text("brand new\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "add and delete")
    target = git(source, "rev-parse", "HEAD").decode().strip()

    company_root = tmp_path / "company-apps"
    company = init_repo(company_root / "sample-app")
    (company / "app.txt").write_text("base content\n", encoding="utf-8")
    (company / "delete_me.txt").write_text("old file\n", encoding="utf-8")
    git(company, "add", "-A")
    git(company, "commit", "-m", "company base")

    package = make_package_repo(tmp_path / "package", source, git, [(base, target)])
    settings = Settings(
        mode="company",
        company_apps_root=str(company_root),
        download_dir=str(package.parent),
        patch_password=PASSWORD,
    )

    # 適用
    result = apply_archive(settings, str(package))
    assert result["failed"] == 0
    # 新規ファイルが自動配置されていること
    assert (company / "nested" / "new_item.txt").is_file()
    assert (company / "nested" / "new_item.txt").read_text(encoding="utf-8") == "brand new\n"
    # 削除ファイルが会社側ワーキングツリーから消去されていること
    assert not (company / "delete_me.txt").exists()


def test_force_overwrite_and_delete_applies_directly(tmp_path: Path, git_helpers) -> None:
    """git applyを使わずにファイルを直接強制上書き・新規配置・削除し、会社側の内容が異なっていても適用できるテスト"""
    git, init_repo = git_helpers
    source = init_repo(tmp_path / "source")
    (source / "file_mod.txt").write_text("source v1\n", encoding="utf-8")
    (source / "file_del.txt").write_text("will be deleted\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "v1")
    base = git(source, "rev-parse", "HEAD").decode().strip()

    # 自宅側で変更、削除、追加
    (source / "file_mod.txt").write_text("source v2 changed\n", encoding="utf-8")
    (source / "file_del.txt").unlink()
    (source / "file_new.txt").write_text("brand new file\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "v2")
    target = git(source, "rev-parse", "HEAD").decode().strip()

    company_root = tmp_path / "company-apps"
    company = init_repo(company_root / "sample-app")
    # 会社側で異なる内容（git diffのコンテキストが一致しない状態）
    (company / "file_mod.txt").write_text("company modified locally\n", encoding="utf-8")
    (company / "file_del.txt").write_text("company del file\n", encoding="utf-8")
    git(company, "add", "-A")
    git(company, "commit", "-m", "company commit")

    package = make_package_repo(tmp_path / "package", source, git, [(base, target)])
    settings = Settings(
        mode="company",
        company_apps_root=str(company_root),
        download_dir=str(package.parent),
        patch_password=PASSWORD,
    )

    result = apply_archive(settings, str(package))
    assert result["failed"] == 0
    # 強制上書きされていること
    assert (company / "file_mod.txt").read_text(encoding="utf-8") == "source v2 changed\n"
    # 新規配置されていること
    assert (company / "file_new.txt").read_text(encoding="utf-8") == "brand new file\n"
    # 削除ファイルが削除されていること
    assert not (company / "file_del.txt").exists()


def test_single_repository_commit_pending(tmp_path: Path, git_helpers) -> None:
    """特定のリポジトリIDを指定して個別コミットが正しく完了するテスト"""
    git, init_repo = git_helpers
    source = init_repo(tmp_path / "source")
    (source / "app.txt").write_text("v1\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "v1")
    base = git(source, "rev-parse", "HEAD").decode().strip()

    (source / "app.txt").write_text("v2\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "v2")
    target = git(source, "rev-parse", "HEAD").decode().strip()

    company_root = tmp_path / "company-apps"
    company = init_repo(company_root / "sample-app")
    (company / "app.txt").write_text("v1\n", encoding="utf-8")
    git(company, "add", "-A")
    git(company, "commit", "-m", "company v1")

    package = make_package_repo(tmp_path / "package", source, git, [(base, target)])
    settings = Settings(
        mode="company",
        company_apps_root=str(company_root),
        download_dir=str(package.parent),
        patch_password=PASSWORD,
    )

    # 適用（即時コミットされる）
    result = apply_archive(settings, str(package))
    assert result["failed"] == 0
    state = load_state(company, "sample-app")
    assert state["confirmed_sequence"] == 1
    assert git(company, "status", "--porcelain").decode() == ""

    # 未コミットの手動変更を作成
    (company / "app.txt").write_text("v3-manual\n", encoding="utf-8")
    assert git(company, "status", "--porcelain").decode() != ""

    # 個別コミット実行
    commit_res = commit_pending(settings, repo_id="sample-app")
    assert len(commit_res["results"]) == 1
    assert commit_res["results"][0]["status"] == "committed"
    assert commit_res["results"][0]["repo_id"] == "sample-app"

    # コミット後状態確認（未コミット変更がクリーンになっていること）
    state_after = load_state(company, "sample-app")
    assert state_after["confirmed_sequence"] == 1
    assert git(company, "status", "--porcelain").decode() == ""
    log = git(company, "log", "-1", "--pretty=%s").decode().strip()
    assert log == "[myao-patch] sample-app update"


def test_list_company_repositories(tmp_path: Path, git_helpers) -> None:
    """会社側のリポジトリ一覧・状態を取得するテスト"""
    git, init_repo = git_helpers
    company_root = tmp_path / "company-apps"
    app1 = init_repo(company_root / "app-one")
    (app1 / "file.txt").write_text("app1\n", encoding="utf-8")
    git(app1, "add", "-A")
    git(app1, "commit", "-m", "app1 init")

    obsidian = init_repo(tmp_path / "company-obsidian")
    (obsidian / "note.md").write_text("obsidian\n", encoding="utf-8")
    git(obsidian, "add", "-A")
    git(obsidian, "commit", "-m", "obsidian init")

    settings = Settings(
        mode="company",
        company_apps_root=str(company_root),
        company_obsidian_repo=str(obsidian),
        patch_password=PASSWORD,
    )

    repos = list_company_repositories(settings)
    repo_ids = [r["repo_id"] for r in repos]
    assert "app-one" in repo_ids
    assert "obsidian-settings" in repo_ids
    for r in repos:
        assert "clean" in r
        assert "head" in r


def test_init_repository_sequence_and_apply_from_sequence_two(tmp_path: Path, git_helpers) -> None:
    """適用開始番号を指定してstate.jsonを自動生成し、連番2からパッチを適用できるテスト"""
    git, init_repo = git_helpers

    # 自宅側: commit0 -> commit1 (連番1) -> commit2 (連番2) を作成
    home = init_repo(tmp_path / "home-app")
    (home / "file1.txt").write_text("v0\n", encoding="utf-8")
    git(home, "add", "-A")
    git(home, "commit", "-m", "c0")
    c0 = git(home, "rev-parse", "HEAD").decode().strip()

    (home / "file1.txt").write_text("v1\n", encoding="utf-8")
    git(home, "add", "-A")
    git(home, "commit", "-m", "c1")
    c1 = git(home, "rev-parse", "HEAD").decode().strip()

    (home / "file2.txt").write_text("v2-new\n", encoding="utf-8")
    git(home, "add", "-A")
    git(home, "commit", "-m", "c2")
    c2 = git(home, "rev-parse", "HEAD").decode().strip()

    # パッチZIP作成 (連番1: c0->c1, 連番2: c1->c2)
    zip_path = make_package_repo(tmp_path / "patch_out", home, git, [(c0, c1), (c1, c2)])

    # 会社側: c1の状態と同じファイル構成で初期コミット (連番1適用済み相当)
    company_root = tmp_path / "company-apps"
    company = init_repo(company_root / "sample-app")
    (company / "file1.txt").write_text("v1\n", encoding="utf-8")
    git(company, "add", "-A")
    git(company, "commit", "-m", "base matches c1")

    settings = Settings(
        mode="company",
        company_apps_root=str(company_root),
        download_dir=str(zip_path.parent),
        patch_password=PASSWORD,
    )

    # 最初は state.json が存在しないことを確認
    state_file = company / ".git" / "rep-patch" / "state.json"
    assert not state_file.exists()

    # 不正な開始連番（0以下）はエラーになることを検証
    with pytest.raises(RepPatchError):
        init_repository_sequence(settings, "sample-app", start_sequence=0)

    # 適用開始番号を 2 に指定して初期化
    res = init_repository_sequence(settings, "sample-app", start_sequence=2)
    assert res["repo_id"] == "sample-app"
    assert res["start_sequence"] == 2
    assert res["confirmed_sequence"] == 1

    # state.json が自動生成され、confirmed_sequence が 1 になっていることを確認
    assert state_file.is_file()
    state = load_state(company, "sample-app")
    assert state["confirmed_sequence"] == 1

    # パッチZIPを適用 -> 連番1はスキップされ、連番2のみが適用・コミットされる
    apply_res = apply_archive(settings, str(zip_path))
    assert apply_res["failed"] == 0
    assert apply_res["succeeded"] == 1
    result_item = apply_res["results"][0]
    assert result_item["status"] == "applied"
    assert result_item["confirmed_sequence"] == 2

    # 作業ツリーがクリーンであり、連番2の新規ファイルが存在することを確認
    assert git(company, "status", "--porcelain").decode().strip() == ""
    assert (company / "file2.txt").read_text(encoding="utf-8") == "v2-new\n"
    state_after = load_state(company, "sample-app")
    assert state_after["confirmed_sequence"] == 2


def test_init_all_repositories_sequence(tmp_path: Path, git_helpers) -> None:
    """全リポジトリ一括で適用開始番号を指定してstate.jsonを生成できるテスト"""
    git, init_repo = git_helpers
    company_root = tmp_path / "company-apps"
    app1 = init_repo(company_root / "app-one")
    (app1 / "file.txt").write_text("app1\n", encoding="utf-8")
    git(app1, "add", "-A")
    git(app1, "commit", "-m", "app1 init")

    app2 = init_repo(company_root / "app-two")
    (app2 / "file.txt").write_text("app2\n", encoding="utf-8")
    git(app2, "add", "-A")
    git(app2, "commit", "-m", "app2 init")

    settings = Settings(
        mode="company",
        company_apps_root=str(company_root),
        patch_password=PASSWORD,
    )

    # 一括初期化（開始番号 3 -> confirmed_sequence = 2）
    results = init_all_repositories_sequence(settings, start_sequence=3)
    assert len(results) == 2
    for item in results:
        assert item["start_sequence"] == 3
        assert item["confirmed_sequence"] == 2

    # 各リポジトリに state.json が作成されたことを確認
    for app in [app1, app2]:
        state_file = app / ".git" / "rep-patch" / "state.json"
        assert state_file.is_file()
        state = load_state(app, app.name)
        assert state["confirmed_sequence"] == 2


def test_apply_auto_commits_uncommitted_changes_and_immediately_commits_patch(
    tmp_path: Path, git_helpers
) -> None:
    """
    会社側に未コミット変更（手動編集や残骸）がある状態からパッチを適用した際、
    既存変更が自動コミットされ、さらにパッチ自体も即座にコミットされて作業ツリーがクリーンになるテスト
    """
    git, init_repo = git_helpers

    # 自宅側: base -> patch1
    home = init_repo(tmp_path / "home-app")
    (home / "app.txt").write_text("v0\n", encoding="utf-8")
    git(home, "add", "-A")
    git(home, "commit", "-m", "c0")
    c0 = git(home, "rev-parse", "HEAD").decode().strip()

    (home / "app.txt").write_text("v1-patch\n", encoding="utf-8")
    (home / "added.txt").write_text("new file\n", encoding="utf-8")
    git(home, "add", "-A")
    git(home, "commit", "-m", "c1")
    c1 = git(home, "rev-parse", "HEAD").decode().strip()

    zip_path = make_package_repo(tmp_path / "patch_out", home, git, [(c0, c1)])

    # 会社側: base を作成後、手動でファイルを未コミット変更（または未追跡ファイル）として追加
    company_root = tmp_path / "company-apps"
    company = init_repo(company_root / "sample-app")
    (company / "app.txt").write_text("v0\n", encoding="utf-8")
    git(company, "add", "-A")
    git(company, "commit", "-m", "company base")

    # 未コミットの変更を作成
    (company / "app.txt").write_text("v0-manual-edit\n", encoding="utf-8")
    status_before = git(company, "status", "--porcelain").decode().strip()
    assert status_before != ""  # 未コミットの変更が存在する

    settings = Settings(
        mode="company",
        company_apps_root=str(company_root),
        download_dir=str(zip_path.parent),
        patch_password=PASSWORD,
    )

    # パッチ適用を実行
    res = apply_archive(settings, str(zip_path))
    assert res["failed"] == 0
    assert res["succeeded"] == 1

    # 検証1: パッチ適用直後に作業ツリーが完全にクリーンであること（未コミット変更が0件）
    status_after = git(company, "status", "--porcelain").decode().strip()
    assert status_after == ""

    # 検証2: 手動変更が事前にコミットされ、パッチもコミットされていること
    logs = git(company, "log", "-2", "--pretty=%s").decode().strip().splitlines()
    assert logs[0] == "[myao-patch] sample-app patch-000001"
    assert logs[1] == "[myao-patch] sample-app update"

    # 検証3: パッチ内容が正しく反映されていること
    assert (company / "app.txt").read_text(encoding="utf-8") == "v1-patch\n"
    assert (company / "added.txt").read_text(encoding="utf-8") == "new file\n"

    # 検証4: state.json の confirmed_sequence が 1 であること
    state = load_state(company, "sample-app")
    assert state["confirmed_sequence"] == 1


def test_apply_added_files_with_crlf_lf_conversion(
    tmp_path: Path, git_helpers
) -> None:
    """
    Obsidianプラグイン等の追加ファイルにおいて、マニフェスト署名時点（自宅側CRLF）と
    ZIP内ファイル実体（GitやアーカイブによるLF変換）で改行コード差異が発生しても、
    破損エラーと誤判定されず透過的に検証および元改行コード復元・適用されるテスト
    """
    git, init_repo = git_helpers

    # 自宅側: base -> patch1
    home = init_repo(tmp_path / "home-obsidian")
    (home / "app.txt").write_text("v0\n", encoding="utf-8")
    git(home, "add", "-A")
    git(home, "commit", "-m", "base")
    c0 = git(home, "rev-parse", "HEAD").decode().strip()

    # プラグインの manifest.json を CRLF で作成
    plugin_dir = home / ".obsidian" / "plugins" / "sample-plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest_content = '{\r\n  "id": "sample-plugin",\r\n  "version": "1.0.0"\r\n}\r\n'.encode("utf-8")
    (plugin_dir / "manifest.json").write_bytes(manifest_content)

    git(home, "add", "-A")
    git(home, "commit", "-m", "add plugin")
    c1 = git(home, "rev-parse", "HEAD").decode().strip()

    # 自宅側パッチZIPを作成（マニフェストには CRLF のサイズとハッシュが記録される）
    raw_zip_path = make_package_repo(tmp_path / "patch_raw", home, git, [(c0, c1)])

    # ZIP内の一部の追加ファイルを LF (\n) に変換した改変ZIPを模倣作成
    # （Git の *.json text eol=lf や GitHub Download ZIP 等による改行コード正規化を再現）
    modified_zip_path = tmp_path / "myao_app_patch-main.zip"
    with zipfile.ZipFile(raw_zip_path, "r") as src_zf, zipfile.ZipFile(
        modified_zip_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as dst_zf:
        for item in src_zf.infolist():
            data = src_zf.read(item.filename)
            if "sample-plugin/manifest.json" in item.filename:
                # CRLF -> LF に変換
                data = data.replace(b"\r\n", b"\n")
            dst_zf.writestr(item, data)

    # 会社側
    company_root = tmp_path / "company-apps"
    company = init_repo(company_root / "sample-app")
    (company / "app.txt").write_text("v0\n", encoding="utf-8")
    git(company, "add", "-A")
    git(company, "commit", "-m", "company base")

    settings = Settings(
        mode="company",
        company_apps_root=str(company_root),
        download_dir=str(tmp_path),
        patch_password=PASSWORD,
    )

    # 1. inspect_archive が「追加ファイルが破損しています」で落ちずに検証成功すること
    inspected = inspect_archive(settings, str(modified_zip_path))
    assert inspected["package_count"] == 1
    assert inspected["repository_count"] == 1

    # 2. apply_archive でパッチが正常に適用・コミットされること
    res = apply_archive(settings, str(modified_zip_path))
    assert res["failed"] == 0
    assert res["succeeded"] == 1

    # 3. 会社側のプラグイン manifest.json が元の CRLF のバイト列として正しく配置されていること
    applied_file = company / ".obsidian" / "plugins" / "sample-plugin" / "manifest.json"
    assert applied_file.exists()
    assert applied_file.read_bytes() == manifest_content


def _source_with_history(tmp_path: Path, git, init_repo, versions: list[str]) -> tuple[Path, list[str]]:
    """app.txt を versions の順に書き換えてコミットした自宅側リポジトリと、各コミットIDを返す"""
    source = init_repo(tmp_path / "source")
    commits = []
    for index, content in enumerate(versions):
        (source / "app.txt").write_text(content, encoding="utf-8")
        git(source, "add", "-A")
        git(source, "commit", "-m", f"v{index}")
        commits.append(git(source, "rev-parse", "HEAD").decode().strip())
    return source, commits


def _company_repo(tmp_path: Path, git, init_repo, content: str, name: str = "sample-app") -> Path:
    company = init_repo(tmp_path / "company-apps" / name)
    (company / "app.txt").write_text(content, encoding="utf-8")
    git(company, "add", "-A")
    git(company, "commit", "-m", "company base")
    return company


def _company_settings(tmp_path: Path, zip_path: Path) -> Settings:
    return Settings(
        mode="company",
        company_apps_root=str(tmp_path / "company-apps"),
        download_dir=str(zip_path.parent),
        patch_password=PASSWORD,
    )


def test_apply_patch_whose_content_already_matches_advances_sequence(tmp_path: Path, git_helpers) -> None:
    """会社側で同じ修正を手で入れてコミット済みでも、以降のパッチが止まらない"""
    git, init_repo = git_helpers
    source, (c0, c1, c2) = _source_with_history(tmp_path, git, init_repo, ["v0\n", "v1\n", "v2\n"])
    company = _company_repo(tmp_path, git, init_repo, "v1\n")  # 連番1の内容を既に持っている
    head_before = git(company, "rev-parse", "HEAD")

    first = make_package_repo(tmp_path / "first", source, git, [(c0, c1)])
    settings = _company_settings(tmp_path, first)
    result = apply_archive(settings, str(first))

    assert result["failed"] == 0
    assert result["results"][0]["status"] == "applied"
    assert "既に一致" in result["results"][0]["message"]
    assert load_state(company, "sample-app")["confirmed_sequence"] == 1
    assert git(company, "rev-parse", "HEAD") == head_before  # 空のコミットは作らない
    assert git(company, "status", "--porcelain").decode() == ""

    # 続く連番2は通常どおり適用・コミットされる
    second = make_package_repo(tmp_path / "second", source, git, [(c0, c1), (c1, c2)])
    settings.download_dir = str(second.parent)
    result = apply_archive(settings, str(second))
    assert result["results"][0]["status"] == "applied"
    assert (company / "app.txt").read_text(encoding="utf-8") == "v2\n"
    assert git(company, "log", "-1", "--pretty=%s").decode().strip() == "[myao-patch] sample-app patch-000002"


@pytest.mark.parametrize("fail_after_commit", [False, True])
def test_failed_apply_restores_pre_apply_commit_and_files(
    tmp_path: Path, git_helpers, monkeypatch: pytest.MonkeyPatch, fail_after_commit: bool
) -> None:
    git, init_repo = git_helpers
    source = init_repo(tmp_path / "source")
    (source / "app.txt").write_text("v0\n", encoding="utf-8")
    (source / "gone.txt").write_text("to be deleted\n", encoding="utf-8")
    (source / ".gitignore").write_text("local.json\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "v0")
    c0 = git(source, "rev-parse", "HEAD").decode().strip()
    (source / "app.txt").write_text("v1\n", encoding="utf-8")
    (source / "gone.txt").unlink()
    (source / "new").mkdir()
    (source / "new" / "added.txt").write_text("added\n", encoding="utf-8")
    (source / ".gitignore").write_text("", encoding="utf-8")
    (source / "local.json").write_text("{\"from\": \"home\"}\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "v1")
    c1 = git(source, "rev-parse", "HEAD").decode().strip()

    company = init_repo(tmp_path / "company-apps" / "sample-app")
    (company / "app.txt").write_text("v0\n", encoding="utf-8")
    (company / "gone.txt").write_text("to be deleted\n", encoding="utf-8")
    (company / ".gitignore").write_text("local.json\n", encoding="utf-8")
    git(company, "add", "-A")
    git(company, "commit", "-m", "company base")
    # 会社側ではGit管理外（.gitignore対象）のローカル設定。パッチが同じパスを上書きしようとする
    (company / "local.json").write_text("{\"from\": \"company\"}\n", encoding="utf-8")
    head_before = git(company, "rev-parse", "HEAD")

    zip_path = make_package_repo(tmp_path / "package", source, git, [(c0, c1)])
    settings = _company_settings(tmp_path, zip_path)

    with monkeypatch.context() as patched:
        if fail_after_commit:
            # コミット成功後の最終確認で失敗させる（1回目は適用前の確認なので通す）
            calls = {"count": 0}
            original = company_module._ensure_clean

            def failing_ensure_clean(repo: Path) -> None:
                calls["count"] += 1
                if calls["count"] >= 2:
                    raise RepPatchError("simulated failure after commit")
                original(repo)

            patched.setattr(company_module, "_ensure_clean", failing_ensure_clean)
        else:
            # ファイル配置後、コミットの時点で失敗させる
            def failing_commit(repo: Path, message: str) -> bool:
                raise RepPatchError("simulated failure before commit")

            patched.setattr(company_module, "_commit_staged", failing_commit)

        result = apply_archive(settings, str(zip_path))

    assert result["failed"] == 1
    assert "simulated failure" in result["results"][0]["message"]
    assert git(company, "rev-parse", "HEAD") == head_before
    assert (company / "app.txt").read_text(encoding="utf-8") == "v0\n"
    assert (company / "gone.txt").read_text(encoding="utf-8") == "to be deleted\n"
    assert not (company / "new" / "added.txt").exists()
    assert (company / "local.json").read_text(encoding="utf-8") == "{\"from\": \"company\"}\n"
    assert git(company, "status", "--porcelain").decode() == ""
    assert load_state(company, "sample-app")["confirmed_sequence"] == 0
    backups = company / ".git" / "rep-patch" / "backups"
    assert not backups.exists() or list(backups.iterdir()) == []

    # 原因が解消されれば、同じZIPをそのまま再適用できる
    result = apply_archive(settings, str(zip_path))
    assert result["failed"] == 0
    assert (company / "app.txt").read_text(encoding="utf-8") == "v1\n"
    assert not (company / "gone.txt").exists()


def test_apply_reports_missing_sequence(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    source, (c0, c1, c2) = _source_with_history(tmp_path, git, init_repo, ["v0\n", "v1\n", "v2\n"])
    company = _company_repo(tmp_path, git, init_repo, "v0\n")
    zip_path = make_package_repo(tmp_path / "package", source, git, [(c0, c1), (c1, c2)])
    settings = _company_settings(tmp_path, zip_path)

    # 会社側は連番1が未適用なのに、連番2しか含まないZIPを作る
    only_second = tmp_path / "only-second"
    only_second.mkdir()
    partial = only_second / "myao_app_patch-main.zip"
    with zipfile.ZipFile(zip_path) as src, zipfile.ZipFile(partial, "w") as dst:
        index_name = next(name for name in src.namelist() if name.endswith("package-index.json"))
        index = json.loads(src.read(index_name))
        index["packages"] = [item for item in index["packages"] if item["sequence"] == 2]
        signed = sign_document({k: v for k, v in index.items() if k != "signature"}, PASSWORD)
        dst.writestr(index_name, json.dumps(signed))
        for name in src.namelist():
            if "/000002/" in name:
                dst.writestr(name, src.read(name))
    settings.download_dir = str(only_second)

    result = apply_archive(settings, str(partial))
    assert result["failed"] == 1
    assert "必要: 000001、検出: 000002" in result["results"][0]["message"]
    assert (company / "app.txt").read_text(encoding="utf-8") == "v0\n"


def test_legacy_pending_state_is_committed_as_confirmed(tmp_path: Path, git_helpers) -> None:
    """旧バージョン（適用後に未コミットで保持する方式）の state.json からでも続きの連番を適用できる"""
    git, init_repo = git_helpers
    source, (c0, c1, c2) = _source_with_history(tmp_path, git, init_repo, ["v0\n", "v1\n", "v2\n"])
    company = _company_repo(tmp_path, git, init_repo, "v0\n")
    # 旧バージョンが連番1を適用し、未コミットのまま残した状態
    (company / "app.txt").write_text("v1\n", encoding="utf-8")
    save_state(
        company,
        {
            "version": 1,
            "repo_id": "sample-app",
            "confirmed_sequence": 0,
            "pending_sequences": [1],
            "pending_target_files": [],
            "pending_changed_paths": ["app.txt"],
            "pre_apply_head": "",
            "backup_dir": "",
        },
    )
    zip_path = make_package_repo(tmp_path / "package", source, git, [(c0, c1), (c1, c2)])
    settings = _company_settings(tmp_path, zip_path)

    result = apply_archive(settings, str(zip_path))

    assert result["failed"] == 0
    assert result["results"][0]["confirmed_sequence"] == 2
    logs = git(company, "log", "-2", "--pretty=%s").decode().strip().splitlines()
    assert logs == ["[myao-patch] sample-app patch-000002", "[myao-patch] sample-app patch-000001"]
    state = load_state(company, "sample-app")
    assert state["confirmed_sequence"] == 2
    assert "pending_sequences" not in state


def test_excluded_repository_is_skipped_but_can_be_applied_individually(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    source, (c0, c1) = _source_with_history(tmp_path, git, init_repo, ["v0\n", "v1\n"])
    company = _company_repo(tmp_path, git, init_repo, "v0\n")
    zip_path = make_package_repo(tmp_path / "package", source, git, [(c0, c1)])
    settings = _company_settings(tmp_path, zip_path)
    settings.company_excluded_repo_ids = ["sample-app"]

    assert list_company_repositories(settings) == []
    result = apply_archive(settings, str(zip_path))
    assert result["results"][0]["status"] == "unchanged"
    assert "除外" in result["results"][0]["message"]
    assert (company / "app.txt").read_text(encoding="utf-8") == "v0\n"

    result = apply_archive(settings, str(zip_path), only_repo_id="sample-app")
    assert result["results"][0]["status"] == "applied"
    assert (company / "app.txt").read_text(encoding="utf-8") == "v1\n"

    # 2回目は新しいパッチが無い
    result = apply_archive(settings, str(zip_path), only_repo_id="sample-app")
    assert result["results"][0]["status"] == "unchanged"


def test_retry_for_repository_not_in_archive_reports_unchanged(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    source, (c0, c1) = _source_with_history(tmp_path, git, init_repo, ["v0\n", "v1\n"])
    _company_repo(tmp_path, git, init_repo, "v0\n")
    zip_path = make_package_repo(tmp_path / "package", source, git, [(c0, c1)])
    settings = _company_settings(tmp_path, zip_path)

    result = apply_archive(settings, str(zip_path), only_repo_id="other-app")
    assert [item["status"] for item in result["results"]] == ["unchanged"]
    assert result["results"][0]["repo_id"] == "other-app"
    assert "含まれていません" in result["results"][0]["message"]


def test_download_listing_and_zip_location_guard(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    source, (c0, c1) = _source_with_history(tmp_path, git, init_repo, ["v0\n", "v1\n"])
    zip_path = make_package_repo(tmp_path / "downloads", source, git, [(c0, c1)])
    (zip_path.parent / "unrelated.zip").write_bytes(b"PK")
    (zip_path.parent / "myao_app_patch_notes.txt").write_text("not a zip", encoding="utf-8")
    settings = _company_settings(tmp_path, zip_path)

    assert [item["name"] for item in list_download_packages(settings)] == ["myao_app_patch-main.zip"]
    settings.download_dir = str(tmp_path / "missing")
    assert list_download_packages(settings) == []

    # ダウンロードフォルダの外にあるZIPや、ZIP以外は受け付けない
    settings.download_dir = str(tmp_path / "elsewhere")
    (tmp_path / "elsewhere").mkdir()
    with pytest.raises(PackageValidationError, match="ダウンロードフォルダ内"):
        inspect_archive(settings, str(zip_path))
    settings.download_dir = str(zip_path.parent)
    with pytest.raises(PackageValidationError, match="ZIPが見つかりません"):
        inspect_archive(settings, str(zip_path.parent / "myao_app_patch_notes.txt"))
    with pytest.raises(PackageValidationError, match="ZIPを開けません"):
        inspect_archive(settings, str(zip_path.parent / "unrelated.zip"))


def test_inspect_reports_unmapped_repository(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    source, (c0, c1) = _source_with_history(tmp_path, git, init_repo, ["v0\n", "v1\n"])
    zip_path = make_package_repo(tmp_path / "package", source, git, [(c0, c1)])
    (tmp_path / "company-apps").mkdir()
    settings = _company_settings(tmp_path, zip_path)

    summary = inspect_archive(settings, str(zip_path))
    assert summary["repositories"][0]["company_path"] == ""
    assert "会社側リポジトリが見つかりません" in summary["repositories"][0]["mapping_error"]

    result = apply_archive(settings, str(zip_path))
    assert result["failed"] == 1
