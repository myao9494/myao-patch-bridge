"""
GitHub Releases 連携モジュール（rep_patch/github.py）の単体テスト

仕様:
- parse_github_repo: HTTPS/SSH等の様々なGitリモートURLからowner/repoを抽出できること
- create_github_release: Releases作成APIを呼び出し、リリース情報を取得できること
- upload_release_asset: ReleasesアセットアップロードAPIを呼び出し、ダウンロードURLを取得できること
- API呼び出し失敗時に適切な例外（GitHubApiError）が発生すること
"""
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

import pytest

from rep_patch.github import (
    create_github_release,
    GitHubApiError,
    parse_github_repo,
    upload_release_asset,
)


def test_parse_github_repo():
    assert parse_github_repo("https://github.com/myao9494/myao_app_patch.git") == "myao9494/myao_app_patch"
    assert parse_github_repo("https://github.com/myao9494/myao_app_patch") == "myao9494/myao_app_patch"
    assert parse_github_repo("git@github.com:myao9494/myao_app_patch.git") == "myao9494/myao_app_patch"
    assert parse_github_repo("ssh://git@github.com/myao9494/myao_app_patch.git") == "myao9494/myao_app_patch"
    assert parse_github_repo("https://gitlab.com/other/repo.git") is None
    assert parse_github_repo("") is None


def test_create_github_release_success():
    fake_response_data = {
        "id": 12345,
        "html_url": "https://github.com/myao9494/myao_app_patch/releases/tag/v1.0.0",
        "upload_url": "https://uploads.github.com/repos/myao9494/myao_app_patch/releases/12345/assets{?name,label}",
    }
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(fake_response_data).encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        result = create_github_release(
            token="fake-token",
            repo_slug="myao9494/myao_app_patch",
            tag_name="v1.0.0",
            name="Patch v1.0.0",
            body="Release notes",
        )

        assert result["id"] == 12345
        assert result["html_url"] == "https://github.com/myao9494/myao_app_patch/releases/tag/v1.0.0"
        assert result["upload_url"] == fake_response_data["upload_url"]

        req = mock_urlopen.call_args[0][0]
        assert req.get_full_url() == "https://api.github.com/repos/myao9494/myao_app_patch/releases"
        assert req.headers["Authorization"] == "Bearer fake-token"


def test_create_github_release_error():
    mock_err = urllib.error.HTTPError(
        url="https://api.github.com/repos/myao9494/myao_app_patch/releases",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=io.BytesIO(b'{"message": "Bad credentials"}'),
    )

    with patch("urllib.request.urlopen", side_effect=mock_err):
        with pytest.raises(GitHubApiError) as exc_info:
            create_github_release(
                token="invalid-token",
                repo_slug="myao9494/myao_app_patch",
                tag_name="v1.0.0",
                name="Patch v1.0.0",
            )
        assert "Bad credentials" in str(exc_info.value) or "401" in str(exc_info.value)


def test_upload_release_asset_success(tmp_path: Path):
    asset_file = tmp_path / "myao_app_patch_20260909.zip"
    asset_file.write_bytes(b"PK\x03\x04dummy zip content")

    fake_asset_response = {
        "id": 67890,
        "name": "myao_app_patch_20260909.zip",
        "browser_download_url": "https://github.com/myao9494/myao_app_patch/releases/download/v1.0.0/myao_app_patch_20260909.zip",
    }
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(fake_asset_response).encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    upload_url_template = "https://uploads.github.com/repos/myao9494/myao_app_patch/releases/12345/assets{?name,label}"

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        result = upload_release_asset(
            token="fake-token",
            upload_url=upload_url_template,
            file_path=asset_file,
        )

        assert result["id"] == 67890
        assert result["browser_download_url"] == fake_asset_response["browser_download_url"]

        req = mock_urlopen.call_args[0][0]
        assert "name=myao_app_patch_20260909.zip" in req.get_full_url()
        assert req.headers["Authorization"] == "Bearer fake-token"
        assert req.headers["Content-type"] == "application/zip"
