# Myao Patch Bridge 仕様書

## 概要
インターネット非接続環境（会社等）と自宅環境の間で、Gitリポジトリの差分パッチを安全に同期・適用するためのローカルブリッジアプリケーション。

---

## アーキテクチャ

- **バックエンド**: Python 3.12+ / FastAPI / Uvicorn (127.0.0.1 のみ待受)
- **フロントエンド**: React 19 / TypeScript / Vite / Vanilla CSS (PWA対応)
- **セキュリティ**:
  - ローカルホスト限定アクセス
  - X-Rep-Patch-Token によるCSRF対策
  - HMAC-SHA256 によるマニフェスト・インデックス署名検証
  - パストラバーサル防止

---

## 動作モード

### 1. 自宅モード (`home`)
- **リポジトリ管理**:
  - **自動検出**: アプリルート（`apps_root`）および Obsidian 設定（`obsidian_repo`）配下の Git リポジトリを一括検出
  - **手動追加**: 任意のパスを指定してリポジトリカードを追加（`POST /api/repositories`）
  - **カード削除**: 管理対象外のリポジトリカードを削除（`DELETE /api/repositories/{repo_id}`）※実リポジトリは保持
  - **対象選択**: トグルスイッチまたは「すべて選択/すべて解除」でパッチ公開対象を選択
  - **設定管理**: 固定ブランチ、初期導入地点コミットの設定・保存
- **パッチ公開 (`publish`)**:
  - 選択・有効化されたリポジトリの未公開コミット差分をバイナリパッチ（20MiB分割可）として生成
  - 署名付き `manifest.json` および `package-index.json` をパッチ専用リポジトリへコミット＆push

### 2. 会社モード (`company`)
- **パッチ受信・検証**:
  - ダウンロードフォルダ内の `myao_app_patch*.zip` を検索
  - 署名・SHA-256・リポジトリ対応状況を検証（`inspect`）
- **パッチ適用・コミット**:
  - 会社側リポジトリへ未コミット状態でパッチ適用（ロールバック用バックアップ作成）
  - 動作確認後に「確認済みをコミット」（`commit-pending`）
  - テスト不合格時は「修正パッチを重ねる」（`correction`）
- **環境診断**:
  - Python、Git、ポート、ディレクトリ権限等を自動診断

---

## API仕様一覧

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

## データ保存場所
- 設定ファイル: `data/settings.local.json`（Git管理外）
