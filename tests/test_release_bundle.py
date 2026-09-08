"""
配布用軽量パッチZIP（create_release_bundle）の生成および検証テスト

仕様:
- create_release_bundle により今回作成されたマニフェストのみを含むZIPが作成されること
- ZIP内に今回分のみの署名付き package-index.json が配置されること
- 過去の他パッチが含まれず、軽量な状態が保たれること
- 会社側モジュール（PatchArchive）でそのZIPを開き、正常に検証・読み込みできること
"""
from pathlib import Path
import tempfile
import zipfile

from rep_patch.packages import (
    create_release_bundle,
    PatchArchive,
    SCHEMA_VERSION,
    split_patch,
    write_json,
)
from rep_patch.security import sha256_bytes, sign_document


def test_create_release_bundle_and_verify_with_patch_archive(tmp_path: Path):
    password = "test-secret-pass"
    patch_root = tmp_path / "patch_repo"
    output_dir = tmp_path / "dist"
    patch_root.mkdir(parents=True)

    # 過去のパッチ (repo-a: seq 1) を作成
    old_dir = patch_root / "packages" / "repo-a" / "000001"
    old_dir.mkdir(parents=True)
    (old_dir / "added_files").mkdir()
    (old_dir / "added_files" / "old_file.txt").write_text("old content", encoding="utf-8")
    old_patch_data = b"old patch diff"
    old_chunks = split_patch(old_patch_data, old_dir, 1024 * 1024)
    old_manifest = sign_document(
        {
            "schema_version": SCHEMA_VERSION,
            "repo_id": "repo-a",
            "display_name": "Repo A",
            "kind": "app",
            "sequence": 1,
            "created_at": "2026-09-01T00:00:00Z",
            "source_branch": "main",
            "source_from_commit": "c0",
            "source_to_commit": "c1",
            "patch_size": len(old_patch_data),
            "patch_sha256": sha256_bytes(old_patch_data),
            "chunks": old_chunks,
            "changed_paths": ["old_file.txt"],
            "added_files": [
                {"path": "old_file.txt", "size": len(b"old content"), "sha256": sha256_bytes(b"old content")}
            ],
            "deleted_files": [],
            "target_files": ["old_file.txt"],
        },
        password,
    )
    write_json(old_dir / "manifest.json", old_manifest)

    # 今回公開するパッチ (repo-a: seq 2) を作成
    new_dir_a = patch_root / "packages" / "repo-a" / "000002"
    new_dir_a.mkdir(parents=True)
    (new_dir_a / "added_files").mkdir()
    (new_dir_a / "added_files" / "new_file_a.txt").write_text("new content a", encoding="utf-8")
    new_patch_a = b"patch diff a"
    chunks_a = split_patch(new_patch_a, new_dir_a, 1024 * 1024)
    manifest_a = sign_document(
        {
            "schema_version": SCHEMA_VERSION,
            "repo_id": "repo-a",
            "display_name": "Repo A",
            "kind": "app",
            "sequence": 2,
            "created_at": "2026-09-09T00:00:00Z",
            "source_branch": "main",
            "source_from_commit": "c1",
            "source_to_commit": "c2",
            "patch_size": len(new_patch_a),
            "patch_sha256": sha256_bytes(new_patch_a),
            "chunks": chunks_a,
            "changed_paths": ["new_file_a.txt"],
            "added_files": [
                {"path": "new_file_a.txt", "size": len(b"new content a"), "sha256": sha256_bytes(b"new content a")}
            ],
            "deleted_files": [],
            "target_files": ["new_file_a.txt"],
        },
        password,
    )
    write_json(new_dir_a / "manifest.json", manifest_a)

    created_manifests = [manifest_a]

    # bundle を作成
    zip_path = create_release_bundle(
        patch_root=patch_root,
        created_manifests=created_manifests,
        password=password,
        output_dir=output_dir,
        bundle_name="myao_app_patch_test.zip",
    )

    assert zip_path.exists()
    assert zip_path.name == "myao_app_patch_test.zip"

    # ZIP内に過去のパッチ (repo-a/000001) が含まれていないことを確認
    with zipfile.ZipFile(zip_path) as zf:
        namelist = zf.namelist()
        assert "package-index.json" in namelist
        assert "packages/repo-a/000002/manifest.json" in namelist
        assert "packages/repo-a/000002/added_files/new_file_a.txt" in namelist
        # 過去パッチは含まれない
        assert not any("repo-a/000001" in name for name in namelist)

    # 会社側の PatchArchive で開いて正常に検証できることを確認
    with PatchArchive(zip_path, password) as archive:
        summary = archive.summary()
        assert len(summary["repositories"]) == 1
        repo_summary = summary["repositories"][0]
        assert repo_summary["repo_id"] == "repo-a"
        assert repo_summary["first_sequence"] == 2
        assert repo_summary["last_sequence"] == 2

        groups = archive.package_groups()
        assert "repo-a" in groups
        package = groups["repo-a"][0]
        added = archive.read_added_file(package, "new_file_a.txt")
        assert added == b"new content a"
