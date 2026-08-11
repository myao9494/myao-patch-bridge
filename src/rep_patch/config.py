from __future__ import annotations

import json
import os
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import RepPatchError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
SETTINGS_PATH = DATA_DIR / "settings.local.json"


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
    repositories: dict[str, RepositoryConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Settings:
        raw = dict(value)
        raw["repositories"] = {
            key: RepositoryConfig.from_dict(item)
            for key, item in raw.get("repositories", {}).items()
        }
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: raw[key] for key in allowed if key in raw})

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["patch_password"] = "" if not self.patch_password else "********"
        value["password_configured"] = bool(self.patch_password)
        return value


class SettingsStore:
    def __init__(self, path: Path = SETTINGS_PATH):
        self.path = path

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            return Settings.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            raise RepPatchError(f"設定ファイルを読み込めません: {exc}") from exc

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
        raw.update(updates)
        updated = Settings.from_dict(raw)
        if updated.mode not in {"home", "company"}:
            raise RepPatchError("mode は home または company で指定してください")
        if updated.listen_host != "127.0.0.1":
            raise RepPatchError("セキュリティのため待受ホストは 127.0.0.1 のみ使用できます")
        if not 1 <= updated.listen_port <= 65535:
            raise RepPatchError("ポート番号が不正です")
        if not 1 <= updated.chunk_size_mib <= 40:
            raise RepPatchError("分割サイズは1〜40 MiBで指定してください")
        return self.save(updated)
