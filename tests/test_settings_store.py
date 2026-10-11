"""
設定の永続化（SettingsStore）のテスト

仕様:
- 起動引数のオーバーライドは実行時にだけ効き、どの保存操作でも設定ファイルへ書き込まれない
- 設定画面からの更新（update）は repositories を書き換えない（リポジトリ登録は専用APIの管轄）
- 並列に更新しても、更新の取りこぼしや一時ファイルの衝突が起きない
- 不正な型・範囲の値は RepPatchError として拒否し、設定ファイルを変更しない
- 既定のダウンロードフォルダは、インスタンス生成時点のホームディレクトリから決まる
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from helpers import auth_headers, commit_all, init_repo, make_client
from rep_patch.config import RepositoryConfig, Settings, SettingsStore
from rep_patch.errors import RepPatchError
from rep_patch.home import update_repository


def _saved(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_overrides_are_never_written_to_the_settings_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.json"
    SettingsStore(path).save(Settings(listen_host="127.0.0.1", listen_port=17345))
    store = SettingsStore(path, overrides={"listen_host": "0.0.0.0", "listen_port": 18000})

    assert store.load_effective().listen_host == "0.0.0.0"
    assert store.load().listen_host == "127.0.0.1"

    # 設定を読んで保存し直す操作（カード追加・公開などが行う）でも、起動引数の値は残らない
    store.save(store.load())
    store.update({"chunk_size_mib": 10})
    assert _saved(path)["listen_host"] == "127.0.0.1"
    assert _saved(path)["listen_port"] == 17345
    assert _saved(path)["chunk_size_mib"] == 10


def test_settings_api_reports_effective_listen_host_without_persisting_it(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.json"
    store = SettingsStore(path, overrides={"listen_host": "0.0.0.0"})
    client = make_client(store)

    payload = client.get("/api/settings").json()
    assert payload["listen_host"] == "127.0.0.1"
    assert payload["effective_listen_host"] == "0.0.0.0"

    # 画面が受け取った値をそのまま保存し直しても、起動引数の値は保存されない
    response = client.put("/api/settings", json={"values": payload}, headers=auth_headers(client))
    assert response.status_code == 200
    assert _saved(path)["listen_host"] == "127.0.0.1"
    assert "effective_listen_host" not in _saved(path)


def test_settings_update_does_not_overwrite_repositories(tmp_path: Path) -> None:
    """設定画面が起動時に読んだ古い repositories を送り返しても、その後の登録内容が巻き戻らない"""
    repo = init_repo(tmp_path / "new-app")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    commit_all(repo, "init")

    store = SettingsStore(tmp_path / "settings.local.json")
    store.save(Settings(mode="home"))
    client = make_client(store)
    headers = auth_headers(client)

    stale = client.get("/api/settings").json()
    assert stale["repositories"] == {}
    assert client.post("/api/repositories", json={"path": str(repo)}, headers=headers).status_code == 200

    response = client.put("/api/settings", json={"values": {**stale, "chunk_size_mib": 30}}, headers=headers)
    assert response.status_code == 200
    loaded = store.load()
    assert list(loaded.repositories) == ["new-app"]
    assert loaded.chunk_size_mib == 30


def test_parallel_repository_updates_are_not_lost(tmp_path: Path) -> None:
    """「すべて選択」のように全カードの更新が同時に届いても、すべて反映される"""
    repo = init_repo(tmp_path / "app")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    commit_all(repo, "init")
    count = 8
    store = SettingsStore(tmp_path / "settings.local.json")
    store.save(
        Settings(
            mode="home",
            repositories={
                f"r{i}": RepositoryConfig(f"r{i}", f"r{i}", str(repo), enabled=False) for i in range(count)
            },
        )
    )
    client = make_client(store)
    headers = auth_headers(client)
    statuses: list[int] = []

    def enable(index: int) -> None:
        response = client.put(
            f"/api/repositories/r{index}", json={"values": {"enabled": True}}, headers=headers
        )
        statuses.append(response.status_code)

    for _ in range(5):
        for config in store.load().repositories.values():
            update_repository(store.load(), store, config.repo_id, {"enabled": False})
        threads = [threading.Thread(target=enable, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert all(config.enabled for config in store.load().repositories.values())
    assert statuses == [200] * (count * 5)
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "updates",
    [
        {"mode": "office"},
        {"listen_host": "invalid host name"},
        {"listen_port": 0},
        {"listen_port": None},
        {"listen_port": "17345"},
        {"chunk_size_mib": 41},
        {"chunk_size_mib": 2.5},
        {"download_dir": None},
        {"company_repo_paths": ["not", "a", "mapping"]},
        {"company_excluded_repo_ids": [1, 2]},
    ],
)
def test_invalid_settings_are_rejected_without_saving(tmp_path: Path, updates: dict) -> None:
    path = tmp_path / "settings.local.json"
    store = SettingsStore(path)
    store.save(Settings())
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RepPatchError):
        store.update(updates)
    assert path.read_text(encoding="utf-8") == before

    client = make_client(store)
    response = client.put("/api/settings", json={"values": updates}, headers=auth_headers(client))
    assert response.status_code == 400


def test_broken_settings_file_is_reported_as_an_api_error(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.json"
    path.write_text("{ broken", encoding="utf-8")
    client = make_client(SettingsStore(path))

    # 画面（静的ファイル）は開け、APIは原因を説明するエラーを返す
    assert client.get("/").status_code == 200
    response = client.get("/api/settings")
    assert response.status_code == 400
    assert "設定ファイルを読み込めません" in response.json()["detail"]


def test_default_download_dir_follows_home_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert Settings().download_dir == str(tmp_path / "Downloads")
