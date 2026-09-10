"""
自宅側でのパッチ生成・公開フローおよびリセット機能のテスト

仕様:
- test_publish_creates_package_pushes_and_advances_cursor: 差分パッチ作成・新規ファイル実体同梱・削除記録・署名・push・カーソル進行の検証
- test_reset_repository_patches_clears_packages_and_resets_sequence: パッチ履歴リセットによりパッチ専用リポジトリの該当パッケージ削除・インデックス再署名・push・次回000001再生成の検証
- test_publish_creates_release_bundle_and_uploads_to_github: 今回分軽量ZIP生成およびGitHub Releases自動公開の検証
- test_publish_cleans_old_packages_keeps_only_latest_and_tracks_sequence: 過去パッチの自動クリーンアップ・リポジトリ軽量化・last_sequence保持の検証
"""
from __future__ import annotations

import json
from pathlib import Path

from rep_patch.config import RepositoryConfig, Settings, SettingsStore
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
    target = git(source, "rev-parse", "HEAD").decode().strip()

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
    apply_res1 = apply_archive(company_settings, str(res1_copied), correction=False)
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

    apply_res2 = apply_archive(company_settings, str(github_dl_zip), correction=False)
    assert apply_res2["results"][0]["status"] == "applied"
    # ファイルが正しくv3に更新され、連番が2に確定コミットされていること
    assert (company_repo / "app.txt").read_text(encoding="utf-8") == "v3\n"
    assert load_state(company_repo, "sample-app")["confirmed_sequence"] == 2
    assert git(company_repo, "status", "--porcelain=v1").decode().strip() == ""
