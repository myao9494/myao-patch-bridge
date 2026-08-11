# Myao Rep Patch

インターネットへ接続できない会社環境へ、複数のGitリポジトリとObsidian設定の変更を安全に持ち込むためのPWAです。

- 自宅モード: 登録した固定ブランチの差分を分割パッチ化し、専用GitHubリポジトリへpush
- 会社モード: DownloadsのGitHub ZIPを直接検証し、独立したGit履歴へ機械的に適用
- 会社側の変更は未コミットで保持し、次回パッチ前にユーザー操作で自動コミット
- HMAC-SHA-256、分割SHA-256、ZIP検査、適用前バックアップ、アプリ単位の復元に対応

## ドキュメント

- [全体設計](docs/architecture.md)
- [自宅側の導入・運用](docs/home-operation.md)
- [会社側の導入・運用](docs/company-operation.md)
- [セキュリティ設計](docs/security.md)
- [パッチパッケージ仕様](docs/package-format.md)
- [トラブルシューティング](docs/troubleshooting.md)
- [開発者向け情報](docs/development.md)

## 開発起動

```bash
uv sync --extra dev
cd frontend && npm install && npm run build && cd ..
uv run rep-patch
```

既定では `http://127.0.0.1:17345` を開きます。

## 動作条件

- 自宅: macOS、Python 3.10以上、Node.js、Git
- 会社: 64bit Windows、Python 3.10以上、Git、Microsoft Edge
- パッチ専用リポジトリ: 通常の公開GitHubリポジトリ（Git LFS不要）

会社側ではNode.js、Git clone、インターネット接続を必要としません。初回または更新時の `pip install` だけインターネットを使用します。
