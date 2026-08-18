<!--
  Myao Patch Bridge 開発仕様書 (claude.md)
  本リポジトリの目的、全体アーキテクチャ、会社/自宅モードの動作仕様、起動方法をシンプルにまとめます。
-->

# Myao Patch Bridge 仕様書

## 1. 概要
インターネット未接続の会社PCへ、自宅で開発したGitリポジトリ（複数アプリ）およびObsidian設定の差分を、署名・検証付きパッチ（ZIP）として安全に転送・適用するためのオフラインファーストWeb/PWAアプリケーション。

## 2. システム構成
- **バックエンド**: Python 3.10+（FastAPI, Uvicorn, Pydantic）
- **フロントエンド**: React, TypeScript, Vite（自宅側でビルドした `frontend/dist` を静的ホスト）
- **通信**: ローカルループバック（`http://127.0.0.1:17345`）限定、セッショントークン検証
- **セキュリティ**: HMAC-SHA-256署名、各パッチのSHA-256検証、ZIPパストラバーサル防止

## 3. モード仕様

### 自宅モード (Home)
- 登録したリポジトリの固定ブランチ差分を検出し、分割パッチを作成。
- パッチメタデータ・署名を生成し、専用GitHubリポジトリへpush。
- 開発起動: `uv run patch-bridge` / `frontend` で `npm run build`。

### 会社モード (Company)
- Node.js / npm 不要（同梱の `frontend/dist` を使用）。
- `.venv` なしでもシステムのPythonで起動可能（`start_patch_app.bat` または `set PYTHONPATH=src && python -m rep_patch`）。
- `Downloads` フォルダのパッチZIPを検証（パスワード・SHA-256・署名一致確認）。
- 前回の未コミット変更を自動検証・コミット後、新パッチを適用し未ステージ状態で展開。

## 4. 起動スクリプト仕様
- **`start_patch_app.bat`**:
  - Windowsコマンドプロンプトのパーサー互換性を担保（構文エラーや文字化けを防止）。
  - `.venv\Scripts\python.exe` があれば優先使用、なければシステム `python` を自動使用。
  - ポート `17345` の既存プロセスを停止して起動。
- **`install_company.bat`**:
  - `.venv` 作成を試行し、不可の場合はシステム環境へ `requirements.lock`（ハッシュ検証付き）を直接インストール。
