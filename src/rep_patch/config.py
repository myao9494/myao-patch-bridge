"""
アプリケーション設定およびリポジトリ設定の定義・永続化モジュール

仕様:
- RepositoryConfig: 単一リポジトリの設定（repo_id, display_name, path, kind, enabled, branch, baseline_commit, published_commit, last_sequence, clean_verify_ignore_patterns）
- Settings: アプリケーション全体設定（mode, 各種ルートパス, パスワード, 待受ホスト・ポート, 分割サイズ, repositories, company_excluded_repo_ids, github_token, github_repo）
- SettingsStore: 設定のローカルJSON永続化（data/settings.local.json）
  - load: 保存済みの設定をそのまま返す（保存してよい値）
  - load_effective: 起動引数によるオーバーライドを重ねた実行時の値を返す（保存してはならない値）
  - save / update: 原子的に書き込む。updateはバリデーションを行い、repositories は専用APIの管轄なので受け付けない
  - lock: 読み込み→変更→保存を直列化するためのロック
- repo_id_for: フォルダ名から安定したリポジトリIDを作る
- resolve_bind_host / allows_remote_clients: 待受ホスト設定の解釈
"""
from __future__ import annotations

import ipaddress
import json
import platform
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import RepPatchError
from .fileio import atomic_write_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
SETTINGS_PATH = DATA_DIR / "settings.local.json"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SECRET_MASK = "********"
MODES = ("home", "company")
REPOSITORY_KINDS = ("app", "obsidian")
OBSIDIAN_REPO_ID = "obsidian-settings"

_HOSTNAME_REGEX = re.compile(
    r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


def is_valid_host(host: str) -> bool:
    """ホストが有効なIPアドレスまたはホスト名/FQDNであるかを判定する"""
    if not isinstance(host, str) or not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return bool(_HOSTNAME_REGEX.match(host))


def allows_remote_clients(listen_host: str) -> bool:
    """待受ホスト設定がVPN/プライベートネットワークからの接続を許可するものかを返す"""
    return listen_host not in LOOPBACK_HOSTS


def resolve_bind_host(listen_host: str) -> str:
    """
    待受ホスト設定から実際にバインドするアドレスを決める。

    IPアドレスはそのまま使う。ホスト名（FQDN / MagicDNS）が指定された場合は、
    その名前宛ての通信を確実に受けるため全インターフェース（0.0.0.0）へバインドする。
    """
    try:
        ipaddress.ip_address(listen_host)
        return listen_host
    except ValueError:
        return "127.0.0.1" if listen_host == "localhost" else "0.0.0.0"


def repo_id_for(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-.").lower()
    if not slug:
        raise RepPatchError(f"リポジトリIDを作成できません: {name}")
    return slug


def _default_mode() -> str:
    return "company" if platform.system() == "Windows" else "home"


def _default_download_dir() -> str:
    return str(Path.home() / "Downloads")


@dataclass
class RepositoryConfig:
    repo_id: str
    display_name: str
    path: str
    kind: str = "app"
    enabled: bool = True
    branch: str = "main"
    baseline_commit: str = ""
    published_commit: str = ""
    last_sequence: int = 0
    clean_verify_ignore_patterns: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RepositoryConfig:
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value[key] for key in allowed if key in value})


