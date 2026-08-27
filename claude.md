# Myao Patch Bridge 仕様書

## 1. 概要
インターネット未接続の会社PCへ、自宅で開発したGitリポジトリ（複数アプリ）およびObsidian設定の差分を、署名・検証付きパッチ（ZIP）として安全に転送・適用するためのオフラインファーストWeb/PWAアプリケーション。

---

## 2. アーキテクチャ・システム構成

- **バックエンド**: Python 3.10+ / FastAPI / Uvicorn (127.0.0.1:17345 のみ待受)
- **フロントエンド**: React 19 / TypeScript / Vite / Vanilla CSS (自宅でビルドした `frontend/dist` を静的ホスト、PWA対応)
- **通信**: ローカルループバック（`http://127.0.0.1:17345`）限定、セッショントークン検証
- **セキュリティ**:
  - ローカルホスト限定アクセス
  - X-Rep-Patch-Token によるCSRF対策
  - HMAC-SHA-256 によるマニフェスト・インデックス署名検証
  - 各パッチの SHA-256 検証
  - ZIP パストラバーサル防止

---

## 3. 動作モード

### 自宅モード (`home`)
- **リポジトリ管理**:
  - **自動検出**: アプリルート（`apps_root`）および Obsidian 設定（`obsidian_repo`）配下の Git リポジトリを一括検出
  - **手動追加**: 任意のパスを指定してリポジトリカードを追加（`POST /api/repositories`）
  - **カード削除**: 管理対象外のリポジトリカードを削除（`DELETE /api/repositories/{repo_id}`）※実リポジトリは保持
  - **対象選択**: トグルスイッチまたは「すべて選択/すべて解除」でパッチ公開対象を選択
  - **設定管理**: 固定ブランチ、初期導入地点コミットの設定・保存
- **パッチ公開 (`publish`)**:
  - 選択・有効化されたリポジトリの未公開コミット差分をバイナリパッチ（20MiB分割可）として生成
  - **差分ファイル実体同梱**: 新規追加および変更されたファイルの実体を `added_files/` へ自動保存しハッシュ署名
  - **削除ファイル記録**: 削除されたファイルを `deleted_files` としてマニフェストへ記録
  - 署名付き `manifest.json` および `package-index.json` をパッチ専用リポジトリへコミット＆push
- **開発起動**: `uv run patch-bridge` / `frontend` で `npm run build`

### 会社モード (`company`)
- **Node.js / npm 不要**: 同梱の `frontend/dist` を使用。
- **Python 実行**: `.venv` なしでもシステムの Python で起動可能（`start_patch_app.bat` または `set PYTHONPATH=src && python -m rep_patch`）。
- **パッチ受信・検証**:
  - `Downloads` フォルダ内の `myao_app_patch*.zip` を検索・検証（署名・SHA-256・追加ファイル・リポジトリ対応状況の一致確認）。
- **パッチ適用・コミット**:
  - 会社側リポジトリへ未コミット状態でパッチ適用（ロールバック用バックアップ作成）
  - **自動ファイル配置・削除**: 同梱された差分ファイル実体（`added_files/` 内の新規追加・変更ファイル）を自動配置し、削除対象ファイルを確実に消去

  - 動作確認後に「確認済みをコミット」（`commit-pending`）
  - テスト不合格時は「修正パッチを重ねる」（`correction`）
- **環境診断**:
  - Python、Git、ポート、ディレクトリ権限等を自動診断


---

## 4. 起動スクリプト仕様
- **`start_patch_app.bat`**:
  - Windowsコマンドプロンプトのパーサー互換性を担保（構文エラーや文字化けを防止）。
  - `.venv\Scripts\python.exe` があれば優先使用、なければシステム `python` を自動使用。
  - ポート `17345` の既存プロセスを停止して起動。
- **`install_company.bat`**:
  - `.venv` 作成を試行し、不可の場合はシステム環境へ `requirements.lock`（ハッシュ検証付き）を直接インストール。

---

## 5. API仕様一覧

| メソッド | パス | 説明 |
| :--- | :--- | :--- |
| `GET` | `/api/health` | サーバーヘルスチェック・モード取得 |
| `GET` | `/api/session` | セッショントークン発行 |
| `GET` | `/api/settings` | 設定取得 |
| `PUT` | `/api/settings` | 設定更新 |
| `GET` | `/api/repositories` | 登録リポジトリ一覧・状態取得 |
| `POST` | `/api/repositories` | リポジトリ手動追加 |
| `PUT` | `/api/repositories/{repo_id}` | リポジトリ設定更新 |
| `DELETE` | `/api/repositories/{repo_id}` | リポジトリ登録削除 |
| `POST` | `/api/repositories/discover` | アプリルートからのリポジトリ自動再検出 |
| `POST` | `/api/home/publish` | 自宅側パッチ作成・署名・公開 |
| `GET` | `/api/company/downloads` | 会社側パッチZIP一覧取得 |
| `POST` | `/api/company/inspect` | パッチZIPの署名・内容検証 |
| `POST` | `/api/company/apply-all` | 全リポジトリへのパッチ適用 |
| `POST` | `/api/company/retry` | 単一リポジトリへのパッチ再適用 |
| `POST` | `/api/company/commit-pending` | 保留中パッチのGitコミット |
| `GET` | `/api/diagnostics` | 環境診断実行 |

---

## 6. データ保存場所
- 設定ファイル: `data/settings.local.json`（Git管理外）
