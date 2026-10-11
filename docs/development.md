# 開発者向け情報

## 構成

```text
src/rep_patch/
  api.py          FastAPI。アクセス制御、全エンドポイント、PWA配信（業務ロジックは持たない）
  home.py         自宅モード: リポジトリ検出・登録・一覧、パッチ生成・公開、履歴リセット
  company.py      会社モード: ZIP検証、バックアップ、適用、コミット、state.json
  verifier.py     公開前の会社環境シミュレーション検証
  packages.py     パッケージ形式、分割、ZIP読取（PatchArchive）、今回分ZIP生成
  security.py     HMAC署名、パス検証、接続元IP・Hostヘッダー判定
  git.py          shellを使わないGitコマンドラッパー（gitの呼び出しは必ずここを通す）
  config.py       設定の定義と永続化（Git対象外の data/settings.local.json）
  github.py       GitHub Releases API
  editors.py      VS Code / Antigravity の起動
  diagnostics.py  会社環境の自己診断
  fileio.py       原子的なファイル書き込み
  main.py         CLIエントリポイント（patch-bridge / python -m rep_patch）
  errors.py       利用者向けエラーの例外クラス
frontend/
  src/App.tsx           画面全体の状態とAPI呼び出し
  src/components/       自宅・会社のダッシュボード、設定ドロワー、モーダル
  src/api.ts            APIクライアント（セッショントークン付与）
  src/types.ts          APIとやり取りする型
  public/               PWA manifest、Service Worker、アイコン
  dist/                 会社配布用ビルド成果物（Git管理対象）
tests/                  Git統合テスト、API、セキュリティ
docs/                   仕様書・設計・運用手順
```

依存の向きは `api → home / company → verifier / packages → security / git / config` です。`company.py` は `home.py` を読み込みません。

## 開発環境

```bash
uv sync --extra dev
cd frontend
npm install
npm run dev
```

別ターミナルでAPIを起動します。

```bash
uv run patch-bridge --no-browser
```

Vite開発サーバーは `/api` を `127.0.0.1:17345` へプロキシします。

`patch-bridge` は `data/settings.local.json` の実設定で動きます。画面から「パッチを作成・公開」を押すと本物のパッチ専用リポジトリへpushされるので、動作確認には一時ディレクトリのリポジトリと設定を使ってください（`rep_patch.api.create_app(SettingsStore(一時パス))` を uvicorn で起動する）。

## テスト

```bash
uv run pytest                                   # 全テスト
uv run pytest tests/test_company_flow.py        # 1ファイル
uv run pytest tests/test_home_flow.py::test_publish_then_apply_end_to_end
uv run pytest -k rollback                       # 名前で絞る
uv run pytest --cov=rep_patch --cov-report=term-missing
npm run lint --prefix frontend                  # フロントエンドの型チェック
```

### 方針

- 実際のGitリポジトリを一時ディレクトリに作って動かす統合テストを中心にします。ソースと会社側は別々に `git init`・commitし、異なるコミットIDを持つ状態で、テキスト、バイナリ、日本語パス、リネーム、削除を適用します。
- `tests/conftest.py` が全テストで `HOME` とGitのグローバル設定を一時領域へ差し替えます。開発者のGit設定（コミット署名、`autocrlf`、既定ブランチ名など）に結果が左右されず、`~/Downloads` など実ユーザーの領域にも書き込みません。`user.name` はあえて未設定にしてあり、会社PCでGitユーザー情報が無くてもコミットできることを確認しています。
- 共通の操作は `tests/helpers.py` にあります（`init_repo`、`commit_all`、`make_patch_repo`、`make_client`、`auth_headers`）。APIテストは `make_client(store, "192.168.1.50")` のように接続元IPを指定できます。
- 不具合を直すときは、先にその不具合を再現するテストを書きます。

### テストファイル

| ファイル | 対象 |
| :--- | :--- |
| `test_home_flow.py` | 公開、履歴リセット、公開失敗時の後始末、自宅→会社の通し |
| `test_company_flow.py` | 適用、事前コミット、内容一致パッチ、失敗時復元、連番、旧形式の状態 |
| `test_packages.py` | ZIPの改ざん・欠落・安全でないパスの拒否、改行コード復元 |
| `test_release_bundle.py` | 今回分ZIPの生成 |
| `test_verifier.py` | 会社環境シミュレーション検証 |
| `test_repository_management.py` | リポジトリの追加・更新・削除・自動検出・一覧 |
| `test_settings_store.py` | 設定の保存（起動引数を保存しない、並列更新、バリデーション） |
| `test_security.py` | 署名、パス検証、接続元IP、Hostヘッダー |
| `test_git.py` | Gitラッパー |
| `test_github.py` | GitHub Releases API（HTTPはモック） |
| `test_api.py` | エンドポイント、アクセス制御、エディタ起動、診断 |

フロントエンドには自動テストがありません。画面の変更は、上記の一時環境のサーバーで実際に操作して確認します。

Windows固有の経路（`start_patch_app.bat`、`install_company.bat`、`code.cmd`）は macOS 上のテストでは実行されません。変更した場合は会社PCでの確認が必要です。

## 配布ビルド

```bash
cd frontend
npm ci
npm run build
cd ..
uv pip compile requirements.in --generate-hashes -o requirements.lock
uv run pytest
```

`frontend/dist` は会社側にNode.jsを要求しないため、Git管理対象に含めます。`frontend/src` を変更したら必ずビルドし、`dist` の差分も一緒にコミットしてください（ハッシュ付きのファイル名が変わります）。

`.bat` ファイルはASCII文字だけで記述します。日本語を含めると、会社PCの cmd.exe が日本語コードページで行を誤って解釈することがあります。

## バージョン更新

Python依存を変えた場合は `requirements.in`、`pyproject.toml`、`requirements.lock` を同時更新します。React依存を変えた場合は `package.json` と `package-lock.json` を同時更新し、`npm audit` とPWAビルドを実行します。

## パッケージ形式を変えるとき

パッケージ形式は自宅側と会社側の間の契約で、会社側のアプリは手作業で別のタイミングに更新されます。形式v1のまま項目を追加する場合は、旧バージョンの会社側アプリが無視できる形（任意項目の追加）に限ります。既存項目の意味や必須性を変える場合は `SCHEMA_VERSION` を上げ、移行手順を [パッチパッケージ仕様](package-format.md) に書いてください。
