"""
アプリケーション設定およびリポジトリ設定の定義・永続化モジュール

仕様:
- RepositoryConfig: 単一リポジトリの設定（repo_id, display_name, path, kind, enabled, branch, baseline_commit, published_commit, last_sequence, clean_verify_ignore_patterns）
- Settings: アプリケーション全体設定（mode, 各種ルートパス, パスワード, 待受ポート, 分割サイズ, repositories, company_excluded_repo_ids, github_token, github_repo）
- SettingsStore: 設定のローカルJSON永続化（data/settings.local.json）、待受ホスト等のバリデーション、および引数オーバーライド
"""
from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import RepPatchError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
SETTINGS_PATH = DATA_DIR / "settings.local.json"

_HOSTNAME_REGEX = re.compile(
    r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


def is_valid_host(host: str) -> bool:
    """ホストが有効なIPアドレスまたはホスト名/FQDNであるかを判定する"""
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return bool(_HOSTNAME_REGEX.match(host))


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
    mode: str = "company" if platform.system() == "Windows" else "home"
    apps_root: str = ""
    obsidian_repo: str = ""
    patch_repo: str = ""
    company_apps_root: str = ""
    company_obsidian_repo: str = ""
    download_dir: str = str(Path.home() / "Downloads")
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
        value["patch_password"] = "" if not self.patch_password else "********"
        value["password_configured"] = bool(self.patch_password)
        value["github_token"] = "" if not self.github_token else "********"
        value["github_token_configured"] = bool(self.github_token)
        return value


class SettingsStore:
    def __init__(self, path: Path = SETTINGS_PATH, overrides: dict[str, Any] | None = None):
        self.path = path
        self.overrides = dict(overrides or {})

    def load(self) -> Settings:
        if not self.path.exists():
            base = Settings()
        else:
            try:
                base = Settings.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError) as exc:
                raise RepPatchError(f"設定ファイルを読み込めません: {exc}") from exc
        if self.overrides:
            for key, value in self.overrides.items():
                if hasattr(base, key) and value is not None:
                    setattr(base, key, value)
        return base

    def save(self, settings: Settings) -> Settings:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(settings)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.path)
        return settings

    def update(self, updates: dict[str, Any]) -> Settings:
        current = self.load()
        raw = asdict(current)
        if updates.get("patch_password") == "********":
            updates = dict(updates)
            updates.pop("patch_password")
        if updates.get("github_token") == "********":
            updates = dict(updates)
            updates.pop("github_token")
        raw.update(updates)
        updated = Settings.from_dict(raw)
        if updated.mode not in {"home", "company"}:
            raise RepPatchError("mode は home または company で指定してください")
        if not is_valid_host(updated.listen_host):
            raise RepPatchError("待受ホストは有効なIPアドレス（127.0.0.1、0.0.0.0 など）またはホスト名を指定してください")
        if not 1 <= updated.listen_port <= 65535:
            raise RepPatchError("ポート番号が不正です")
        if not 1 <= updated.chunk_size_mib <= 40:
            raise RepPatchError("分割サイズは1〜40 MiBで指定してください")
        return self.save(updated)
