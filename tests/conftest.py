from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode(errors="replace"))
    return completed.stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    return path


@pytest.fixture
def git_helpers():
    return git, init_repo
