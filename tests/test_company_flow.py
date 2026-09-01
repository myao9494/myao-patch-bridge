"""
会社環境でのパッチZIP検証・適用・バックアップ・コミットフローのテスト

仕様:
- test_independent_git_history_apply_then_auto_commit: 独立Git履歴へのパッチ適用・新規ファイル自動配置・削除ファイル反映・自動コミット検証
- test_unexpected_files_fails_without_changing_company_files: 想定外ファイル存在時のロールバック検証
- test_apply_with_added_and_deleted_files_and_rollback: 新規ファイル自動配置と削除ファイル処理および失敗時の完全復元検証
- test_force_overwrite_and_delete_applies_directly: git applyを使わずファイル直接上書き配置と削除で同期するテスト
- test_single_repository_commit_pending: リポジトリID指定での個別コミット検証
- test_list_company_repositories: 会社側リポジトリ一覧および状態取得の検証
- test_init_repository_sequence_and_apply_from_sequence_two: 適用開始番号指定によるstate.json生成と連番スキップ適用の検証
- test_init_all_repositories_sequence: 全リポジトリ一括での適用開始番号初期化・state.json生成検証
- test_apply_auto_commits_uncommitted_changes_and_immediately_commits_patch: 未コミット変更の自動事前コミットとパッチ適用の即時確定・クリーン化検証
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from rep_patch.company import (
    apply_archive,
    commit_pending,
    init_all_repositories_sequence,
    init_repository_sequence,
    list_company_repositories,
    load_state,
)
from rep_patch.config import Settings
from rep_patch.errors import RepPatchError
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
    assert state["pending_sequences"] == []

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
    assert state["pending_sequences"] == []


def test_unexpected_files_fails_without_changing_company_files(tmp_path: Path, git_helpers) -> None:
    """想定外のファイル差分がある場合は適用が失敗し、会社側ファイルが元通り復元されるテスト"""
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
    (company / "unexpected_extra.txt").write_text("unexpected\n", encoding="utf-8")
    git(company, "add", "-A")
    git(company, "commit", "-m", "company base with extra file")
    before = git(company, "rev-parse", "HEAD").decode().strip()

    package = make_package_repo(tmp_path / "package", source, git, [(base, target)])
    settings = Settings(
        mode="company",
        company_apps_root=str(company_root),
        download_dir=str(package.parent),
        patch_password=PASSWORD,
    )
    result = apply_archive(settings, str(package))
    assert result["failed"] == 1
    assert (company / "app.txt").read_text(encoding="utf-8") == "expected base\n"
    assert (company / "unexpected_extra.txt").read_text(encoding="utf-8") == "unexpected\n"
    assert git(company, "rev-parse", "HEAD").decode().strip() == before
    assert git(company, "status", "--porcelain").decode() == ""


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
    """git applyを使わずにファイルを直接強制上書き・新規配置・削除して未コミット状態にするテスト"""
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
    assert state["pending_sequences"] == []
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
    assert state_after["pending_sequences"] == []
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
        assert "pending_sequences" in r
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
    assert state["pending_sequences"] == []

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
    assert state_after["pending_sequences"] == []


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

    # 未コミットの変更を作成（state.json には pending はない状態）
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

    # 検証4: state.json が confirmed_sequence: 1, pending: [] であること
    state = load_state(company, "sample-app")
    assert state["confirmed_sequence"] == 1
    assert state["pending_sequences"] == []





