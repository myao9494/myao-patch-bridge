"""
公開前リハーサル（rehearsal.py）と、それを組み込んだ公開処理のテスト

会社PCで起きるエラーを、pushする前に自宅で検出できることを確認する。

仕様:
- アプリの .gitignore の対象でもある追跡ファイルを同梱しても、コミットとZIPから漏れない
  （実際に起きた不具合: 同梱した .gitignore がパッチ専用リポジトリの中で効き、ZIPにファイルが入らなかった）
- リハーサルは、作業ツリーではなく「Download ZIP」相当のZIP（コミット済みの内容）で行う
- リハーサルが失敗したら、コミットを取り消し、pushせず、連番も進めず、作りかけを残さない
- 同梱した .gitattributes（export-ignore）でZIPからファイルが落ちる場合も、公開前に検出する
- 大文字小文字だけの名前変更、ファイルとフォルダの置き換えを含む差分も、会社側へ正しく適用できる
  （大文字小文字だけの名前変更では、古い名前を削除一覧に載せない。載っている古いパッケージでも実体を失わない）
- フォルダをファイルへ置き換えられない場合は、会社側リポジトリを適用前の状態へ戻す
- マニフェストに載っていない変更（届け漏れ）を、Gitのツリー同士の比較で検出する
- 自宅側に登録の無いリポジトリのパッケージは、確認できないものとして報告する
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import rep_patch.home as home_module
from helpers import commit_all, git, init_repo, make_patch_repo
from rep_patch.company import apply_archive, load_state
from rep_patch.config import RepositoryConfig, Settings, SettingsStore
from rep_patch.errors import RepPatchError
from rep_patch.git import run_git
from rep_patch.home import publish
from rep_patch.rehearsal import build_download_zip, rehearse_archive, rehearse_commit
from rep_patch.security import sign_document

PASSWORD = "test-password"


def _home(tmp_path: Path) -> tuple[SettingsStore, Path, Path]:
    """v1 を公開済み地点（baseline）とする自宅側リポジトリと、リモート付きパッチ専用リポジトリ"""
    patch_repo = make_patch_repo(tmp_path)
    source = init_repo(tmp_path / "source" / "sample-app")
    (source / "app.txt").write_text("v1\n", encoding="utf-8")
    baseline = commit_all(source, "v1")
    store = SettingsStore(tmp_path / "data" / "settings.local.json")
    store.save(
        Settings(
            mode="home",
            patch_repo=str(patch_repo),
            patch_password=PASSWORD,
            download_dir=str(tmp_path / "bundles"),
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
    )
    return store, patch_repo, source


def _rebase_baseline(store: SettingsStore, source: Path, message: str = "baseline") -> None:
    """現在の作業ツリーをコミットし、そこを公開済み地点（会社側と同じ状態）にする"""
    settings = store.load()
    settings.repositories["sample-app"].baseline_commit = commit_all(source, message)
    store.save(settings)


def _company_copy(tmp_path: Path, source: Path, revision: str) -> Path:
    """source の revision 時点の内容を持つ、独立した履歴の会社側リポジトリ"""
    company = init_repo(tmp_path / "company-apps" / "sample-app")
    archive = tmp_path / "company-seed.zip"
    git(source, "archive", "--format=zip", "-o", str(archive), revision)
    with zipfile.ZipFile(archive) as seed:
        seed.extractall(company)
    commit_all(company, "company baseline")
    return company


def _download_zip(tmp_path: Path, patch_repo: Path) -> Path:
    downloads = tmp_path / "downloads"
    downloads.mkdir(exist_ok=True)
    return build_download_zip(patch_repo, "HEAD", downloads / "myao_app_patch-main.zip")


def _company_settings(tmp_path: Path) -> Settings:
    return Settings(
        mode="company",
        company_apps_root=str(tmp_path / "company-apps"),
        patch_password=PASSWORD,
        download_dir=str(tmp_path / "downloads"),
    )


def _rewrite_manifest(zip_path: Path, **changes: object) -> None:
    """ZIP内のマニフェストの項目を書き換え、署名し直す"""
    with zipfile.ZipFile(zip_path) as original:
        members = {name: original.read(name) for name in original.namelist() if not name.endswith("/")}
    manifest_name = next(name for name in members if name.endswith("manifest.json"))
    manifest = {key: value for key, value in json.loads(members[manifest_name]).items() if key != "signature"}
    manifest.update(changes)
    members[manifest_name] = json.dumps(sign_document(manifest, PASSWORD)).encode("utf-8")
    with zipfile.ZipFile(zip_path, "w") as rewritten:
        for name, content in members.items():
            rewritten.writestr(name, content)


def _status(repo: Path) -> str:
    return git(repo, "status", "--porcelain=v1", "--ignored").decode().strip()


def _add_ignored_but_tracked_files(source: Path) -> None:
    (source / ".gitignore").write_text("backend/settings.local.json\nbuild/\n.DS_Store\n", encoding="utf-8")
    (source / "backend").mkdir()
    (source / "backend" / "settings.local.json").write_text('{"mode": "tab"}\n', encoding="utf-8")
    (source / "build").mkdir()
    (source / "build" / "bundle.js").write_text("console.log(1)\n", encoding="utf-8")
    (source / ".DS_Store").write_bytes(b"\x00\x01finder")
    git(source, "add", "-A")
    git(source, "add", "-f", "backend/settings.local.json", "build/bundle.js", ".DS_Store")
    git(source, "commit", "-m", "track files that .gitignore excludes")


def test_publish_ships_tracked_files_that_the_app_gitignore_excludes(tmp_path: Path) -> None:
    store, patch_repo, source = _home(tmp_path)
    company = _company_copy(tmp_path, source, "HEAD")
    # パッチ専用リポジトリ自身の .gitignore も、同梱ファイルを巻き込んではならない
    (patch_repo / ".gitignore").write_text(".DS_Store\n", encoding="utf-8")
    commit_all(patch_repo, "ignore finder files")
    _add_ignored_but_tracked_files(source)

    result = publish(store.load(), store)

    assert result["published"] is True
    committed = git(patch_repo, "ls-tree", "-r", "--name-only", "HEAD").decode().splitlines()
    base = "packages/sample-app/000001/added_files/"
    for name in (".gitignore", "backend/settings.local.json", "build/bundle.js", ".DS_Store"):
        assert base + name in committed
    assert _status(patch_repo) == ""
    # 上書きに気付けるよう、該当ファイルを警告として知らせる
    assert len(result["warnings"]) == 3
    assert any("backend/settings.local.json" in warning for warning in result["warnings"])
    assert all(warning.startswith("[sample-app]") for warning in result["warnings"])

    # GitHubの「Download ZIP」と同じZIPを、会社側へ実際に適用できる
    applied = apply_archive(_company_settings(tmp_path), str(_download_zip(tmp_path, patch_repo)))
    assert applied["failed"] == 0, applied["results"]
    assert (company / "backend" / "settings.local.json").read_text(encoding="utf-8") == '{"mode": "tab"}\n'
    assert (company / "build" / "bundle.js").exists()
    assert load_state(company, "sample-app")["confirmed_sequence"] == 1


def test_publish_is_cancelled_when_rehearsal_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, patch_repo, source = _home(tmp_path)
    remote = tmp_path / "patch-remote.git"
    head_before = git(patch_repo, "rev-parse", "HEAD")
    _add_ignored_but_tracked_files(source)

    # 以前の実装（除外ルールに従う git add）を再現する
    with monkeypatch.context() as patched:
        patched.setattr(home_module, "_stage_packages", lambda root, created: run_git(root, ["add", "-A"]))
        # 「検証をスキップして公開」でも、リハーサルは省略されない
        with pytest.raises(RepPatchError, match="公開前リハーサル.*ZIP内のファイルがありません") as error:
            publish(store.load(), store, skip_verification=True)
    assert "packages/sample-app/000001/added_files/" in str(error.value)
    # 画面の「検証をスキップして公開」の対象にはしない
    assert "会社環境検証" not in str(error.value)

    # コミットは取り消され、pushもされず、無視されたファイルも含めて作りかけが残らない
    assert git(patch_repo, "rev-parse", "HEAD") == head_before
    assert git(remote, "rev-parse", "main") == head_before
    assert _status(patch_repo) == ""
    assert not (patch_repo / "packages").exists()
    config = store.load().repositories["sample-app"]
    assert (config.published_commit, config.last_sequence) == ("", 0)
    assert list(tmp_path.glob("bundles/*.zip")) == []

    # 原因が解消されれば、同じ連番でそのまま公開できる
    result = publish(store.load(), store)
    assert [package["sequence"] for package in result["packages"]] == [1]
    assert git(remote, "rev-parse", "main") == git(patch_repo, "rev-parse", "HEAD")


def test_rehearsal_detects_files_dropped_from_the_download_zip(tmp_path: Path) -> None:
    """同梱した .gitattributes の export-ignore は、GitHubのZIPからファイルを落とす"""
    store, patch_repo, source = _home(tmp_path)
    head_before = git(patch_repo, "rev-parse", "HEAD")
    (source / ".gitattributes").write_text("internal.txt export-ignore\n", encoding="utf-8")
    (source / "internal.txt").write_text("not exported\n", encoding="utf-8")
    commit_all(source, "add export-ignore")

    with pytest.raises(RepPatchError, match="公開前リハーサル.*internal.txt"):
        publish(store.load(), store)
    assert git(patch_repo, "rev-parse", "HEAD") == head_before
    assert _status(patch_repo) == ""


def test_case_only_rename_is_applied_without_losing_the_file(tmp_path: Path) -> None:
    store, patch_repo, source = _home(tmp_path)
    (source / "Readme.md").write_text("old\n", encoding="utf-8")
    _rebase_baseline(store, source)
    company = _company_copy(tmp_path, source, "HEAD")

    git(source, "mv", "Readme.md", "README.md")
    (source / "README.md").write_text("new\n", encoding="utf-8")
    commit_all(source, "rename by case only")

    result = publish(store.load(), store)
    # 古い名前は削除一覧に載せない（削除を後から実行する版の会社側アプリでも実体を失わないように）
    assert result["packages"][0]["deleted_files"] == []
    assert [record["path"] for record in result["packages"][0]["added_files"]] == ["README.md"]
    applied = apply_archive(_company_settings(tmp_path), str(_download_zip(tmp_path, patch_repo)))

    assert applied["failed"] == 0, applied["results"]
    assert "README.md" in [item.name for item in company.iterdir()]
    assert "Readme.md" not in [item.name for item in company.iterdir()]
    assert (company / "README.md").read_text(encoding="utf-8") == "new\n"
    assert git(company, "status", "--porcelain=v1").decode().strip() == ""


def test_case_only_rename_listed_as_deleted_and_added_keeps_the_file(tmp_path: Path) -> None:
    """以前の版が作ったパッケージ（古い名前が削除一覧にも載っている）でも、実体を失わない"""
    store, patch_repo, source = _home(tmp_path)
    (source / "Readme.md").write_text("old\n", encoding="utf-8")
    _rebase_baseline(store, source)
    company = _company_copy(tmp_path, source, "HEAD")
    git(source, "mv", "Readme.md", "README.md")
    (source / "README.md").write_text("new\n", encoding="utf-8")
    commit_all(source, "rename by case only")
    assert publish(store.load(), store)["published"] is True

    zip_path = _download_zip(tmp_path, patch_repo)
    _rewrite_manifest(zip_path, deleted_files=["Readme.md"])

    applied = apply_archive(_company_settings(tmp_path), str(zip_path))

    assert applied["failed"] == 0, applied["results"]
    assert [item.name for item in company.iterdir() if item.name.lower() == "readme.md"] == ["README.md"]
    assert (company / "README.md").read_text(encoding="utf-8") == "new\n"


def test_file_and_folder_replacements_are_applied(tmp_path: Path) -> None:
    store, patch_repo, source = _home(tmp_path)
    (source / "to_folder").write_text("was a file\n", encoding="utf-8")
    (source / "to_file").mkdir()
    (source / "to_file" / "inner.txt").write_text("was in a folder\n", encoding="utf-8")
    (source / "emptied" / "deep").mkdir(parents=True)
    (source / "emptied" / "deep" / "only.txt").write_text("last file\n", encoding="utf-8")
    _rebase_baseline(store, source)
    company = _company_copy(tmp_path, source, "HEAD")

    (source / "to_folder").unlink()
    (source / "to_folder").mkdir()
    (source / "to_folder" / "inner.txt").write_text("now in a folder\n", encoding="utf-8")
    (source / "to_file" / "inner.txt").unlink()
    (source / "to_file").rmdir()
    (source / "to_file").write_text("now a file\n", encoding="utf-8")
    (source / "emptied" / "deep" / "only.txt").unlink()
    commit_all(source, "swap files and folders")

    assert publish(store.load(), store)["published"] is True
    applied = apply_archive(_company_settings(tmp_path), str(_download_zip(tmp_path, patch_repo)))

    assert applied["failed"] == 0, applied["results"]
    assert (company / "to_folder" / "inner.txt").read_text(encoding="utf-8") == "now in a folder\n"
    assert (company / "to_file").read_text(encoding="utf-8") == "now a file\n"
    # 最後のファイルが消えて空になったフォルダは残さない
    assert not (company / "emptied").exists()
    assert git(company, "status", "--porcelain=v1").decode().strip() == ""


def test_failed_folder_replacement_restores_the_company_repository(tmp_path: Path) -> None:
    """フォルダをファイルへ置き換えられない（会社側だけのファイルが中にある）場合、適用前の状態へ戻す"""
    store, patch_repo, source = _home(tmp_path)
    (source / "to_file").mkdir()
    (source / "to_file" / "inner.txt").write_text("was in a folder\n", encoding="utf-8")
    _rebase_baseline(store, source)
    company = _company_copy(tmp_path, source, "HEAD")
    (company / ".gitignore").write_text("to_file/local.cache\n", encoding="utf-8")
    commit_all(company, "company ignores a local file")
    (company / "to_file" / "local.cache").write_text("company only\n", encoding="utf-8")
    head_before = git(company, "rev-parse", "HEAD")

    (source / "app.txt").write_text("v2\n", encoding="utf-8")
    (source / "to_file" / "inner.txt").unlink()
    (source / "to_file").rmdir()
    (source / "to_file").write_text("now a file\n", encoding="utf-8")
    commit_all(source, "folder becomes a file")

    assert publish(store.load(), store)["published"] is True
    applied = apply_archive(_company_settings(tmp_path), str(_download_zip(tmp_path, patch_repo)))

    assert applied["failed"] == 1
    assert git(company, "rev-parse", "HEAD") == head_before
    assert (company / "app.txt").read_text(encoding="utf-8") == "v1\n"
    assert (company / "to_file" / "inner.txt").read_text(encoding="utf-8") == "was in a folder\n"
    assert (company / "to_file" / "local.cache").read_text(encoding="utf-8") == "company only\n"
    assert git(company, "status", "--porcelain=v1").decode().strip() == ""
    assert load_state(company, "sample-app")["confirmed_sequence"] == 0


def test_rehearsal_reports_content_that_differs_from_the_published_commit(tmp_path: Path) -> None:
    """マニフェストに載っていない変更（届け漏れ）を、Gitのツリー同士の比較で検出する"""
    store, patch_repo, source = _home(tmp_path)
    (source / "app.txt").write_text("v2\n", encoding="utf-8")
    (source / "extra.txt").write_text("extra\n", encoding="utf-8")
    commit_all(source, "v2")
    assert publish(store.load(), store)["published"] is True
    sources = {"sample-app": source}
    assert rehearse_commit(patch_repo, "HEAD", PASSWORD, sources) == {"repositories": 1, "checked_paths": 2}

    # 公開先コミットにあるはずの変更が、パッケージに入っていない状況を作る
    (source / "late.txt").write_text("late\n", encoding="utf-8")
    late = commit_all(source, "late change")
    zip_path = _download_zip(tmp_path, patch_repo)
    _rewrite_manifest(zip_path, source_to_commit=late)

    with pytest.raises(RepPatchError, match=r"\[sample-app\] 適用後の内容が公開先コミットと一致しません: 'late.txt'"):
        rehearse_archive(zip_path, PASSWORD, sources)


def test_rehearsal_reports_packages_of_unregistered_repositories(tmp_path: Path) -> None:
    store, patch_repo, source = _home(tmp_path)
    (source / "app.txt").write_text("v2\n", encoding="utf-8")
    commit_all(source, "v2")
    assert publish(store.load(), store)["published"] is True

    with pytest.raises(RepPatchError, match="自宅側のリポジトリが登録されていない"):
        rehearse_commit(patch_repo, "HEAD", PASSWORD, {})
