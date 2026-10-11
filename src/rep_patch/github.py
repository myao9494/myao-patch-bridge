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

# 応答が返らないまま公開処理が終わらなくなるのを防ぐための通信タイムアウト（秒）
API_TIMEOUT_SECONDS = 60
UPLOAD_TIMEOUT_SECONDS = 30 * 60


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


def _post_json(
    url: str,
    token: str,
    data: bytes,
    content_type: str,
    *,
    timeout: float,
    action: str,
) -> dict[str, Any]:
    """GitHub APIへPOSTし、JSON応答を返す。失敗時はGitHubのエラーメッセージを添えてGitHubApiErrorにする。"""
    if not token.strip():
        raise GitHubApiError("GitHubトークンが指定されていません")
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "myao-patch-bridge",
        "Content-Type": content_type,
        "Content-Length": str(len(data)),
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(err_body).get("message", err_body)
        except Exception:
            msg = err_body
        raise GitHubApiError(f"GitHub {action}エラー ({exc.code}): {msg}") from exc
    except urllib.error.URLError as exc:
        raise GitHubApiError(f"GitHub通信エラー: {exc.reason}") from exc
    except (OSError, ValueError) as exc:
        # 応答待ちのタイムアウトや、JSONとして読めない応答
        raise GitHubApiError(f"GitHub通信エラー: {exc}") from exc


def create_github_release(
    token: str,
    repo_slug: str,
    tag_name: str,
    name: str,
    body: str = "",
) -> dict[str, Any]:
    """GitHub Releases APIを呼び出して新規リリースを作成する"""
    payload = {
        "tag_name": tag_name,
        "name": name,
        "body": body,
        "draft": False,
        "prerelease": False,
    }
    return _post_json(
        f"https://api.github.com/repos/{repo_slug}/releases",
        token,
        json.dumps(payload).encode("utf-8"),
        "application/json",
        timeout=API_TIMEOUT_SECONDS,
        action="Release作成",
    )


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
    return _post_json(
        f"{base_url}?{query}",
        token,
        file_path.read_bytes(),
        content_type,
        timeout=UPLOAD_TIMEOUT_SECONDS,
        action="Assetアップロード",
    )
