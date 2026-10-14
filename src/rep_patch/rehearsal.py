"""
公開前リハーサル（会社側での検証・適用を、自宅側で実際に実行して確かめる）モジュール

会社PCで起きるエラーを、pushする前に自宅で起こすための仕組み。マニフェストの計算結果ではなく
「会社へ実際に届くZIP」を作り、会社側と同じコード（PatchArchive / apply_archive）で処理する。

仕様:
- build_download_zip: パッチ専用リポジトリのコミット済みの内容から、GitHubの「Code → Download ZIP」と
  同じ方法（git archive）でZIPを作る。作業ツリーではなくコミットから作るので、.gitignore で
  コミットから漏れたファイルや、同梱した .gitattributes による変換・除外もそのまま現れる
- rehearse_archive: ZIPを会社側と同じ手順で検証・適用する
  1. ZIPを開く（署名、分割パッチ、同梱ファイル実体のサイズとSHA-256の検証）
  2. パッケージごとに「適用前の会社側リポジトリ」を一時フォルダへ再現する
     （公開元コミットの内容で、差分に関わるパスだけを配置してコミット）
  3. 会社側の適用処理をそのまま実行する
  4. 適用後の内容が、公開先コミットの内容と1バイトも違わないことを確認する
     （届け漏れ・消し漏れは、マニフェストとは独立にGitのツリー同士を比べて検出する）
- rehearse_commit: 上の2つを続けて実行する

問題があれば RepPatchError を送出する。メッセージには、問題のあったリポジトリとパスを含める。
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .company import GIT_USER_FALLBACK, apply_archive, init_repository_sequence
from .config import Settings
from .errors import RepPatchError
from .git import read_blobs, run_git, tracked_files
from .packages import ArchivePackage, PatchArchive
from .security import safe_repo_path

# エラーメッセージへ列挙するパスの上限（多すぎると画面で読めなくなる）
_MAX_REPORTED_PATHS = 5


def build_download_zip(patch_root: Path, revision: str, output: Path) -> Path:
    """revision のコミット済み内容から、GitHubの「Download ZIP」と同じ内容のZIPを作る"""
    run_git(
        patch_root,
        [
            # GitHubのサーバー（Linux）と同じ条件にする。利用者のGit設定で結果が変わらないようにする
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            "archive",
            "--format=zip",
            "-0",  # 無圧縮。中身は同じで、検証のためだけに圧縮する時間を省く
            f"--prefix={patch_root.name}-rehearsal/",
            "-o",
            str(output),
            revision,
        ],
    )
    return output


def _blob_entries(repo: Path, revision: str) -> dict[str, str]:
    """revision 時点の通常ファイル（パス → blob ID）。サブモジュールは含めない"""
    return {item["path"]: item["blob"] for item in tracked_files(repo, revision) if item["type"] == "blob"}


def _manifest_paths(packages: list[ArchivePackage]) -> set[str]:
    paths: set[str] = set()
    for package in packages:
        manifest = package.manifest
        paths.update(manifest.get("changed_paths") or [])
        paths.update(record["path"] for record in manifest.get("added_files") or [])
        paths.update(manifest.get("deleted_files") or [])
    return paths


def _summarize(paths: list[str]) -> str:
    listed = ", ".join(f"'{path}'" for path in paths[:_MAX_REPORTED_PATHS])
    if len(paths) > _MAX_REPORTED_PATHS:
        listed += f" ほか{len(paths) - _MAX_REPORTED_PATHS}件"
    return listed


def _file_remains(path: Path) -> bool:
    """
    消えているはずのファイルが残っているかを判定する。

    大文字小文字を区別しないファイルシステムでは、名前の大文字小文字だけを変えた新しいファイルも
    同じパスとして見えるので、フォルダ内の実際の名前で照合する。同じ名前のフォルダへ置き換わった
    場合は、残っているとはみなさない。
    """
    try:
        return path.name in os.listdir(path.parent) and not path.is_dir()
    except OSError:
        return False


class _SimulatedRepository:
    """1リポジトリ分の「適用前の会社側リポジトリ」と、適用後に確認すべき内容"""

    def __init__(self, repo_id: str, packages: list[ArchivePackage], source_repo: Path, root: Path):
        self.repo_id = repo_id
        self.display_name = packages[-1].display_name
        self.first_sequence = packages[0].sequence
        self.root = root
        self.source_repo = source_repo
        before = _blob_entries(source_repo, str(packages[0].manifest["source_from_commit"]))
        self.after = _blob_entries(source_repo, str(packages[-1].manifest["source_to_commit"]))
        # マニフェストの記載に加えて、Gitのツリー同士を比べて変化したパスも確認対象にする
        changed = {path for path, blob in self.after.items() if before.get(path) != blob}
        removed = set(before) - set(self.after)
        self.paths = sorted(_manifest_paths(packages) | changed | removed)
        self._before = {path: before[path] for path in self.paths if path in before}

    def create(self) -> None:
        self.root.mkdir(parents=True)
        run_git(self.root, ["init", "-q"])
        # 利用者のグローバル設定（コミット署名、フック、改行変換）に左右されないようにする
        hooks = self.root.parent / "no-hooks"
        hooks.mkdir(exist_ok=True)
        for key, value in (
            ("commit.gpgsign", "false"),
            ("core.autocrlf", "false"),
            ("core.hooksPath", str(hooks)),
        ):
            run_git(self.root, ["config", key, value])
        blobs = read_blobs(self.source_repo, self._before.values())
        for path, blob in self._before.items():
            target = safe_repo_path(self.root, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blobs[blob])
        run_git(self.root, ["add", "-A"])
        run_git(self.root, [*GIT_USER_FALLBACK, "commit", "-q", "--allow-empty", "-m", "before patch"])

    def mismatches(self) -> list[str]:
        """適用後の内容が公開先コミットと食い違っているパス"""
        expected = {path: self.after[path] for path in self.paths if path in self.after}
        blobs = read_blobs(self.source_repo, expected.values())
        wrong: list[str] = []
        for path in self.paths:
            actual = self.root.joinpath(*PurePosixPath(path).parts)
            if path in expected:
                if not actual.is_file() or actual.read_bytes() != blobs[expected[path]]:
                    wrong.append(path)
            elif _file_remains(actual):
                wrong.append(path)
        return wrong


def rehearse_archive(zip_path: Path, password: str, sources: Mapping[str, Path]) -> dict[str, Any]:
    """
    ZIPを会社側と同じ手順で検証・適用し、結果が公開先コミットと一致することを確認する。

    sources: リポジトリID → 自宅側リポジトリのパス（公開元・公開先コミットの内容を読むために使う）
    """
    with PatchArchive(zip_path, password) as archive:
        groups = archive.package_groups()

    with tempfile.TemporaryDirectory(prefix="patch-bridge-rehearsal-") as temp:
        problems: list[str] = []
        simulated: dict[str, _SimulatedRepository] = {}
        settings = Settings(mode="company", download_dir=str(zip_path.parent), patch_password=password)
        for index, (repo_id, packages) in enumerate(groups.items()):
            source_repo = sources.get(repo_id)
            if source_repo is None:
                problems.append(f"[{repo_id}] 自宅側のリポジトリが登録されていないため、適用を確認できません")
                continue
            try:
                repository = _SimulatedRepository(repo_id, packages, source_repo, Path(temp) / f"repo-{index}")
                repository.create()
            except RepPatchError as exc:
                problems.append(f"[{packages[-1].display_name}] 適用前の状態を再現できません: {exc}")
                continue
            simulated[repo_id] = repository
            settings.company_repo_paths[repo_id] = str(repository.root)
            init_repository_sequence(settings, repo_id, repository.first_sequence)

        outcome = apply_archive(settings, str(zip_path))
        checked_paths = 0
        for result in outcome["results"]:
            repository = simulated.get(result["repo_id"])
            if repository is None:
                continue
            if result["status"] != "applied":
                problems.append(f"[{repository.display_name}] 適用できません: {result['message']}")
                continue
            wrong = repository.mismatches()
            checked_paths += len(repository.paths)
            if wrong:
                problems.append(
                    f"[{repository.display_name}] 適用後の内容が公開先コミットと一致しません: {_summarize(wrong)}"
                )
        if problems:
            raise RepPatchError(" / ".join(problems))
        return {"repositories": len(simulated), "checked_paths": checked_paths}


def rehearse_commit(
    patch_root: Path, revision: str, password: str, sources: Mapping[str, Path]
) -> dict[str, Any]:
    """パッチ専用リポジトリの revision を「Download ZIP」相当のZIPにして、会社側の検証・適用をリハーサルする"""
    with tempfile.TemporaryDirectory(prefix="patch-bridge-download-") as temp:
        zip_path = build_download_zip(patch_root, revision, Path(temp) / "myao_app_patch-rehearsal.zip")
        return rehearse_archive(zip_path, password, sources)
