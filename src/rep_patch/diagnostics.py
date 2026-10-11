"""
環境診断（Pythonバージョン、Gitコマンド、ポート、ディレクトリ権限、Gitでのファイル配置・削除・コミット）を行うモジュール

仕様:
- run_diagnostics: 各診断項目を実行し、項目ごとの成否と詳細を返却
  - 診断は一時ディレクトリの一時リポジトリだけを使用し、本物のアプリは変更しない
  - Git同期互換性: 会社側のパッチ適用と同じ手順（ファイル上書き配置・削除・`git add -A`・コミット）を
    テキスト / CRLF / バイナリ / 日本語パス / 削除 について実行し、内容が保たれ作業ツリーがクリーンになることを確認
"""
from __future__ import annotations

import platform
import socket
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Settings, resolve_bind_host
from .git import run_git

_IDENTITY = ["-c", "user.name=Patch Bridge Diagnostic", "-c", "user.email=diagnostic@localhost"]


def run_diagnostics(settings: Settings) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, operation: Callable[[], str]) -> None:
        try:
            detail = operation()
            checks.append({"name": name, "status": "ok", "detail": detail})
        except Exception as exc:  # noqa: BLE001 - diagnostics must report every failed check
            checks.append({"name": name, "status": "failed", "detail": str(exc)})

    check("Python", _python_version)
    check("64-bit環境", _require_64bit)
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


def _python_version() -> str:
    if sys.version_info < (3, 10):
        raise RuntimeError(f"Python 3.10以上が必要です（現在: {platform.python_version()}）")
    return platform.python_version()


def _require_64bit() -> str:
    if sys.maxsize <= 2**32:
        raise RuntimeError(f"64bit版のPythonが必要です（現在: {platform.machine()}）")
    return platform.machine()


def _write_test(directory: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="rep-patch-check-", dir=directory, delete=True) as stream:
        stream.write(b"ok")
        stream.flush()
    return str(directory)


def _port_test(listen_host: str, port: int) -> str:
    # The running app already owns the configured port. Binding port 0 verifies the address is usable.
    bind_host = resolve_bind_host(listen_host)
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as server:
        server.bind((bind_host, 0))
        selected = server.getsockname()[1]
    return f"{listen_host}:{port} (bind test: {bind_host}:{selected})"


def _git_sync_roundtrip() -> str:
    crlf_text = b"line 1\r\nline 2 changed\r\n"
    binary = bytes(reversed(range(256))) * 8
    with tempfile.TemporaryDirectory(prefix="rep-patch-diagnostic-") as value:
        target = Path(value) / "target"
        target.mkdir()
        run_git(target, ["init"])
        (target / "text.txt").write_bytes(b"line 1\nline 2\n")
        (target / "binary.bin").write_bytes(bytes(range(256)) * 8)
        (target / "old_file.txt").write_text("delete target\n", encoding="utf-8")
        run_git(target, ["add", "-A"])
        run_git(target, [*_IDENTITY, "commit", "-m", "initial base"])

        # ファイルの上書き・新規配置および削除
        (target / "text.txt").write_bytes(crlf_text)
        (target / "binary.bin").write_bytes(binary)
        (target / "日本語.txt").write_text("確認\n", encoding="utf-8")
        (target / "old_file.txt").unlink()

        run_git(target, ["add", "-A"])
        run_git(target, [*_IDENTITY, "commit", "-m", "apply"])
        if run_git(target, ["status", "--porcelain=v1"]).text:
            raise RuntimeError("コミット後も未コミットの変更が残っています")
        if (target / "text.txt").read_bytes() != crlf_text or (target / "binary.bin").read_bytes() != binary:
            raise RuntimeError("適用後ファイルが一致しません")
        if (target / "日本語.txt").read_text(encoding="utf-8") != "確認\n":
            raise RuntimeError("適用後ファイルが一致しません")
        if (target / "old_file.txt").exists():
            raise RuntimeError("削除対象ファイルが残っています")
        tracked = run_git(target, ["ls-files", "-z"]).stdout.split(b"\0")
        if "日本語.txt".encode("utf-8") not in tracked or b"old_file.txt" in tracked:
            raise RuntimeError("Gitの追跡状態が適用結果と一致しません")
    return "text / CRLF / binary / Japanese path / deletion / commit: OK"
