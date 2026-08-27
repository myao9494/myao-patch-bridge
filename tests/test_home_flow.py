"""
自宅側でのパッチ生成・公開フローのテスト

仕様:
- test_publish_creates_package_pushes_and_advances_cursor: 差分パッチ作成・新規ファイル実体同梱・削除記録・署名・push・カーソル進行の検証
"""
from __future__ import annotations

import json
from pathlib import Path

from rep_patch.config import RepositoryConfig, Settings, SettingsStore
from rep_patch.home import publish
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


