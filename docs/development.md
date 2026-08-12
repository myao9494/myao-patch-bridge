# 開発者向け情報

## 構成

```text
src/rep_patch/
  api.py          FastAPI、セッション保護、PWA配信
  company.py      ZIP検証、バックアップ、適用、コミット
  config.py       Git対象外ローカル設定
  diagnostics.py  会社環境の自己診断
  git.py          shellを使わないGitコマンドラッパー
  home.py         リポジトリ検出、パッチ生成、push
  packages.py     パッケージ形式、分割、ZIP読取
  security.py     HMAC、SHA-256、パス検証
frontend/
  src/            React UI
  public/         PWA manifest、Service Worker、アイコン
  dist/           会社配布用ビルド成果物
tests/            Git統合テスト、API、セキュリティ
```

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

## 配布ビルド

```bash
cd frontend
npm ci
npm run build
cd ..
uv pip compile requirements.in --generate-hashes -o requirements.lock
uv run pytest
```

`frontend/dist` は会社側にNode.jsを要求しないため、Git管理対象に含めます。

## テスト方針

最重要の統合テストでは、ソースと会社側で別々に `git init`・commitし、異なるコミットIDを作ります。その状態でテキスト、バイナリ、日本語ファイルを適用し、次のパッケージで前回分が自動コミットされることを確認します。

```bash
uv run pytest
npm run build --prefix frontend
```

## バージョン更新

Python依存を変えた場合は `requirements.in`、`pyproject.toml`、`requirements.lock` を同時更新します。React依存を変えた場合は `package.json` と `package-lock.json` を同時更新し、`npm audit` とPWAビルドを実行します。
