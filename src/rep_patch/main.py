"""
Myao Patch Bridge CLI エントリポイント

仕様:
- 設定ファイル（settings.local.json）の読み込み
- ブラウザ自動起動（--no-browser で無効化可能）
- uvicorn によるローカルWebサーバー起動
"""
from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from .api import create_app
from .config import SettingsStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Myao Patch Bridge")
    parser.add_argument("--no-browser", action="store_true", help="Edge/ブラウザを自動で開かない")
    args = parser.parse_args()
    settings = SettingsStore().load()
    url = f"http://{settings.listen_host}:{settings.listen_port}"
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        create_app(),
        host=settings.listen_host,
        port=settings.listen_port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
