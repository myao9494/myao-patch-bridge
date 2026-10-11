"""
ローカルのエディタでリポジトリを開く処理を行うモジュール

仕様:
- open_in_vscode: VS Code（`code` / Windowsは `code.cmd`）で指定ディレクトリを開く（会社側カード用）
- open_in_antigravity: Antigravity（Antigravity IDE）で指定ディレクトリを開く（自宅側カード用）
- find_antigravity_command: PATH、既知のインストール先、macOSの `open -a` の順に起動コマンドを探索
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .errors import RepPatchError


def _existing_directory(value: str) -> Path:
    target = Path(value).expanduser().resolve()
    if not target.is_dir():
        raise RepPatchError(f"指定されたディレクトリが存在しません: {target}")
    return target


def find_antigravity_command() -> list[str] | None:
    """Antigravity IDEまたはAntigravityの起動コマンド・パスを探索する"""
    # 1. PATHからの探索
    for name in ("antigravity-ide", "antigravity-ide.cmd", "antigravity", "antigravity.cmd"):
        found = shutil.which(name)
        if found:
            return [found]

    # 2. macOS / Linux 既知パスの探索
    candidates = [
        Path.home() / ".antigravity-ide" / "antigravity-ide" / "bin" / "antigravity-ide",
        Path("/Applications/Antigravity IDE.app/Contents/Resources/app/bin/antigravity-ide"),
        Path.home() / "Applications" / "Antigravity IDE.app" / "Contents" / "Resources" / "app" / "bin" / "antigravity-ide",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]

    # 3. macOS open コマンドによるアプリ起動
    if shutil.which("open"):
        if Path("/Applications/Antigravity IDE.app").is_dir():
            return ["open", "-a", "Antigravity IDE"]
        if Path("/Applications/Antigravity.app").is_dir():
            return ["open", "-a", "Antigravity"]

    return None


def open_in_vscode(path: str) -> dict[str, str]:
    target = _existing_directory(path)
    command = shutil.which("code") or shutil.which("code.cmd")
    if not command:
        raise RepPatchError(
            "VS Code (codeコマンド) が見つかりません。PATHにVS Codeが登録されているか確認してください。"
        )
    subprocess.Popen([command, str(target)])
    return {"status": "ok", "path": str(target)}


def open_in_antigravity(path: str) -> dict[str, str]:
    target = _existing_directory(path)
    command = find_antigravity_command()
    if not command:
        raise RepPatchError(
            "Antigravity (antigravity-ideコマンド) が見つかりません。PATHにAntigravityが登録されているか確認してください。"
        )
    subprocess.Popen([*command, str(target)])
    return {"status": "ok", "path": str(target)}
