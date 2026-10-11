"""
Myao Patch Bridge CLI エントリポイント

仕様:
- 設定ファイル（settings.local.json）の読み込みおよびコマンドライン引数によるオーバーライド
  （オーバーライドはこの起動の間だけ有効で、設定ファイルには書き込まない）
- 待受ホスト（--host, --allow-vpn）、待受ポート（--port）の指定
- ブラウザ自動起動（--no-browser で無効化可能）
- uvicorn によるWebサーバー起動
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .api import create_app
from .config import SettingsStore, allows_remote_clients, resolve_bind_host


def detect_tailscale_dns_name() -> str | None:
    """TailscaleのMagicDNSホスト名を検出する"""
    candidates = [
        "tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ]
    for cmd in candidates:
        exe = shutil.which(cmd) if cmd == "tailscale" else (cmd if Path(cmd).is_file() else None)
        if not exe:
            continue
        try:
            res = subprocess.run(
                [exe, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if res.returncode == 0 and res.stdout:
                data = json.loads(res.stdout)
                dns_name = data.get("Self", {}).get("DNSName", "").rstrip(".")
                if dns_name:
                    return dns_name
        except Exception:
            pass
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Myao Patch Bridge")
    parser.add_argument("--host", type=str, default=None, help="待受ホスト（例: 0.0.0.0 でVPN/LAN待受）")
    parser.add_argument("--port", type=int, default=None, help="待受ポート番号（既定: 17345）")
    parser.add_argument(
        "--allow-vpn",
        "--vpn",
        action="store_true",
        help="VPN/プライベートネットワークからのアクセスを許可（0.0.0.0 でリッスン）",
    )
    parser.add_argument("--no-browser", action="store_true", help="Edge/ブラウザを自動で開かない")
    args = parser.parse_args()

    overrides: dict[str, object] = {}
    if args.allow_vpn:
        overrides["listen_host"] = "0.0.0.0"
    elif args.host:
        overrides["listen_host"] = args.host
    if args.port:
        overrides["listen_port"] = args.port

    store = SettingsStore(overrides=overrides)
    settings = store.load_effective()

    # ホスト名が指定された場合は 0.0.0.0 でバインドし、Tailscale等のドメイン宛て通信を確実に受信
    bind_host = resolve_bind_host(settings.listen_host)
    browser_host = "127.0.0.1" if bind_host in {"0.0.0.0", "::"} else bind_host
    browser_url = f"http://{browser_host}:{settings.listen_port}"

    print("Myao Patch Bridge を起動しています...")
    print(f"ローカルアクセスURL: {browser_url}")
    if allows_remote_clients(settings.listen_host):
        print(f"VPN/ネットワーク待受ホスト: {settings.listen_host}:{settings.listen_port}")
        tailscale_dns = detect_tailscale_dns_name()
        if tailscale_dns:
            print(f"TailscaleアクセスURL: http://{tailscale_dns}:{settings.listen_port}/")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(browser_url)).start()

    uvicorn.run(
        create_app(store),
        host=bind_host,
        port=settings.listen_port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()

