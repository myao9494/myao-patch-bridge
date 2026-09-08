"""
GitHub Releases API 連携モジュール

仕様:
- parse_github_repo: GitリモートURLからowner/repoをパース・抽出
- get_repo_slug: パッチ専用リポジトリのoriginリモートまたは設定からowner/repoを取得
- create_github_release: GitHub Releases APIを呼び出して新規リリースを作成
- upload_release_asset: 作成したリリースへ配布用パッチZIPをアセットとしてアップロード
- GitHubApiError: GitHub API通信エラー時のカスタム例外
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from .errors import RepPatchError
from .git import run_git


class GitHubApiError(RepPatchError):
    """GitHub API関連のエラー"""
    pass


def parse_github_repo(remote_url: str) -> str | None:
    """GitのリモートURL（HTTPSまたはSSH）から 'owner/repo' 文字列を抽出する"""
    if not remote_url:
        return None
    url = remote_url.strip()
    # https://github.com/owner/repo(.git)
    match_https = re.search(r"github\.com[/:]([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+?)(?:\.git)?$", url)
    if match_https:
        owner = match_https.group(1)
        repo = match_https.group(2)
        return f"{owner}/{repo}"
    return None


def get_repo_slug(patch_root: Path, explicit_repo: str = "") -> str:
    """明示的な指定またはGitリモートから owner/repo を解決する"""
    if explicit_repo.strip():
        return explicit_repo.strip()
    try:
        remote_res = run_git(patch_root, ["config", "--get", "remote.origin.url"], check=False)
        if remote_res.returncode == 0 and remote_res.text:
            parsed = parse_github_repo(remote_res.text)
            if parsed:
                return parsed
    except Exception:
        pass
    raise GitHubApiError("GitHubリポジトリ（owner/repo）を特定できませんでした")


def create_github_release(
    token: str,
    repo_slug: str,
    tag_name: str,
    name: str,
    body: str = "",
) -> dict[str, Any]:
    """GitHub Releases APIを呼び出して新規リリースを作成する"""
    if not token.strip():
        raise GitHubApiError("GitHubトークンが指定されていません")
    url = f"https://api.github.com/repos/{repo_slug}/releases"
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "myao-patch-bridge",
        "Content-Type": "application/json",
    }
    payload = {
        "tag_name": tag_name,
        "name": name,
        "body": body,
        "draft": False,
        "prerelease": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("message", err_body)
        except Exception:
            msg = err_body
        raise GitHubApiError(f"GitHub Release作成エラー ({exc.code}): {msg}") from exc
    except urllib.error.URLError as exc:
        raise GitHubApiError(f"GitHub通信エラー: {exc.reason}") from exc


def upload_release_asset(
    token: str,
    upload_url: str,
    file_path: Path,
    content_type: str = "application/zip",
) -> dict[str, Any]:
    """Releaseにファイルをアセットとしてアップロードする"""
    if not token.strip():
        raise GitHubApiError("GitHubトークンが指定されていません")
    if not file_path.is_file():
        raise GitHubApiError(f"アセットファイルが見つかりません: {file_path}")

    # upload_url は "https://uploads.github.com/.../assets{?name,label}" のような形式
    base_url = re.sub(r"\{.*?\}$", "", upload_url)
    query = urllib.parse.urlencode({"name": file_path.name})
    target_url = f"{base_url}?{query}"

    file_bytes = file_path.read_bytes()
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "myao-patch-bridge",
        "Content-Type": content_type,
        "Content-Length": str(len(file_bytes)),
    }
    req = urllib.request.Request(target_url, data=file_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("message", err_body)
        except Exception:
            msg = err_body
        raise GitHubApiError(f"GitHub Assetアップロードエラー ({exc.code}): {msg}") from exc
    except urllib.error.URLError as exc:
        raise GitHubApiError(f"GitHub通信エラー: {exc.reason}") from exc
