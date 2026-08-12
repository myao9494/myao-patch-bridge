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
    check("Gitパッチ互換性", _git_patch_roundtrip)
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


def _git_patch_roundtrip() -> str:
    with tempfile.TemporaryDirectory(prefix="rep-patch-diagnostic-") as value:
        root = Path(value)
        source = root / "source"
        target = root / "target"
        source.mkdir()
        run_git(source, ["init"])
        run_git(source, ["config", "user.name", "Patch Bridge Diagnostic"])
        run_git(source, ["config", "user.email", "diagnostic@localhost"])
        (source / "text.txt").write_bytes(b"line 1\nline 2\n")
        (source / "binary.bin").write_bytes(bytes(range(256)) * 8)
        run_git(source, ["add", "-A"])
        run_git(source, ["commit", "-m", "base"])
        base = run_git(source, ["rev-parse", "HEAD"]).text
        (source / "text.txt").write_bytes(b"line 1\r\nline 2 changed\r\n")
        (source / "binary.bin").write_bytes(bytes(reversed(range(256))) * 8)
        (source / "日本語.txt").write_text("確認\n", encoding="utf-8")
        run_git(source, ["add", "-A"])
        run_git(source, ["commit", "-m", "change"])
        target_commit = run_git(source, ["rev-parse", "HEAD"]).text
        patch = run_git(source, ["diff", "--binary", "--full-index", base, target_commit]).stdout

        target.mkdir()
        run_git(target, ["init"])
        run_git(target, ["config", "user.name", "Patch Bridge Diagnostic"])
        run_git(target, ["config", "user.email", "diagnostic@localhost"])
        (target / "text.txt").write_bytes(b"line 1\nline 2\n")
        (target / "binary.bin").write_bytes(bytes(range(256)) * 8)
        run_git(target, ["add", "-A"])
        run_git(target, ["commit", "-m", "independent base"])
        patch_path = root / "diagnostic.patch"
        patch_path.write_bytes(patch)
        run_git(target, ["apply", "--check", "--index", "--binary", str(patch_path)])
        run_git(target, ["apply", "--index", "--binary", str(patch_path)])
        run_git(target, ["reset", "--mixed", "HEAD"])
        if (target / "日本語.txt").read_text(encoding="utf-8") != "確認\n":
            raise RuntimeError("適用後ファイルが一致しません")
    return "text / CRLF / binary / Japanese path: OK"
