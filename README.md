# Myao Patch Bridge

インターネットへ接続できない会社環境へ、複数のGitリポジトリとObsidian設定の変更を安全に持ち込むためのPWAです。

- 自宅モード: 登録した固定ブランチの差分（変更ファイルの実体と削除一覧）を署名付きパッケージにし、専用GitHubリポジトリへpush
- 会社モード: DownloadsのGitHub ZIPを直接検証し、独立したGit履歴を持つリポジトリへファイルを上書き配置して即時コミット
- 会社側に未コミットの変更があれば、適用前に自動コミットして作業ツリーを常にクリーンに保つ
- HMAC-SHA-256署名、SHA-256検証、ZIP検査、適用前バックアップ、アプリ単位の復元に対応
- 公開前に「会社へ届かないファイル」を検知する会社環境シミュレーション検証
- 公開前リハーサル: 会社へ届くZIPを実際に作り、会社側と同じ検証・適用を自宅で実行してからpush

## ドキュメント

- [仕様書（機能・API一覧）](docs/specification.md)
- [全体設計](docs/architecture.md)
- [自宅側の導入・運用](docs/home-operation.md)
- [会社側の導入・運用](docs/company-operation.md)
- [セキュリティ設計](docs/security.md)
- [パッチパッケージ仕様](docs/package-format.md)
- [会社環境シミュレーション検証（事前クリーン検証）](docs/clean-verification.md)
- [トラブルシューティング](docs/troubleshooting.md)
- [開発者向け情報](docs/development.md)

## 開発起動

```bash
uv sync --extra dev
cd frontend && npm install && npm run build && cd ..
uv run patch-bridge
```

既定では `http://127.0.0.1:17345` を開きます。

## 動作条件

- 自宅: macOS、Python 3.10以上、Node.js、Git
- 会社: 64bit Windows、Python 3.10以上、Git、Microsoft Edge
- パッチ専用リポジトリ: 通常の公開GitHubリポジトリ（Git LFS不要）

会社側ではNode.js、Git clone、インターネット接続を必要としません。初回または更新時の `pip install` だけインターネットを使用します。

## テスト

```bash
uv run pytest
npm run lint --prefix frontend
```

詳細は [開発者向け情報](docs/development.md) を参照してください。
