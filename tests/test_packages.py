"""
パッチZIP（PatchArchive）の検証テスト

仕様:
- 正しいパスワードで署名されたZIPだけを受け付ける
- インデックス・マニフェスト・分割パッチ・同梱ファイルのいずれかが改変・欠落していれば拒否する
- 安全でないパス（上位ディレクトリ参照、絶対パス、.git 配下）や重複パスを含むZIPを拒否する
- 検証に失敗したZIPを開いたままにしない
- 同梱ファイルは、マニフェストに記載されたものだけを読み出せる
"""
from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from rep_patch.errors import PackageValidationError
from rep_patch.packages import (
    PatchArchive,
    build_index,
    package_dir,
    resolve_added_file_content,
    split_patch,
)
from rep_patch.security import sha256_bytes, sign_document

PASSWORD = "test-password"
ROOT = "myao_app_patch-main/"


def _entries(added: dict[str, bytes] | None = None, sequence: int = 1) -> dict[str, bytes]:
    """1パッケージ分の正しいZIP内容（アーカイブ内パス → バイト列）を作る"""
    added = {"app.txt": b"new content\n"} if added is None else added
    patch = b"diff --git a/app.txt b/app.txt\n"
    base = ROOT + str(package_dir("sample-app", sequence)) + "/"
    manifest = sign_document(
        {
            "schema_version": 1,
            "repo_id": "sample-app",
            "display_name": "sample-app",
            "kind": "app",
            "sequence": sequence,
            "patch_size": len(patch),
            "patch_sha256": sha256_bytes(patch),
            "chunks": [{"name": "changes.patch.part-000001", "size": len(patch), "sha256": sha256_bytes(patch)}],
            "changed_paths": sorted(added),
            "added_files": [
                {"path": path, "size": len(content), "sha256": sha256_bytes(content)}
                for path, content in added.items()
            ],
            "deleted_files": [],
            "target_files": [],
        },
        PASSWORD,
    )
    entries = {
        ROOT + "package-index.json": json.dumps(build_index([manifest], PASSWORD)).encode(),
        base + "manifest.json": json.dumps(manifest).encode(),
        base + "changes.patch.part-000001": patch,
    }
    for path, content in added.items():
        entries[base + "added_files/" + path] = content
    return entries


def _write_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def _manifest_name(entries: dict[str, bytes]) -> str:
    return next(name for name in entries if name.endswith("manifest.json"))


def test_valid_archive_is_accepted_and_added_files_are_readable(tmp_path: Path) -> None:
    zip_path = _write_zip(tmp_path / "ok.zip", _entries({"app.txt": b"new\n", "dir/sub.bin": b"\x00\x01"}))
    with PatchArchive(zip_path, PASSWORD) as archive:
        (package,) = archive.packages
        assert archive.read_added_file(package, "dir/sub.bin") == b"\x00\x01"
        assert archive.summary()["repositories"][0]["last_sequence"] == 1
        # マニフェストに載っていないファイルは、ZIP内に存在しても読み出さない
        with pytest.raises(PackageValidationError, match="記載のない"):
            archive.read_added_file(package, "not-listed.txt")


def test_wrong_password_is_rejected(tmp_path: Path) -> None:
    zip_path = _write_zip(tmp_path / "ok.zip", _entries())
    with pytest.raises(PackageValidationError, match="パスワードが異なる"):
        PatchArchive(zip_path, "another-password")


def _tamper_manifest(entries: dict[str, bytes]) -> None:
    name = _manifest_name(entries)
    manifest = json.loads(entries[name])
    manifest["deleted_files"] = ["important.txt"]
    entries[name] = json.dumps(manifest).encode()


def _tamper_index(entries: dict[str, bytes]) -> None:
    name = ROOT + "package-index.json"
    index = json.loads(entries[name])
    index["packages"].append({"repo_id": "x", "sequence": 1, "manifest_path": "packages/x/000001/manifest.json"})
    entries[name] = json.dumps(index).encode()


def _tamper_chunk(entries: dict[str, bytes]) -> None:
    name = next(name for name in entries if "changes.patch.part-" in name)
    entries[name] = entries[name] + b"extra"


def _tamper_added_file(entries: dict[str, bytes]) -> None:
    name = next(name for name in entries if "/added_files/" in name)
    entries[name] = b"malicious content\n"


def _drop_added_file(entries: dict[str, bytes]) -> None:
    del entries[next(name for name in entries if "/added_files/" in name)]


