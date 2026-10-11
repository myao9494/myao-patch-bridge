"""
ファイルの安全な書き込みユーティリティ

仕様:
- atomic_write_text: 同一ディレクトリの一意な一時ファイルへ書き出してから置換する原子的な書き込み
  - 一時ファイル名を呼び出しごとに一意にし、並列書き込みでも互いの一時ファイルを壊さない
  - 改行は常にLFで書き出す（OSによる改行変換を行わない）
  - Windowsでウイルス対策ソフト等が一時的にファイルを掴んでいる場合に備えて置換を数回再試行する
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_SECONDS = 0.05


def atomic_write_text(path: Path, text: str, *, private: bool = False) -> None:
    """textをUTF-8でpathへ原子的に書き込む。privateなら所有者のみ読み書き可能にする。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        try:
            os.chmod(temporary, 0o600 if private else 0o644)
        except OSError:
            pass
        _replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _replace(source: Path, destination: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_RETRY_SECONDS)
