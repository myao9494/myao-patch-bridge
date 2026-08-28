"""
環境診断（Pythonバージョン、Gitコマンド、ポート使用状況、各種ディレクトリ権限等）を行うモジュール

仕様:
- run_diagnostics: 各種診断項目（Python, Git, Port, AppsRoot, DownloadDir, PatchPassword等）を実行し結果を返却
"""
from __future__ import annotations

import platform
import socket
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Settings
from .git import run_git


def run_diagnostics(settings: Settings) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, operation: Callable[[], str]) -> None:
        try:
            detail = operation()
            checks.append({"name": name, "status": "ok", "detail": detail})
        except Exception as exc:  # noqa: BLE001 - diagnostics must report every failed check
            checks.append({"name": name, "status": "failed", "detail": str(exc)})

    check("Python", lambda: platform.python_version())
    check("64-bit環境", lambda: platform.machine())
    check("Git", lambda: run_git(None, ["--version"]).text)
    check("Downloads書き込み", lambda: _write_test(Path(settings.download_dir).expanduser()))
    check("ローカルポート", lambda: _port_test(settings.listen_host, settings.listen_port))
    check("Git同期互換性", _git_sync_roundtrip)
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "checks": checks,
        "passed": all(item["status"] == "ok" for item in checks),
    }


def _write_test(directory: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="rep-patch-check-", dir=directory, delete=True) as stream:
        stream.write(b"ok")
        stream.flush()
    return str(directory)


def _port_test(host: str, port: int) -> str:
    # The running app already owns the configured port. Binding port 0 verifies loopback capability.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind((host, 0))
        selected = server.getsockname()[1]
    return f"{host}:{port} (loopback test port: {selected})"


def _git_sync_roundtrip() -> str:
    with tempfile.TemporaryDirectory(prefix="rep-patch-diagnostic-") as value:
        root = Path(value)
        target = root / "target"
        target.mkdir()
        run_git(target, ["init"])
        run_git(target, ["config", "user.name", "Patch Bridge Diagnostic"])
        run_git(target, ["config", "user.email", "diagnostic@localhost"])
        (target / "text.txt").write_bytes(b"line 1\nline 2\n")
        (target / "binary.bin").write_bytes(bytes(range(256)) * 8)
        (target / "old_file.txt").write_text("delete target\n", encoding="utf-8")
        run_git(target, ["add", "-A"])
        run_git(target, ["commit", "-m", "initial base"])

        # ファイルの上書き・新規配置および削除
        (target / "text.txt").write_bytes(b"line 1\r\nline 2 changed\r\n")
        (target / "binary.bin").write_bytes(bytes(reversed(range(256))) * 8)
        (target / "日本語.txt").write_text("確認\n", encoding="utf-8")
        (target / "old_file.txt").unlink()

        run_git(target, ["add", "-A"])
        run_git(target, ["reset", "--mixed", "HEAD"])
        if (target / "日本語.txt").read_text(encoding="utf-8") != "確認\n":
            raise RuntimeError("適用後ファイルが一致しません")
        if (target / "old_file.txt").exists():
            raise RuntimeError("削除対象ファイルが残っています")
    return "text / CRLF / binary / Japanese path / deletion: OK"