def _drop_index(entries: dict[str, bytes]) -> None:
    del entries[ROOT + "package-index.json"]


def _break_json(entries: dict[str, bytes]) -> None:
    entries[ROOT + "package-index.json"] = b"{ not json"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_tamper_manifest, "改変"),
        (_tamper_index, "改変"),
        (_tamper_chunk, "破損"),
        (_tamper_added_file, "追加ファイルが破損"),
        (_drop_added_file, "ZIP内のファイルがありません"),
        (_drop_index, "package-index.jsonを1つだけ"),
        (_break_json, "package-index.jsonが不正"),
    ],
)
def test_modified_or_incomplete_archive_is_rejected(
    tmp_path: Path, mutate: Callable[[dict[str, bytes]], None], message: str
) -> None:
    entries = _entries()
    mutate(entries)
    zip_path = _write_zip(tmp_path / "bad.zip", entries)

    with pytest.raises(PackageValidationError, match=message):
        PatchArchive(zip_path, PASSWORD)
    # 検証に失敗したZIPを掴んだままにしない（すぐ削除・差し替えできる）
    zip_path.unlink()


@pytest.mark.parametrize("unsafe", ["../outside.txt", "/absolute.txt", ".git/hooks/pre-commit", "sub/.GIT/config"])
def test_signed_manifest_with_unsafe_added_path_is_rejected(tmp_path: Path, unsafe: str) -> None:
    """正しく署名されていても、リポジトリ外やGit管理領域へ書き込むパスは受け付けない"""
    entries = _entries({"app.txt": b"x\n"})
    name = _manifest_name(entries)
    manifest = {k: v for k, v in json.loads(entries[name]).items() if k != "signature"}
    manifest["added_files"][0]["path"] = unsafe
    entries[name] = json.dumps(sign_document(manifest, PASSWORD)).encode()

    with pytest.raises(PackageValidationError):
        PatchArchive(_write_zip(tmp_path / "bad.zip", entries), PASSWORD)


@pytest.mark.parametrize("member", ["../evil.txt", "/etc/evil.txt", "C:/evil.txt"])
def test_archive_with_unsafe_member_path_is_rejected(tmp_path: Path, member: str) -> None:
    entries = _entries()
    entries[member] = b"evil"
    with pytest.raises(PackageValidationError, match="安全でないパス"):
        PatchArchive(_write_zip(tmp_path / "bad.zip", entries), PASSWORD)


def test_archive_with_duplicate_member_is_rejected(tmp_path: Path) -> None:
    zip_path = tmp_path / "dup.zip"
    with zipfile.ZipFile(zip_path, "w") as archive, pytest.warns(UserWarning):
        for name, content in _entries().items():
            archive.writestr(name, content)
        archive.writestr(ROOT + "package-index.json", b"{}")
    with pytest.raises(PackageValidationError, match="重複"):
        PatchArchive(zip_path, PASSWORD)


def test_not_a_zip_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.zip"
    path.write_bytes(b"this is not a zip")
    with pytest.raises(PackageValidationError, match="ZIPを開けません"):
        PatchArchive(path, PASSWORD)


def test_resolve_added_file_content_restores_line_endings() -> None:
    crlf = b"a\r\nb\r\n"
    lf = b"a\nb\n"
    assert resolve_added_file_content(crlf, len(crlf), sha256_bytes(crlf)) == crlf
    # 元がCRLFで、ZIP内がLFへ正規化されている
    assert resolve_added_file_content(lf, len(crlf), sha256_bytes(crlf)) == crlf
    # 元がLFで、ZIP内がCRLFへ変換されている
    assert resolve_added_file_content(crlf, len(lf), sha256_bytes(lf)) == lf
    # 改行以外が違うものは復元しない
    assert resolve_added_file_content(b"a\nX\n", len(lf), sha256_bytes(lf)) is None


def test_split_patch_chunks_reassemble(tmp_path: Path) -> None:
    data = bytes(range(256)) * 10
    chunks = split_patch(data, tmp_path / "pkg", 1000)
    assert [chunk["name"] for chunk in chunks] == [f"changes.patch.part-{i:06d}" for i in range(1, 4)]
    assert b"".join((tmp_path / "pkg" / chunk["name"]).read_bytes() for chunk in chunks) == data
    assert split_patch(b"", tmp_path / "empty", 1000) == []