@dataclass
class Settings:
    mode: str = field(default_factory=_default_mode)
    apps_root: str = ""
    obsidian_repo: str = ""
    patch_repo: str = ""
    company_apps_root: str = ""
    company_obsidian_repo: str = ""
    download_dir: str = field(default_factory=_default_download_dir)
    patch_password: str = ""
    listen_host: str = "127.0.0.1"
    listen_port: int = 17345
    chunk_size_mib: int = 20
    company_repo_paths: dict[str, str] = field(default_factory=dict)
    company_excluded_repo_ids: list[str] = field(default_factory=list)
    github_token: str = ""
    github_repo: str = ""
    repositories: dict[str, RepositoryConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Settings:
        raw = dict(value)
        raw["repositories"] = {
            key: RepositoryConfig.from_dict(item)
            for key, item in raw.get("repositories", {}).items()
        }
        raw["company_excluded_repo_ids"] = list(raw.get("company_excluded_repo_ids", []))
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: raw[key] for key in allowed if key in raw})

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["patch_password"] = "" if not self.patch_password else SECRET_MASK
        value["password_configured"] = bool(self.patch_password)
        value["github_token"] = "" if not self.github_token else SECRET_MASK
        value["github_token_configured"] = bool(self.github_token)
        return value

    def validate(self) -> None:
        """設定画面などから受け取った値の型と範囲を検証する"""
        text_fields = (
            "apps_root",
            "obsidian_repo",
            "patch_repo",
            "company_apps_root",
            "company_obsidian_repo",
            "download_dir",
            "patch_password",
            "github_token",
            "github_repo",
        )
        for name in text_fields:
            if not isinstance(getattr(self, name), str):
                raise RepPatchError(f"{name} は文字列で指定してください")
        if self.mode not in MODES:
            raise RepPatchError("mode は home または company で指定してください")
        if not is_valid_host(self.listen_host):
            raise RepPatchError(
                "待受ホストは有効なIPアドレス（127.0.0.1、0.0.0.0 など）またはホスト名を指定してください"
            )
        if not _is_int(self.listen_port) or not 1 <= self.listen_port <= 65535:
            raise RepPatchError("ポート番号が不正です")
        if not _is_int(self.chunk_size_mib) or not 1 <= self.chunk_size_mib <= 40:
            raise RepPatchError("分割サイズは1〜40 MiBで指定してください")
        if not isinstance(self.company_repo_paths, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in self.company_repo_paths.items()
        ):
            raise RepPatchError("company_repo_paths は リポジトリID: パス の対応で指定してください")
        if not all(isinstance(item, str) for item in self.company_excluded_repo_ids):
            raise RepPatchError("company_excluded_repo_ids は文字列の一覧で指定してください")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class SettingsStore:
    # 設定画面（PUT /api/settings）からは変更させないキー。リポジトリ登録は専用APIだけが書き換える。
    _UPDATE_IGNORED_KEYS = frozenset({"repositories"})
    _SECRET_KEYS = ("patch_password", "github_token")

    def __init__(self, path: Path = SETTINGS_PATH, overrides: dict[str, Any] | None = None):
        self.path = path
        self.overrides = {key: value for key, value in (overrides or {}).items() if value is not None}
        self.lock = threading.RLock()

    def load(self) -> Settings:
        """保存済みの設定を返す。起動引数のオーバーライドは含まないので、そのまま保存してよい。"""
        if not self.path.exists():
            return Settings()
        try:
            return Settings.from_dict(json.loads(self.path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise RepPatchError(f"設定ファイルを読み込めません: {exc}") from exc

    def load_effective(self) -> Settings:
        """起動引数（--host / --port など）を重ねた実行時の設定を返す。この値は保存しないこと。"""
        settings = self.load()
        for key, value in self.overrides.items():
            if hasattr(settings, key):
                setattr(settings, key, value)
        return settings

    def save(self, settings: Settings) -> Settings:
        payload = json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n"
        with self.lock:
            atomic_write_text(self.path, payload, private=True)
        return settings

    def update(self, updates: dict[str, Any]) -> Settings:
        changes = {
            key: value
            for key, value in updates.items()
            if key not in self._UPDATE_IGNORED_KEYS
        }
        for key in self._SECRET_KEYS:
            if changes.get(key) == SECRET_MASK:
                changes.pop(key)
        with self.lock:
            raw = asdict(self.load())
            raw.update(changes)
            try:
                updated = Settings.from_dict(raw)
            except (TypeError, ValueError, AttributeError) as exc:
                raise RepPatchError(f"設定値の形式が不正です: {exc}") from exc
            updated.validate()
            return self.save(updated)
