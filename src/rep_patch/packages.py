"""
パッチパッケージの分割・保存およびZIPアーカイブ処理を行うモジュール

仕様:
- SCHEMA_VERSION: パッケージ仕様バージョン
- package_dir / index_entry / build_index: パッケージの配置パスと署名付きインデックスの組み立て
- split_patch: 指定サイズごとのバイナリパッチ分割処理
- split_added_file / added_part_name: 大きな同梱ファイル実体のパーツ分割（会社側で結合して照合する）
- resolve_added_file_content: 改行コード変換（CRLF↔LF）差異を透過的に検知・元バイト列復元するファイル検証処理
- PatchArchive: パッチZIPを一括展開せずに検証し、同梱ファイル実体を読み出す
- create_release_bundle: 今回公開されたパッチのみを含む配布用軽量ZIPアーカイブの生成

補足:
- 分割パッチ（changes.patch.part-*）は形式v1との互換性のために生成・検証するが、会社側の適用には使わない。
  適用に使うのは同梱ファイル実体（added_files/）と削除一覧（deleted_files）だけである。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from typing_extensions import Self

from .errors import PackageValidationError
from .fileio import atomic_write_text
from .security import safe_relative_path, sha256_bytes, sign_document, verify_document

SCHEMA_VERSION = 1
PACKAGE_TYPE = "myao-patch-bridge"
LEGACY_PACKAGE_TYPES = {"myao-rep-patch"}
INDEX_NAME = "package-index.json"
MAX_ZIP_FILES = 20_000
MAX_ZIP_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 20 * 1024 * 1024
MAX_CHUNK_BYTES = 50 * 1024 * 1024
MAX_ADDED_FILE_BYTES = 500 * 1024 * 1024


def write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageValidationError(f"JSONを読み込めません: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PackageValidationError(f"JSONのルートはオブジェクトである必要があります: {path}")
    return value


def package_dir(repo_id: str, sequence: int) -> PurePosixPath:
    """パッチ専用リポジトリ（およびZIP）内でのパッケージ配置パス"""
    return PurePosixPath("packages") / repo_id / f"{sequence:06d}"


def index_entry(repo_id: str, sequence: int) -> dict[str, Any]:
    return {
        "repo_id": repo_id,
        "sequence": sequence,
        "manifest_path": str(package_dir(repo_id, sequence) / "manifest.json"),
    }


def build_index(manifests: list[dict[str, Any]], password: str) -> dict[str, Any]:
    """指定したマニフェストだけを載せた署名付き package-index.json を組み立てる"""
    return sign_document(
        {
            "schema_version": SCHEMA_VERSION,
            "package_type": PACKAGE_TYPE,
            "packages": [
                index_entry(str(manifest["repo_id"]), int(manifest["sequence"]))
                for manifest in manifests
            ],
        },
        password,
    )


def new_index(password: str) -> dict[str, Any]:
    return build_index([], password)


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


def added_part_name(safe_path: str, index: int) -> str:
    """分割された同梱ファイル実体のパーツ名（パッケージ内の added_parts/ からの相対パス）"""
    return f"{safe_path}.part-{index:06d}"


def split_added_file(
    data: bytes, safe_path: str, target_dir: Path, chunk_size: int
) -> list[dict[str, Any]]:
    """
    同梱ファイル実体を chunk_size ごとに分割して target_dir/added_parts/ へ書き出す。

    GitHubは1ファイル100MBまでしか受け付けないため、大きな実体は分割して運び、会社側で結合する。
    返り値はマニフェストの added_files[].parts に記録する（順序、サイズ、SHA-256）。
    """
    parts: list[dict[str, Any]] = []
    for index, offset in enumerate(range(0, len(data), chunk_size), start=1):
        value = data[offset : offset + chunk_size]
        part_path = target_dir / "added_parts" / added_part_name(safe_path, index)
        part_path.parent.mkdir(parents=True, exist_ok=True)
        part_path.write_bytes(value)
        parts.append({"size": len(value), "sha256": sha256_bytes(value)})
    return parts


def resolve_added_file_content(
    content: bytes, expected_size: int, expected_sha256: str
) -> bytes | None:
    """
    追加ファイルの生バイト列を検証し、期待ハッシュに一致するバイト列を返す。
    Gitの自動改行コード正規化（eol/autocrlf）やアーカイブ生成時等で CRLF ↔ LF 変換が生じた場合でも、
    透過的に検知して元の期待バイト列を復元する。
    """
    if len(content) == expected_size and sha256_bytes(content) == expected_sha256:
        return content
    # LF -> CRLF 変換の検証（元ファイルがCRLFでアーカイブ内がLFの場合）
    crlf_candidate = content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if len(crlf_candidate) == expected_size and sha256_bytes(crlf_candidate) == expected_sha256:
        return crlf_candidate
    # CRLF -> LF 変換の検証（元ファイルがLFでアーカイブ内がCRLFの場合）
    lf_candidate = content.replace(b"\r\n", b"\n")
    if len(lf_candidate) == expected_size and sha256_bytes(lf_candidate) == expected_sha256:
        return lf_candidate
    return None


@dataclass
class ArchivePackage:
    manifest_path: str
    manifest: dict[str, Any]

    @property
    def repo_id(self) -> str:
        return str(self.manifest["repo_id"])

    @property
    def sequence(self) -> int:
        return int(self.manifest["sequence"])

    @property
    def display_name(self) -> str:
        return str(self.manifest.get("display_name") or self.repo_id)


class PatchArchive:
    """Safely reads a GitHub source ZIP without extracting it wholesale."""

    def __init__(self, path: Path, password: str):
        self.path = path
        self.password = password
        try:
            self.zip = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise PackageValidationError(f"ZIPを開けません: {exc}") from exc
        try:
            self._validate_members()
            self.root_prefix, self.index = self._load_index()
            self.packages = self._load_manifests()
        except BaseException:
            # 検証に失敗したZIPを開いたままにしない（Windowsではファイルがロックされ続ける）
            self.zip.close()
            raise

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
            if PurePosixPath(name).name == INDEX_NAME
        ]
        if len(candidates) != 1:
            raise PackageValidationError("package-index.jsonを1つだけ含む必要があります")
        name = candidates[0]
        raw = self._read_limited(name, MAX_JSON_BYTES)
        try:
            index = json.loads(raw.decode("utf-8-sig"))
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
                manifest = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PackageValidationError(f"マニフェストが不正です: {relative}: {exc}") from exc
            if not isinstance(manifest, dict):
                raise PackageValidationError(f"マニフェストが不正です: {relative}")
            verify_document(manifest, self.password)
            if manifest.get("schema_version") != SCHEMA_VERSION:
                raise PackageValidationError(f"未対応のマニフェストです: {relative}")
            sequence = manifest.get("sequence")
            if not isinstance(manifest.get("repo_id"), str) or not isinstance(sequence, int) or sequence < 1:
                raise PackageValidationError(f"マニフェストのリポジトリIDまたは連番が不正です: {relative}")
            key = (manifest["repo_id"], sequence)
            if key in seen:
                raise PackageValidationError(f"パッケージ連番が重複しています: {key}")
            seen.add(key)
            self._verify_chunks(relative, manifest)
            self._verify_added_files(relative, manifest)
            result.append(ArchivePackage(relative, manifest))
        return sorted(result, key=lambda package: (package.repo_id, package.sequence))

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
            value = self._read_limited(archive_name, MAX_CHUNK_BYTES)
            if len(value) != chunk.get("size") or sha256_bytes(value) != chunk.get("sha256"):
                raise PackageValidationError(f"パッチ分割ファイルが破損しています: {name}")
            digest.update(value)
            total_size += len(value)
        if total_size != manifest.get("patch_size"):
            raise PackageValidationError("復元パッチのサイズが一致しません")
        if digest.hexdigest() != manifest.get("patch_sha256"):
            raise PackageValidationError("復元パッチのSHA-256が一致しません")

    def _verify_added_files(self, manifest_path: str, manifest: dict[str, Any]) -> None:
        added_files = manifest.get("added_files")
        if added_files is None:
            return
        if not isinstance(added_files, list):
            raise PackageValidationError(f"added_filesが不正です: {manifest_path}")
        for record in added_files:
            if not isinstance(record, dict):
                raise PackageValidationError(f"added_filesの要素が不正です: {manifest_path}")
            self._read_added_record(manifest_path, record)

    def _read_added_record(self, manifest_path: str, record: dict[str, Any]) -> bytes:
        """同梱ファイル実体を読み、マニフェスト記載のサイズ・SHA-256と一致するバイト列を返す"""
        safe_path = str(safe_relative_path(str(record.get("path", ""))))
        base = PurePosixPath(manifest_path).parent
        size = record.get("size")
        expected_size = size if isinstance(size, int) else -1
        parts = record.get("parts")
        if parts is None:
            archive_name = self.root_prefix + str(base / "added_files" / safe_path)
            content = self._read_limited(archive_name, MAX_ADDED_FILE_BYTES)
            resolved = resolve_added_file_content(content, expected_size, str(record.get("sha256", "")))
            if resolved is None:
                raise PackageValidationError(f"追加ファイルが破損しています: {safe_path}")
            return resolved

        # 分割されたファイル: パーツごとに照合してから結合し、全体のサイズとSHA-256も照合する
        if not isinstance(parts, list) or not parts or expected_size < 0 or expected_size > MAX_ADDED_FILE_BYTES:
            raise PackageValidationError(f"追加ファイルの分割情報が不正です: {safe_path}")
        buffer = bytearray()
        for index, part in enumerate(parts, start=1):
            if not isinstance(part, dict):
                raise PackageValidationError(f"追加ファイルの分割情報が不正です: {safe_path}")
            archive_name = self.root_prefix + str(base / "added_parts" / added_part_name(safe_path, index))
            value = self._read_limited(archive_name, MAX_CHUNK_BYTES)
            if len(value) != part.get("size") or sha256_bytes(value) != part.get("sha256"):
                raise PackageValidationError(f"追加ファイルの分割パーツが破損しています: {safe_path} ({index}/{len(parts)})")
            buffer.extend(value)
            if len(buffer) > expected_size:
                break
        content = bytes(buffer)
        if len(content) != expected_size or sha256_bytes(content) != record.get("sha256"):
            raise PackageValidationError(f"追加ファイルの結合結果が一致しません: {safe_path}")
        return content

    def read_added_file(self, package: ArchivePackage, relative_path: str) -> bytes:
        safe_path = str(safe_relative_path(relative_path))
        for record in package.manifest.get("added_files") or []:
            if isinstance(record, dict) and str(safe_relative_path(str(record.get("path", "")))) == safe_path:
                return self._read_added_record(package.manifest_path, record)
        raise PackageValidationError(f"マニフェストに記載のない追加ファイルです: {safe_path}")

    def package_groups(self) -> dict[str, list[ArchivePackage]]:
        result: dict[str, list[ArchivePackage]] = {}
        for package in self.packages:
            result.setdefault(package.repo_id, []).append(package)
        return result

    def summary(self) -> dict[str, Any]:
        groups = self.package_groups()
        return {
            "path": str(self.path),
            "package_count": len(self.packages),
            "repository_count": len(groups),
            "repositories": [
                {
                    "repo_id": repo_id,
                    "display_name": packages[-1].display_name,
                    "kind": str(packages[-1].manifest.get("kind", "app")),
                    "first_sequence": packages[0].sequence,
                    "last_sequence": packages[-1].sequence,
                    "total_patch_size": sum(int(item.manifest.get("patch_size", 0)) for item in packages),
                }
                for repo_id, packages in groups.items()
            ],
        }


def create_release_bundle(
    patch_root: Path,
    created_manifests: list[dict[str, Any]],
    password: str,
    output_dir: Path,
    bundle_name: str | None = None,
) -> Path:
    """今回作成されたマニフェストのみを含む軽量な配布用ZIPアーカイブを生成する"""
    if not created_manifests:
        raise PackageValidationError("公開対象のマニフェストがありません")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not bundle_name:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        bundle_name = f"myao_app_patch_{timestamp}.zip"
    zip_path = output_dir / bundle_name

    bundle_index = build_index(created_manifests, password)

    temp_zip = zip_path.with_suffix(zip_path.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            index_bytes = (json.dumps(bundle_index, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            zf.writestr(INDEX_NAME, index_bytes)

            for manifest in created_manifests:
                relative_dir = package_dir(str(manifest["repo_id"]), int(manifest["sequence"]))
                target_dir = patch_root.joinpath(*relative_dir.parts)
                if not target_dir.exists():
                    raise PackageValidationError(f"パッチディレクトリが存在しません: {target_dir}")
                for item in sorted(target_dir.rglob("*")):
                    if item.is_file():
                        zf.write(item, str(relative_dir / item.relative_to(target_dir).as_posix()))
        temp_zip.replace(zip_path)
    except BaseException:
        temp_zip.unlink(missing_ok=True)
        raise
    return zip_path

