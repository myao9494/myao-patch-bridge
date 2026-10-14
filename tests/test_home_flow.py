"""
自宅側でのパッチ生成・公開フローおよびリセット機能のテスト

仕様:
- test_publish_creates_package_pushes_and_advances_cursor: 差分パッチ作成・新規ファイル実体同梱・削除記録・署名・push・カーソル進行の検証
- test_reset_repository_patches_clears_packages_and_resets_sequence: パッチ履歴リセットによりパッチ専用リポジトリの該当パッケージ削除・インデックス再署名・push・次回000001再生成の検証
- test_publish_creates_release_bundle_and_uploads_to_github: 今回分軽量ZIP生成およびGitHub Releases自動公開の検証
- test_publish_cleans_old_packages_keeps_only_latest_and_tracks_sequence: 過去パッチの自動クリーンアップ・リポジトリ軽量化・last_sequence保持の検証
- test_publish_without_changes_leaves_patch_repo_clean: 変更が無い公開操作でもパッチ専用リポジトリを汚さず、次回の公開を妨げない検証
- test_publish_failure_leaves_patch_repo_clean: 設定不備や書き出し途中の失敗で作りかけのパッケージを残さない検証
- test_publish_push_failure_is_recovered_on_next_publish: push失敗後、次回の公開操作でpushと公開済み地点が回復する検証
- test_publish_rejects_unsupported_entries: サブモジュールを含む差分は書き出し前に拒否する検証
- test_publish_reports_bundle_failure_without_failing: 今回分ZIPの生成失敗が公開結果を失敗にしない検証
- test_publish_writes_bundle_into_patch_repo_dist_when_download_dir_is_empty: 出力先未設定時に dist/ へ出力し、次回公開を妨げない検証
- test_publish_then_apply_end_to_end: 実際の公開物（リネーム・バイナリ・日本語パス・削除を含む）を会社側へ適用する通し検証
- test_reset_requires_clean_patch_repo: パッチ専用リポジトリに未コミットの変更がある場合はリセットしない検証
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

import rep_patch.home as home_module
from helpers import commit_all, git, init_repo, make_patch_repo
from rep_patch.company import apply_archive, load_state
from rep_patch.config import RepositoryConfig, Settings, SettingsStore
from rep_patch.errors import RepPatchError
from rep_patch.home import publish, reset_repository_patches
from rep_patch.security import verify_document


def test_publish_creates_package_pushes_and_advances_cursor(tmp_path: Path, git_helpers) -> None:
    git, init_repo = git_helpers
    source = init_repo(tmp_path / "source" / "sample-app")
    git(source, "branch", "-M", "main")
    (source / "app.txt").write_text("base\n", encoding="utf-8")
    (source / "to_delete.txt").write_text("will be deleted\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "base")
    baseline = git(source, "rev-parse", "HEAD").decode().strip()

    (source / "app.txt").write_text("published change\n", encoding="utf-8")
    (source / "to_delete.txt").unlink()
    nested_dir = source / "nested" / "sub"
    nested_dir.mkdir(parents=True)
    (nested_dir / "new_file.txt").write_text("new nested content\n", encoding="utf-8")
    (source / "binary.bin").write_bytes(b"\x00\x01\x02\xff")
    git(source, "add", "-A")
    git(source, "commit", "-m", "change with add and delete")
    target = git(source, "rev-parse", "HEAD").decode().strip()

    remote = tmp_path / "patch-remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    patch_repo = init_repo(tmp_path / "myao_app_patch")
    git(patch_repo, "branch", "-M", "main")
    (patch_repo / "README.md").write_text("patches\n", encoding="utf-8")
    git(patch_repo, "add", "-A")
    git(patch_repo, "commit", "-m", "initial")
    git(patch_repo, "remote", "add", "origin", str(remote))
    git(patch_repo, "push", "-u", "origin", "main")

    settings_path = tmp_path / "data" / "settings.local.json"
    store = SettingsStore(settings_path)
    settings = Settings(
        mode="home",
        patch_repo=str(patch_repo),
        patch_password="test-password",
        repositories={
            "sample-app": RepositoryConfig(
                repo_id="sample-app",
                display_name="sample-app",
                path=str(source),
                branch="main",
                baseline_commit=baseline,
            )
        },
    )
    store.save(settings)

    result = publish(settings, store)
    assert result["published"] is True
    package_dir = patch_repo / "packages" / "sample-app" / "000001"
    manifest_path = package_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_document(manifest, "test-password")
    assert manifest["source_from_commit"] == baseline
    assert manifest["source_to_commit"] == target
    assert manifest["chunks"]
    assert store.load().repositories["sample-app"].published_commit == target
    assert git(patch_repo, "rev-list", "--count", "@{u}..HEAD").decode().strip() == "0"

    # 差分ファイル（新規追加および変更ファイル）の実体が同梱されているか検証
    assert "added_files" in manifest
    added_paths = [item["path"] for item in manifest["added_files"]]
    assert "app.txt" in added_paths
    assert "nested/sub/new_file.txt" in added_paths
    assert "binary.bin" in added_paths
    assert (package_dir / "added_files" / "app.txt").read_text(
        encoding="utf-8"
    ) == "published change\n"
    assert (package_dir / "added_files" / "nested" / "sub" / "new_file.txt").read_text(
        encoding="utf-8"
    ) == "new nested content\n"
    assert (package_dir / "added_files" / "binary.bin").read_bytes() == b"\x00\x01\x02\xff"

    # 削除ファイルが記録されているか検証
    assert "deleted_files" in manifest
    assert "to_delete.txt" in manifest["deleted_files"]

    # パッチ専用リポジトリの .gitattributes に packages/** binary が設定され、改行コード変換が防止されているか検証
    gitattributes_content = (patch_repo / ".gitattributes").read_text(encoding="utf-8")
    assert "packages/** binary" in gitattributes_content


def test_reset_repository_patches_clears_packages_and_resets_sequence(
    tmp_path: Path, git_helpers
) -> None:
    git, init_repo = git_helpers
    source = init_repo(tmp_path / "source" / "sample-app")
    git(source, "branch", "-M", "main")
    (source / "app.txt").write_text("base\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "base")
    baseline = git(source, "rev-parse", "HEAD").decode().strip()

    (source / "app.txt").write_text("change 1\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "commit 1")

    remote = tmp_path / "patch-remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    patch_repo = init_repo(tmp_path / "myao_app_patch")
    git(patch_repo, "branch", "-M", "main")
    (patch_repo / "README.md").write_text("patches\n", encoding="utf-8")
    git(patch_repo, "add", "-A")
    git(patch_repo, "commit", "-m", "initial")
    git(patch_repo, "remote", "add", "origin", str(remote))
    git(patch_repo, "push", "-u", "origin", "main")

    settings_path = tmp_path / "data" / "settings.local.json"
    store = SettingsStore(settings_path)
    settings = Settings(
        mode="home",
        patch_repo=str(patch_repo),
        patch_password="test-password",
        repositories={
            "sample-app": RepositoryConfig(
                repo_id="sample-app",
                display_name="sample-app",
                path=str(source),
                branch="main",
                baseline_commit=baseline,
            )
        },
    )
    store.save(settings)

    # 1回目のパッチ公開（000001）
    res1 = publish(settings, store)
    assert res1["published"] is True
    assert (patch_repo / "packages" / "sample-app" / "000001").exists()

    # 2回目の変更と公開（000002）
    (source / "app.txt").write_text("change 2\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "commit 2")
    res2 = publish(settings, store)
    assert res2["published"] is True
    # パッチリポジトリの index に最新の 1件（000002）のみが入っていることを確認（過去パッチは軽量化のため削除）
    index1 = json.loads((patch_repo / "package-index.json").read_text(encoding="utf-8"))
    assert len([p for p in index1["packages"] if p["repo_id"] == "sample-app"]) == 1
    assert not (patch_repo / "packages" / "sample-app" / "000001").exists()

    # パッチ履歴リセットを実行
    reset_res = reset_repository_patches(settings, store, "sample-app")
    assert reset_res["reset"] is True
    assert reset_res["repo_id"] == "sample-app"

    # パッチ専用リポジトリのディレクトリが消去されていること
    assert not (patch_repo / "packages" / "sample-app").exists()

    # package-index.json から除外され、再署名されていること
    index2 = json.loads((patch_repo / "package-index.json").read_text(encoding="utf-8"))
    assert len([p for p in index2["packages"] if p["repo_id"] == "sample-app"]) == 0
    verify_document(index2, "test-password")

    # パッチ専用リポジトリでコミット＆pushされていること
    assert git(patch_repo, "rev-list", "--count", "@{u}..HEAD").decode().strip() == "0"

    # 設定ファイルの published_commit がクリアされていること
    loaded_config = store.load().repositories["sample-app"]
    assert loaded_config.published_commit == ""

    # 再び変更を加えて公開すると、連番 000001 で作成されること
    (source / "app.txt").write_text("fresh start 000001\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "fresh start")
    fresh_target = git(source, "rev-parse", "HEAD").decode().strip()

    res3 = publish(settings, store)
    assert res3["published"] is True
    package_dir = patch_repo / "packages" / "sample-app" / "000001"
    assert package_dir.exists()
    assert not (patch_repo / "packages" / "sample-app" / "000002").exists()

    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sequence"] == 1
    assert manifest["source_from_commit"] == baseline
    assert manifest["source_to_commit"] == fresh_target


def test_publish_creates_release_bundle_and_uploads_to_github(
    tmp_path: Path, git_helpers, monkeypatch
) -> None:
    git, init_repo = git_helpers
    source = init_repo(tmp_path / "source" / "sample-app")
    git(source, "branch", "-M", "main")
    (source / "app.txt").write_text("v1\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "v1")
    baseline = git(source, "rev-parse", "HEAD").decode().strip()

    (source / "app.txt").write_text("v2\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "v2")

    remote = tmp_path / "patch-remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    patch_repo = init_repo(tmp_path / "myao_app_patch")
    git(patch_repo, "branch", "-M", "main")
    (patch_repo / "README.md").write_text("patches\n", encoding="utf-8")
    git(patch_repo, "add", "-A")
    git(patch_repo, "commit", "-m", "initial")
    git(patch_repo, "remote", "add", "origin", "https://github.com/myao9494/myao_app_patch.git")
    # upstream 用に push 先をローカル bare remote に向けるため remote set-url または別リモート
    git(patch_repo, "remote", "set-url", "--push", "origin", str(remote))
    git(patch_repo, "push", "-u", "origin", "main")

    settings_path = tmp_path / "data" / "settings.local.json"
    store = SettingsStore(settings_path)
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    settings = Settings(
        mode="home",
        patch_repo=str(patch_repo),
        patch_password="test-password",
        download_dir=str(download_dir),
        github_token="fake-github-token",
        github_repo="myao9494/myao_app_patch",
        repositories={
            "sample-app": RepositoryConfig(
                repo_id="sample-app",
                display_name="sample-app",
                path=str(source),
                branch="main",
                baseline_commit=baseline,
            )
        },
    )
    store.save(settings)

    # GitHub API のモック
    from unittest.mock import MagicMock
    mock_release = {
        "id": 999,
        "html_url": "https://github.com/myao9494/myao_app_patch/releases/tag/v2026.09.09-01",
        "upload_url": "https://uploads.github.com/repos/myao9494/myao_app_patch/releases/999/assets{?name,label}",
    }
    mock_asset = {
        "id": 888,
        "browser_download_url": "https://github.com/myao9494/myao_app_patch/releases/download/v2026.09.09-01/myao_app_patch_test.zip",
    }

    import rep_patch.home as home_module
    create_rel_mock = MagicMock(return_value=mock_release)
    upload_asset_mock = MagicMock(return_value=mock_asset)
    monkeypatch.setattr(home_module, "create_github_release", create_rel_mock)
    monkeypatch.setattr(home_module, "upload_release_asset", upload_asset_mock)

    result = publish(settings, store)
    assert result["published"] is True
    assert "bundle_path" in result
    assert Path(result["bundle_path"]).exists()
    assert result["bundle_name"].startswith("myao_app_patch_")
    assert result["bundle_size"] > 0
    assert result.get("release_url") == mock_release["html_url"]
    assert result.get("download_url") == mock_asset["browser_download_url"]

    assert create_rel_mock.called
    assert upload_asset_mock.called


def test_publish_cleans_old_packages_keeps_only_latest_and_tracks_sequence(
    tmp_path: Path, git_helpers
) -> None:
    git, init_repo = git_helpers
    source = init_repo(tmp_path / "source" / "sample-app")
    git(source, "branch", "-M", "main")
    (source / "app.txt").write_text("v1\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "v1")
    baseline = git(source, "rev-parse", "HEAD").decode().strip()

    # パッチ専用リポジトリの準備
    remote = tmp_path / "patch-remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    patch_repo = init_repo(tmp_path / "myao_app_patch")
    git(patch_repo, "branch", "-M", "main")
    (patch_repo / "README.md").write_text("patches\n", encoding="utf-8")
    git(patch_repo, "add", "-A")
    git(patch_repo, "commit", "-m", "initial")
    git(patch_repo, "remote", "add", "origin", str(remote))
    git(patch_repo, "push", "-u", "origin", "main")

    settings_path = tmp_path / "data" / "settings.local.json"
    store = SettingsStore(settings_path)
    settings = Settings(
        mode="home",
        patch_repo=str(patch_repo),
        patch_password="test-password",
        repositories={
            "sample-app": RepositoryConfig(
                repo_id="sample-app",
                display_name="sample-app",
                path=str(source),
                branch="main",
                baseline_commit=baseline,
            )
        },
    )
    store.save(settings)

    # 1回目の変更と公開
    (source / "app.txt").write_text("v2\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "v2")
    res1 = publish(settings, store)
    assert res1["published"] is True
    assert (patch_repo / "packages" / "sample-app" / "000001").exists()
    assert store.load().repositories["sample-app"].last_sequence == 1

    # 2回目の変更と公開
    (source / "app.txt").write_text("v3\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "v3")
    res2 = publish(settings, store)
    assert res2["published"] is True

    # 最新パッチ 000002 が存在すること
    assert (patch_repo / "packages" / "sample-app" / "000002").exists()

    # 【最重要】過去パッチ 000001 はリポジトリから削除されていること！
    assert not (patch_repo / "packages" / "sample-app" / "000001").exists()

    # package-index.json にも 000002 のみ記載され、署名が正しいこと
    index = json.loads((patch_repo / "package-index.json").read_text(encoding="utf-8"))
    verify_document(index, "test-password")
    assert len(index["packages"]) == 1
    assert index["packages"][0]["sequence"] == 2
    assert index["packages"][0]["manifest_path"] == "packages/sample-app/000002/manifest.json"

    # 設定ファイルで last_sequence が 2 に更新されていること
    assert store.load().repositories["sample-app"].last_sequence == 2

    # パッチ専用リポジトリのGit状態がクリーン（未追跡や未コミットなし）でリモートにpushされていること
    assert git(patch_repo, "status", "--porcelain=v1").decode().strip() == ""
    assert git(patch_repo, "rev-list", "--count", "@{u}..HEAD").decode().strip() == "0"

    # 【会社側検証】会社PCでGitHubの「Code -> Download ZIP」で落ちてきたZIPを模したアーカイブを適用
    import shutil
    import zipfile
    from rep_patch.company import apply_archive, inspect_archive, load_state

    company_root = tmp_path / "company_apps"
    company_root.mkdir()
    company_repo = init_repo(company_root / "sample-app")
    git(company_repo, "branch", "-M", "main")
    (company_repo / "app.txt").write_text("v1\n", encoding="utf-8")
    git(company_repo, "add", "-A")
    git(company_repo, "commit", "-m", "init company")

    company_download_dir = Path(tmp_path / "company_downloads")
    company_download_dir.mkdir()
    company_settings = Settings(
        mode="company",
        company_apps_root=str(company_root),
        patch_password="test-password",
        download_dir=str(company_download_dir),
    )

    # 1回目のパッチZIPを会社側download_dirに置いて適用
    res1_copied = company_download_dir / Path(res1["bundle_path"]).name
    shutil.copy(res1["bundle_path"], res1_copied)
    apply_res1 = apply_archive(company_settings, str(res1_copied))
    assert apply_res1["results"][0]["status"] == "applied", f"Apply failed: {apply_res1['results'][0]['message']}"
    assert (company_repo / "app.txt").read_text(encoding="utf-8") == "v2\n"
    assert load_state(company_repo, "sample-app")["confirmed_sequence"] == 1

    # 2回目のパッチ: GitHubから「Code -> Download ZIP」された状態（パッチ専用リポジトリHEADのZIP）を作成
    github_dl_zip = Path(company_settings.download_dir) / "myao_app_patch-main.zip"
    with zipfile.ZipFile(github_dl_zip, "w") as zf:
        for f in patch_repo.rglob("*"):
            if ".git" in f.parts:
                continue
            if f.is_file():
                arcname = f"myao_app_patch-main/{f.relative_to(patch_repo).as_posix()}"
                zf.write(f, arcname)

    # 会社側アプリで GitHub Download ZIP（myao_app_patch-main.zip）を検証＆適用
    summary = inspect_archive(company_settings, str(github_dl_zip))
    assert len(summary["repositories"]) == 1
    assert summary["repositories"][0]["first_sequence"] == 2
    assert summary["repositories"][0]["last_sequence"] == 2

    apply_res2 = apply_archive(company_settings, str(github_dl_zip))
    assert apply_res2["results"][0]["status"] == "applied"
    # ファイルが正しくv3に更新され、連番が2に確定コミットされていること
    assert (company_repo / "app.txt").read_text(encoding="utf-8") == "v3\n"
    assert load_state(company_repo, "sample-app")["confirmed_sequence"] == 2
    assert git(company_repo, "status", "--porcelain=v1").decode().strip() == ""


def _home_setup(tmp_path: Path, names: tuple[str, ...] = ("sample-app",)) -> tuple[SettingsStore, Path, dict[str, Path]]:
    """公開済み地点（baseline）を持つ自宅側リポジトリ群と、リモート付きパッチ専用リポジトリを用意する"""
    patch_repo = make_patch_repo(tmp_path)
    sources: dict[str, Path] = {}
    repositories: dict[str, RepositoryConfig] = {}
    for name in names:
        source = init_repo(tmp_path / "source" / name)
        (source / "app.txt").write_text("v1\n", encoding="utf-8")
        baseline = commit_all(source, "v1")
        sources[name] = source
        repositories[name] = RepositoryConfig(
            repo_id=name, display_name=name, path=str(source), branch="main", baseline_commit=baseline
        )
    store = SettingsStore(tmp_path / "data" / "settings.local.json")
    store.save(
        Settings(
            mode="home",
            patch_repo=str(patch_repo),
            patch_password="test-password",
            download_dir=str(tmp_path / "bundles"),
            repositories=repositories,
        )
    )
    return store, patch_repo, sources


def _status(repo: Path) -> str:
    return git(repo, "status", "--porcelain=v1").decode().strip()


def test_publish_without_changes_leaves_patch_repo_clean(tmp_path: Path) -> None:
    store, patch_repo, sources = _home_setup(tmp_path)

    result = publish(store.load(), store)
    assert result["published"] is False
    assert _status(patch_repo) == ""

    # 直後の公開が「未コミットの変更があります」で止まらない
    (sources["sample-app"] / "app.txt").write_text("v2\n", encoding="utf-8")
    commit_all(sources["sample-app"], "v2")
    assert publish(store.load(), store)["published"] is True
    assert _status(patch_repo) == ""
    assert "packages/** binary" in (patch_repo / ".gitattributes").read_text(encoding="utf-8")


def test_publish_failure_leaves_patch_repo_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, patch_repo, sources = _home_setup(tmp_path, ("app-a", "app-b"))
    for source in sources.values():
        (source / "app.txt").write_text("v2\n", encoding="utf-8")
        commit_all(source, "v2")
    head_before = git(patch_repo, "rev-parse", "HEAD")

    # 1. 設定不備（初期導入コミット未設定）は、何も書き出す前に検出する
    settings = store.load()
    settings.repositories["app-b"].baseline_commit = ""
    store.save(settings)
    with pytest.raises(RepPatchError, match="初期導入コミットが未設定です"):
        publish(store.load(), store)
    assert _status(patch_repo) == ""

    # 2. 2つ目のリポジトリの書き出し途中で失敗しても、1つ目の作りかけを残さない
    settings.repositories["app-b"].baseline_commit = git(
        sources["app-b"], "rev-parse", "HEAD~1"
    ).decode().strip()
    store.save(settings)
    original = home_module.split_patch
    calls = {"count": 0}

    def failing_split(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("disk full")
        return original(*args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(home_module, "split_patch", failing_split)
        with pytest.raises(OSError, match="disk full"):
            publish(store.load(), store)
    assert _status(patch_repo) == ""
    assert git(patch_repo, "rev-parse", "HEAD") == head_before
    assert not (patch_repo / "packages").exists()
    assert store.load().repositories["app-a"].published_commit == ""

    # 3. 原因が解消されれば、そのまま両方を連番1で公開できる
    result = publish(store.load(), store)
    assert result["published"] is True
    assert sorted((m["repo_id"], m["sequence"]) for m in result["packages"]) == [("app-a", 1), ("app-b", 1)]
    assert _status(patch_repo) == ""


def test_publish_push_failure_is_recovered_on_next_publish(tmp_path: Path) -> None:
    store, patch_repo, sources = _home_setup(tmp_path)
    source = sources["sample-app"]
    (source / "app.txt").write_text("v2\n", encoding="utf-8")
    target = commit_all(source, "v2")

    # リモートへ届かない状態にしてpushを失敗させる
    remote = tmp_path / "patch-remote.git"
    hidden = tmp_path / "patch-remote.hidden"
    remote.rename(hidden)
    with pytest.raises(RepPatchError):
        publish(store.load(), store)
    assert _status(patch_repo) == ""
    assert git(patch_repo, "rev-list", "--count", "@{u}..HEAD").decode().strip() == "1"
    assert store.load().repositories["sample-app"].published_commit == ""  # push成功時だけ進む

    # 回線が戻った後の公開操作で、残っていたコミットがpushされ、公開済み地点も追いつく
    hidden.rename(remote)
    result = publish(store.load(), store)
    assert result["published"] is False  # 新しい変更は無い
    assert git(patch_repo, "rev-list", "--count", "@{u}..HEAD").decode().strip() == "0"
    assert store.load().repositories["sample-app"].published_commit == target

    # 次のパッチは連番2になる（連番1を作り直さない）
    (source / "app.txt").write_text("v3\n", encoding="utf-8")
    commit_all(source, "v3")
    result = publish(store.load(), store)
    assert [m["sequence"] for m in result["packages"]] == [2]


def test_publish_rejects_unsupported_entries(tmp_path: Path) -> None:
    store, patch_repo, sources = _home_setup(tmp_path)
    source = sources["sample-app"]
    inner = init_repo(tmp_path / "inner")
    (inner / "lib.txt").write_text("lib\n", encoding="utf-8")
    commit_all(inner, "lib")
    git(source, "-c", "protocol.file.allow=always", "submodule", "add", str(inner), "vendor/inner")
    commit_all(source, "add submodule")

    with pytest.raises(RepPatchError, match="サブモジュール"):
        publish(store.load(), store)
    with pytest.raises(RepPatchError, match="サブモジュール"):
        publish(store.load(), store, skip_verification=True)
    assert _status(patch_repo) == ""


def test_publish_reports_bundle_failure_without_failing(tmp_path: Path) -> None:
    store, patch_repo, sources = _home_setup(tmp_path)
    (sources["sample-app"] / "app.txt").write_text("v2\n", encoding="utf-8")
    target = commit_all(sources["sample-app"], "v2")
    # 出力先がフォルダではなくファイルになっている
    (tmp_path / "bundles").write_text("not a directory", encoding="utf-8")

    result = publish(store.load(), store)

    assert result["published"] is True
    assert result["bundle_path"] == ""
    assert result["bundle_error"]
    assert store.load().repositories["sample-app"].published_commit == target


def test_publish_writes_bundle_into_patch_repo_dist_when_download_dir_is_empty(tmp_path: Path) -> None:
    store, patch_repo, sources = _home_setup(tmp_path)
    settings = store.load()
    settings.download_dir = ""
    store.save(settings)
    source = sources["sample-app"]

    (source / "app.txt").write_text("v2\n", encoding="utf-8")
    commit_all(source, "v2")
    result = publish(store.load(), store)
    assert Path(result["bundle_path"]).parent == patch_repo / "dist"
    assert _status(patch_repo) == ""  # dist/ は未コミットの変更として扱われない

    (source / "app.txt").write_text("v3\n", encoding="utf-8")
    commit_all(source, "v3")
    assert publish(store.load(), store)["published"] is True


def test_publish_then_apply_end_to_end(tmp_path: Path) -> None:
    """実際の公開処理が作ったZIPを、独立した履歴を持つ会社側リポジトリへ適用する"""
    store, patch_repo, sources = _home_setup(tmp_path)
    source = sources["sample-app"]
    binary_v1 = bytes(range(256)) * 4
    (source / "data.bin").write_bytes(binary_v1)
    (source / "old_name.txt").write_text("renamed content that stays the same\n" * 5, encoding="utf-8")
    (source / "remove_me.txt").write_text("bye\n", encoding="utf-8")
    baseline = commit_all(source, "baseline")
    settings = store.load()
    settings.repositories["sample-app"].baseline_commit = baseline
    settings.chunk_size_mib = 1
    store.save(settings)

    company = init_repo(tmp_path / "company-apps" / "sample-app")
    for name in ("app.txt", "data.bin", "old_name.txt", "remove_me.txt"):
        shutil.copy(source / name, company / name)
    (company / "company_only.txt").write_text("keep me\n", encoding="utf-8")
    commit_all(company, "company baseline")

    binary_v2 = bytes(reversed(range(256))) * 4
    crlf = b"line one\r\nline two\r\n"
    (source / "app.txt").write_text("v2\n", encoding="utf-8")
    (source / "data.bin").write_bytes(binary_v2)
    (source / "crlf.txt").write_bytes(crlf)
    (source / "日本語 フォルダ").mkdir()
    (source / "日本語 フォルダ" / "メモ.txt").write_text("追加\n", encoding="utf-8")
    git(source, "mv", "old_name.txt", "new_name.txt")
    (source / "remove_me.txt").unlink()
    commit_all(source, "v2")

    result = publish(store.load(), store)
    assert result["published"] is True
    manifest = result["packages"][0]
    assert "old_name.txt" in manifest["deleted_files"] and "remove_me.txt" in manifest["deleted_files"]

    company_settings = Settings(
        mode="company",
        company_apps_root=str(tmp_path / "company-apps"),
        patch_password="test-password",
        download_dir=str(tmp_path / "bundles"),
    )
    applied = apply_archive(company_settings, result["bundle_path"])

    assert applied["failed"] == 0, applied["results"]
    assert (company / "app.txt").read_text(encoding="utf-8") == "v2\n"
    assert (company / "data.bin").read_bytes() == binary_v2
    assert (company / "crlf.txt").read_bytes() == crlf
    assert (company / "日本語 フォルダ" / "メモ.txt").read_text(encoding="utf-8") == "追加\n"
    assert (company / "new_name.txt").exists()
    assert not (company / "old_name.txt").exists()
    assert not (company / "remove_me.txt").exists()
    assert (company / "company_only.txt").read_text(encoding="utf-8") == "keep me\n"
    assert _status(company) == ""
    assert load_state(company, "sample-app")["confirmed_sequence"] == 1


def test_reset_requires_clean_patch_repo(tmp_path: Path) -> None:
    store, patch_repo, sources = _home_setup(tmp_path)
    (sources["sample-app"] / "app.txt").write_text("v2\n", encoding="utf-8")
    commit_all(sources["sample-app"], "v2")
    publish(store.load(), store)
    (patch_repo / "stray.txt").write_text("unrelated work in progress\n", encoding="utf-8")

    with pytest.raises(RepPatchError, match="未コミットの変更"):
        reset_repository_patches(store.load(), store, "sample-app")
    assert (patch_repo / "stray.txt").exists()
    assert (patch_repo / "packages" / "sample-app" / "000001").exists()
    assert store.load().repositories["sample-app"].last_sequence == 1


def _lfs_pointer(content: bytes) -> bytes:
    digest = hashlib.sha256(content).hexdigest()
    return f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize {len(content)}\n".encode("ascii")


def _cache_lfs_object(repo: Path, content: bytes) -> None:
    digest = hashlib.sha256(content).hexdigest()
    target = repo / ".git" / "lfs" / "objects" / digest[:2] / digest[2:4] / digest
    target.parent.mkdir(parents=True)
    target.write_bytes(content)


def test_publish_ships_lfs_object_split_into_parts_and_company_joins_it(tmp_path: Path) -> None:
    """LFS管理の大きなファイルは、ポインタではなく実体を分割して運び、会社側で結合して配置する"""
    store, patch_repo, sources = _home_setup(tmp_path)
    source = sources["sample-app"]
    (source / "base.txt").write_text("base\n", encoding="utf-8")
    baseline = commit_all(source, "baseline")
    settings = store.load()
    settings.repositories["sample-app"].baseline_commit = baseline
    settings.chunk_size_mib = 1
    store.save(settings)
    company = init_repo(tmp_path / "company-apps" / "sample-app")
    shutil.copy(source / "app.txt", company / "app.txt")
    commit_all(company, "company baseline")

    real_exe = os.urandom(2_500_000)
    (source / ".gitattributes").write_text("*.exe filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
    (source / "launcher.exe").write_bytes(_lfs_pointer(real_exe))
    commit_all(source, "add lfs launcher")
    _cache_lfs_object(source, real_exe)
    # 実運用では git-lfs の設定が入っている。パッチ専用リポジトリへ入れる際にポインタへ戻されないこと
    (Path.home() / ".gitconfig").write_text(
        '[filter "lfs"]\n\tclean = false\n\tprocess = /nonexistent/git-lfs filter-process\n\trequired = true\n',
        encoding="utf-8",
    )

    result = publish(store.load(), store)
    assert result["published"] is True
    record = next(r for r in result["packages"][0]["added_files"] if r["path"] == "launcher.exe")
    assert record["size"] == len(real_exe)
    assert len(record["parts"]) == 3
    package_root = patch_repo / "packages" / "sample-app" / "000001"
    assert not (package_root / "added_files" / "launcher.exe").exists()
    assert all(
        len((package_root / "added_parts" / f"launcher.exe.part-{i:06d}").read_bytes()) <= 1024 * 1024
        for i in (1, 2, 3)
    )

    company_settings = Settings(
        mode="company",
        company_apps_root=str(tmp_path / "company-apps"),
        patch_password="test-password",
        download_dir=str(tmp_path / "bundles"),
    )
    applied = apply_archive(company_settings, result["bundle_path"])
    assert applied["failed"] == 0, applied["results"]
    assert (company / "launcher.exe").read_bytes() == real_exe


def test_publish_refuses_when_lfs_object_is_unavailable(tmp_path: Path) -> None:
    """LFSの実体を取り出せないときは、ポインタを黙って運ばず公開を止める"""
    store, patch_repo, sources = _home_setup(tmp_path)
    source = sources["sample-app"]
    (source / "base.txt").write_text("base\n", encoding="utf-8")
    baseline = commit_all(source, "baseline")
    settings = store.load()
    settings.repositories["sample-app"].baseline_commit = baseline
    store.save(settings)
    (source / ".gitattributes").write_text("*.exe filter=lfs -text\n", encoding="utf-8")
    (source / "launcher.exe").write_bytes(_lfs_pointer(b"not available locally"))
    commit_all(source, "add lfs launcher")

    with pytest.raises(RepPatchError, match="Git LFS"):
        publish(store.load(), store)
    assert _status(patch_repo) == ""
