from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from typing_extensions import Self

from .errors import PackageValidationError
from .security import safe_relative_path, sha256_bytes, sign_document, verify_document

SCHEMA_VERSION = 1
PACKAGE_TYPE = "myao-patch-bridge"
LEGACY_PACKAGE_TYPES = {"myao-rep-patch"}
MAX_ZIP_FILES = 20_000
MAX_ZIP_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 20 * 1024 * 1024


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageValidationError(f"JSONを読み込めません: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PackageValidationError(f"JSONのルートはオブジェクトである必要があります: {path}")
    return value


def new_index(password: str) -> dict[str, Any]:
    return sign_document(
        {
            "schema_version": SCHEMA_VERSION,
            "package_type": PACKAGE_TYPE,
            "packages": [],
        },
        password,
    )


def load_index(path: Path, password: str) -> dict[str, Any]:
    if not path.exists():
        return new_index(password)
    value = read_json(path)
    validate_index(value, password)
    return value


def validate_index(value: dict[str, Any], password: str) -> None:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise PackageValidationError("未対応のパッケージ形式です")
    if value.get("package_type") not in {PACKAGE_TYPE} | LEGACY_PACKAGE_TYPES:
        raise PackageValidationError("Patch Bridgeパッケージではありません")
    if not isinstance(value.get("packages"), list):
        raise PackageValidationError("packagesが不正です")
    verify_document(value, password)


def split_patch(data: bytes, target_dir: Path, chunk_size: int) -> list[dict[str, Any]]:
    target_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[dict[str, Any]] = []
    if not data:
        return chunks
    for index, offset in enumerate(range(0, len(data), chunk_size), start=1):
        value = data[offset : offset + chunk_size]
        name = f"changes.patch.part-{index:06d}"
        (target_dir / name).write_bytes(value)
        chunks.append({"name": name, "size": len(value), "sha256": sha256_bytes(value)})
    return chunks


@dataclass
class ArchivePackage:
    manifest_path: str
    manifest: dict[str, Any]


class PatchArchive:
    """Safely reads a GitHub source ZIP without extracting it wholesale."""

    def __init__(self, path: Path, password: str):
        self.path = path
        self.password = password
        try:
            self.zip = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise PackageValidationError(f"ZIPを開けません: {exc}") from exc
        self._validate_members()
        self.root_prefix, self.index = self._load_index()
        self.packages = self._load_manifests()

    def close(self) -> None:
        self.zip.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _validate_members(self) -> None:
        members = self.zip.infolist()
        if len(members) > MAX_ZIP_FILES:
            raise PackageValidationError("ZIP内のファイル数が上限を超えています")
        total = 0
        seen: set[str] = set()
        for member in members:
            path = PurePosixPath(member.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or ":" in (path.parts[0] if path.parts else ""):
                raise PackageValidationError(f"ZIPに安全でないパスがあります: {member.filename}")
            normalized = str(path)
            if normalized in seen:
                raise PackageValidationError(f"ZIP内でパスが重複しています: {member.filename}")
            seen.add(normalized)
            total += member.file_size
            if total > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise PackageValidationError("ZIPの展開後サイズが上限を超えています")

    def _load_index(self) -> tuple[str, dict[str, Any]]:
        candidates = [
            name
            for name in self.zip.namelist()
            if PurePosixPath(name).name == "package-index.json"
        ]
        if len(candidates) != 1:
            raise PackageValidationError("package-index.jsonを1つだけ含む必要があります")
        name = candidates[0]
        raw = self._read_limited(name, MAX_JSON_BYTES)
        try:
            index = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PackageValidationError(f"package-index.jsonが不正です: {exc}") from exc
        if not isinstance(index, dict):
            raise PackageValidationError("package-index.jsonが不正です")
        validate_index(index, self.password)
        parent = str(PurePosixPath(name).parent)
        return "" if parent == "." else parent + "/", index

    def _load_manifests(self) -> list[ArchivePackage]:
        result: list[ArchivePackage] = []
        seen: set[tuple[str, int]] = set()
        for item in self.index["packages"]:
            if not isinstance(item, dict):
                raise PackageValidationError("パッケージ索引が不正です")
            relative = str(safe_relative_path(str(item.get("manifest_path", ""))))
            raw = self._read_limited(self.root_prefix + relative, MAX_JSON_BYTES)
            try:
                manifest = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PackageValidationError(f"マニフェストが不正です: {relative}: {exc}") from exc
            if not isinstance(manifest, dict):
                raise PackageValidationError(f"マニフェストが不正です: {relative}")
            verify_document(manifest, self.password)
            if manifest.get("schema_version") != SCHEMA_VERSION:
                raise PackageValidationError(f"未対応のマニフェストです: {relative}")
            key = (str(manifest.get("repo_id")), int(manifest.get("sequence", 0)))
            if key in seen:
                raise PackageValidationError(f"パッケージ連番が重複しています: {key}")
            seen.add(key)
            self._verify_chunks(relative, manifest)
            result.append(ArchivePackage(relative, manifest))
        return sorted(result, key=lambda package: (package.manifest["repo_id"], package.manifest["sequence"]))

    def _read_limited(self, name: str, limit: int) -> bytes:
        try:
            info = self.zip.getinfo(name)
        except KeyError as exc:
            raise PackageValidationError(f"ZIP内のファイルがありません: {name}") from exc
        if info.file_size > limit:
            raise PackageValidationError(f"ファイルが大きすぎます: {name}")
        return self.zip.read(info)

    def _verify_chunks(self, manifest_path: str, manifest: dict[str, Any]) -> None:
        chunks = manifest.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise PackageValidationError(f"パッチ分割情報がありません: {manifest_path}")
        base = PurePosixPath(manifest_path).parent
        total_size = 0
        digest = hashlib.sha256()
        for expected_index, chunk in enumerate(chunks, start=1):
            if not isinstance(chunk, dict):
                raise PackageValidationError("パッチ分割情報が不正です")
            name = str(safe_relative_path(str(chunk.get("name", ""))))
            if name != f"changes.patch.part-{expected_index:06d}":
                raise PackageValidationError("パッチ分割ファイルの順序が不正です")
            archive_name = self.root_prefix + str(base / name)
            value = self._read_limited(archive_name, 50 * 1024 * 1024)
            if len(value) != int(chunk.get("size", -1)) or sha256_bytes(value) != chunk.get("sha256"):
                raise PackageValidationError(f"パッチ分割ファイルが破損しています: {name}")
            digest.update(value)
            total_size += len(value)
        if total_size != int(manifest.get("patch_size", -1)):
            raise PackageValidationError("復元パッチのサイズが一致しません")
        if digest.hexdigest() != manifest.get("patch_sha256"):
            raise PackageValidationError("復元パッチのSHA-256が一致しません")

    def package_groups(self) -> dict[str, list[ArchivePackage]]:
        result: dict[str, list[ArchivePackage]] = {}
        for package in self.packages:
            result.setdefault(package.manifest["repo_id"], []).append(package)
        return result

    @contextmanager
    def reconstructed_patch(self, package: ArchivePackage, directory: Path) -> Iterator[Path]:
        manifest = package.manifest
        base = PurePosixPath(package.manifest_path).parent
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix="rep-patch-", suffix=".patch", dir=directory)
        os.close(descriptor)
        target = Path(temporary_name)
        try:
            with target.open("wb") as stream:
                for chunk in manifest["chunks"]:
                    stream.write(self.zip.read(self.root_prefix + str(base / chunk["name"])))
            if target.stat().st_size != manifest["patch_size"]:
                raise PackageValidationError("復元パッチのサイズが一致しません")
            yield target
        finally:
            target.unlink(missing_ok=True)

    def summary(self) -> dict[str, Any]:
        groups = self.package_groups()
        return {
            "path": str(self.path),
            "package_count": len(self.packages),
            "repository_count": len(groups),
            "repositories": [
                {
                    "repo_id": repo_id,
                    "display_name": packages[-1].manifest["display_name"],
                    "kind": packages[-1].manifest["kind"],
                    "first_sequence": packages[0].manifest["sequence"],
                    "last_sequence": packages[-1].manifest["sequence"],
                    "total_patch_size": sum(item.manifest["patch_size"] for item in packages),
                }
                for repo_id, packages in groups.items()
            ],
        }
