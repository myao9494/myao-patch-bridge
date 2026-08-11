from __future__ import annotations

import json
import zipfile
from pathlib import Path

from rep_patch.company import apply_archive, load_state
from rep_patch.config import Settings
from rep_patch.git import changed_paths, tracked_files
from rep_patch.packages import SCHEMA_VERSION, split_patch
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
        chunks = split_patch(patch, package_root / relative, 80)
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
            "package_type": "myao-rep-patch",
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
    assert git(company, "rev-parse", "HEAD").decode().strip() == company_base
    assert git(company, "diff", "--name-only").decode().splitlines() == ["app.txt", "binary.bin"]
    assert b"\xe6\x97\xa5" in git(company, "status", "--porcelain=v1", "-z")
    state = load_state(company, "sample-app")
    assert state["pending_sequences"] == [1]

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
    assert git(company, "rev-parse", "HEAD").decode().strip() != company_base
    log = git(company, "log", "-1", "--pretty=%s").decode().strip()
    assert log == "[myao-patch] sample-app patch-000001"
    state = load_state(company, "sample-app")
    assert state["confirmed_sequence"] == 1
    assert state["pending_sequences"] == [2]


def test_preimage_mismatch_fails_without_changing_company_files(tmp_path: Path, git_helpers) -> None:
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
    (company / "app.txt").write_text("different company content\n", encoding="utf-8")
    git(company, "add", "-A")
    git(company, "commit", "-m", "different base")
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
    assert (company / "app.txt").read_text(encoding="utf-8") == "different company content\n"
    assert git(company, "rev-parse", "HEAD").decode().strip() == before
    assert git(company, "status", "--porcelain").decode() == ""
