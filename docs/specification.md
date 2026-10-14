# Myao Patch Bridge 仕様書

実装（`src/rep_patch/`、`frontend/src/`）を正として、機能と振る舞いをまとめた仕様書です。
機能を追加・変更したときは、この文書と関連する `docs/` を同じ変更の中で更新します。

## 1. 概要

インターネット未接続の会社PCへ、自宅で開発したGitリポジトリ（複数アプリ）およびObsidian設定の差分を、署名・検証付きパッチ（ZIP）として安全に転送・適用するためのオフラインファーストWeb/PWAアプリケーション。

自宅と会社のリポジトリはコミット履歴を共有しない（会社側は独自に `git init` されている）ため、履歴ではなく**ファイルの実体**を運びます。

---

## 2. アーキテクチャ・システム構成

- **バックエンド**: Python 3.10+ / FastAPI / Uvicorn（既定 `127.0.0.1:17345`）
- **フロントエンド**: React 19 / TypeScript / Vite / Vanilla CSS（自宅でビルドした `frontend/dist` を静的ホスト、PWA対応）
- **アクセス制御**（全リクエスト共通）:
  - 接続元IP: ローカルループバックのみ。待受ホストを `127.0.0.1` / `localhost` / `::1` 以外にした場合に限り、VPN/プライベートネットワーク（RFC 1918、Tailscale `100.64.0.0/10`、IPv6 ULA、リンクローカル）も許可。パブリックIPは 403
  - Hostヘッダー: このアプリを指す名前だけを許可（IPアドレス、`localhost`、ドットを含まないマシン名、待受ホストに設定した名前、このPC自身のホスト名、VPN許可時の `*.ts.net`）。それ以外は 403（DNSリバインディング対策）
  - 状態を変更するAPI: 起動ごとに発行するセッショントークン（`X-Rep-Patch-Token`）が必要
- **排他制御**: 設定やリポジトリを書き換えるAPIは1つずつ順番に実行する（同時に届いても直列化）
- **パッケージの正当性**:
  - HMAC-SHA-256 によるマニフェスト・インデックス署名検証（鍵はパスワードからPBKDF2で導出）
  - 分割パッチ・同梱ファイルの SHA-256 検証
  - ZIP を一括展開しない。パストラバーサル、絶対パス、`.git` 配下を指すパス、重複パスを拒否
- **文字コード**: 設定ファイル・マニフェスト・インデックスは UTF-8 BOM（`﻿`）付きでも読み込める

---

## 3. 動作モード

### 自宅モード (`home`)

- **リポジトリ管理**:
  - **自動検出**: アプリルート（`apps_root`）直下のGitリポジトリと Obsidian 設定（`obsidian_repo`）を登録（`POST /api/repositories/discover`）。Patch App 自身は除外。既存カードの設定は保持
  - **手動追加**: 任意のパスを指定してカードを追加（`POST /api/repositories`）。同じIDのカードが既にある場合は拒否（既存の公開済みコミット・連番を上書きしない）
  - **カード削除**: 管理対象から外す（`DELETE /api/repositories/{repo_id}`）※実リポジトリは保持
  - **設定の保存**: 有効/無効、固定ブランチ、初期導入地点（保存時に完全なコミットIDへ解決）、検証の除外パターン（`PUT /api/repositories/{repo_id}`）
  - **対象選択**: トグルスイッチまたは「すべて選択/すべて解除」でパッチ公開対象を選択
  - **パッチ履歴リセット**: パッチ専用リポジトリ（リモート含む）から該当リポジトリの全パッチとインデックス項目を削除・pushし、公開済みコミットをクリアして次回連番を `#000001` に戻す（`POST /api/repositories/{repo_id}/reset`、初期導入地点を現在のHEADへ更新するオプション付き）。パッチ専用リポジトリに未コミットの変更がある場合は実行しない
- **会社環境シミュレーション検証（事前クリーン検証）**（詳細: [clean-verification.md](clean-verification.md)）:
  - 固定ブランチのGit追跡ファイル（会社へ届くファイル）を基準に、ローカルにだけ存在する設定ファイル（`.json`, `.txt`, `.yaml` 等）を欠落エラーとして検知（`POST /api/repositories/{repo_id}/verify`、一括: `POST /api/home/verify-all`）
  - **テンプレート自動除外**: `.example` / `.sample` / `.template` / `.default` がGit追跡されている場合、対応する実ファイルは環境別設定として除外
  - **カスタム除外パターン**（`clean_verify_ignore_patterns`）: リポジトリごとにGlobパターンで除外
  - **差分の公開可否チェック**: 直前の公開地点が固定ブランチの祖先であること、差分に会社側が受け付けないパスやサブモジュール、会社PC（Windows）で作れない名前（予約名、使えない文字、大文字小文字だけが違う同名ファイル）が無いこと、パスが長すぎて会社PCで扱えなくならないこと（会社側のリポジトリまでを50文字と仮定し、退避先を含めて259文字以内）
  - **注意（warnings）**: 公開は止めずに知らせる。追跡されているのに `.gitignore` の対象でもあるファイル（会社側の同名ファイルを上書きする）、Git LFS のファイル（公開時に実体を取り出して同梱する。取得できなければ公開を中止）
- **パッチ公開**（`POST /api/home/publish`）:
  1. パッチ専用リポジトリがクリーンであることを確認し、前回push失敗分があれば先にpush
  2. 有効な全リポジトリの差分範囲を確定（設定不備はここで検出し、何も書き出さない）
  3. 事前クリーン検証（`skip_verification: true` で省略可。画面ではエラー表示後に「検証をスキップして公開」を選べる）
  4. リポジトリごとにパッケージを生成: 差分のバイナリパッチ（既定20MiBで分割）、**新規追加・変更ファイルの実体**（`added_files/`、サイズとSHA-256を記録）、削除ファイル一覧（`deleted_files`）、署名付きマニフェスト
  5. **リポジトリ軽量化**: 過去のパッチディレクトリを削除し、今回分だけの署名付き `package-index.json` を書き出してコミット。パッケージ内のファイルは、同梱したアプリの `.gitignore` の対象であっても強制的にコミットへ含める
  6. **公開前リハーサル**: コミットした内容からGitHubの「Download ZIP」と同じZIPを作り、会社側と同じ検証・適用を一時フォルダで実行して、適用後の内容が公開先コミットと一致することを確認する。問題があればコミットを取り消して中断する（pushしない。省略不可）
  7. push。成功後に、自宅側設定の公開済みコミット（`published_commit`）と連番（`last_sequence`）を保存
  8. 今回分だけを収めた配布用ZIP（`myao_app_patch_YYYYMMDD_HHMMSS_ffffff.zip`）をダウンロードフォルダへ生成し、会社側と同じ検証を通ることを確認
  9. GitHubトークン設定時は GitHub Releases へリリースを作成し、ZIPをアセットとしてアップロード
  - 手順6までに失敗した場合、作りかけのパッケージとコミットを破棄してパッチ専用リポジトリを公開前のコミットへ戻す（次回の公開を妨げない）
  - 手順8・9の失敗は公開全体の失敗にしない（結果の `bundle_error` / `release_error` に理由を返す）
  - 結果の `warnings` に、上記の「注意」を `[表示名] 内容` の形で返す（画面は公開完了の通知に表示する）
  - **大文字小文字だけの名前変更**（`claude.md` → `CLAUDE.md`）では、古い名前を `deleted_files` に載せない（会社PCでは新旧が同じファイルを指すため）
  - **パッチ専用リポジトリのバイナリ保護**: `.gitattributes` に `packages/** binary` と `*.patch.part-* binary` を自動設定し、改行コード正規化による破損を防ぐ
  - **連番の継続**: 自宅側設定の `last_sequence` と、パッチ専用リポジトリに残る最新連番の大きい方から続ける
- **エディタ連携**: 各カードからローカルの Antigravity を開く（`POST /api/open-antigravity`）

### 会社モード (`company`)

- **Node.js / npm 不要**: 同梱の `frontend/dist` を使用
- **Python 実行**: `.venv` なしでもシステムの Python で起動可能（`start_patch_app.bat` または `set PYTHONPATH=src && python -m rep_patch`）
- **リポジトリの対応付け**: `company_repo_paths` の明示指定 → Obsidian設定（`company_obsidian_repo`） → 会社側アプリルート（`company_apps_root`）直下の同名フォルダ、の順に解決
- **カード削除（除外）**: 管理対象から外す（`DELETE /api/company/repositories/{repo_id}`）※実リポジトリは保持。設定ドロワーから除外を解除できる。除外中のリポジトリは一括適用・一括コミットの対象外（個別適用は可能）
- **パッチ受信・検証**（`GET /api/company/downloads`、`POST /api/company/inspect`）:
  - ダウンロードフォルダ内の `myao_app_patch*.zip` を一覧表示。GitHubの「Code → Download ZIP」（`myao_app_patch-main.zip`）と、今回分ZIP（Releases / 手渡し）のどちらも同じ形式として扱う
  - 署名・SHA-256・同梱ファイル・会社側リポジトリとの対応を検証
  - **改行コード変換耐性**: 同梱ファイルの改行コードがLF↔CRLFに変換されていても、マニフェスト記載のハッシュに一致する元のバイト列へ復元して扱う
- **パッチ適用**（一括: `POST /api/company/apply-all`、個別: `POST /api/company/retry`）。リポジトリごとに独立して次を実行:
  1. **自動事前コミット**: 未コミットの変更があれば `[myao-patch] アプリ名 update` としてコミット
  2. 連番の連続性を確認（欠けていれば適用しない）し、対象パスをバックアップ
  3. **ファイル直接削除・上書き配置**: `git apply` は使わず、削除対象を消去（空になったフォルダも片付ける）してから、同梱ファイル実体を上書き配置（会社側独自ファイルや改行コード差分と共存）。削除を先に行うので、大文字小文字だけの名前変更や、ファイルとフォルダの置き換えも適用できる
  4. **即時コミット確定**: `[myao-patch] アプリ名 patch-連番` としてコミット。内容が既に一致していて差分が無い場合は、コミットを作らず適用済みとして連番だけ進める
  5. 失敗した場合は、適用前のコミットとバックアップから復元（他のリポジトリは続行）
  - **Gitユーザー情報自動補完**: `user.name` / `user.email` が未設定でもコミットできる
- **手動変更のコミット**（`POST /api/company/commit-pending`）: 未コミットの変更を一括または個別にコミット
- **適用開始番号指定**（`POST /api/company/repositories/{repo_id}/sequence`、一括: `.../sequence-all`）: 次回適用したい連番を指定すると、直前まで確定済みとして `state.json` を作成
- **エディタ連携**: 各カードからローカルの VS Code を開く（`POST /api/open-vscode`、Windows `code.cmd` / macOS `code` 両対応）
- **環境診断**（`GET /api/diagnostics`）: Python、64bit、Git、ダウンロードフォルダ書き込み、待受アドレス、Gitでのファイル配置・削除・コミットを一時リポジトリで確認

---

## 4. 起動スクリプトとCLI

- **`start_patch_app.bat`**（Windows）:
  - `.venv\Scripts\python.exe` があれば優先使用、なければシステム `python`
  - ポート `17345` の既存プロセスを停止して起動
  - 引数（`%*`）をそのまま `rep_patch` へ渡す
  - cmd.exe が日本語コードページで誤解釈しないよう、ファイル全体をASCIIのみで記述する
- **`start_patch_app.command`**（macOS）: `.venv` 必須。ポート `17345` の既存プロセスを停止して起動。引数（`"$@"`）をそのまま渡す
- **`install_company.bat`**: `.venv` 作成を試み、不可ならシステム環境へ `requirements.lock`（ハッシュ検証付き）をインストール
- **CLI オプション**（この起動の間だけ有効。設定ファイルには保存しない）:
  - `--host`: 待受ホスト（例: `0.0.0.0` でVPN/LAN全体へ公開）
  - `--port`: 待受ポート番号
  - `--allow-vpn`（`--vpn`）: VPN/プライベートネットワーク許可（`--host 0.0.0.0` と同等）
  - `--no-browser`: ブラウザ自動起動を無効化
- 待受ホストにホスト名（FQDN / MagicDNS）を指定した場合は、全インターフェース（`0.0.0.0`）へバインドする

---

## 5. API仕様一覧

`GET` 以外はセッショントークン（`X-Rep-Patch-Token`）が必要。エラーは HTTP 400 と `{"detail": "理由"}`、アクセス制御による拒否は HTTP 403。

| メソッド | パス | 説明 |
| :--- | :--- | :--- |
| `GET` | `/api/health` | サーバーヘルスチェック・モード取得 |
| `GET` | `/api/session` | セッショントークン発行 |
| `GET` | `/api/settings` | 設定取得（パスワード・トークンは伏せ字。起動引数を反映した実際の待受設定を `effective_listen_host` / `effective_listen_port` として付加） |
| `PUT` | `/api/settings` | 設定更新（`repositories` は対象外。リポジトリ登録は下記の専用APIで変更する） |
| `GET` | `/api/repositories` | 登録リポジトリ一覧・状態取得（自宅側） |
| `POST` | `/api/repositories` | リポジトリ手動追加 |
| `PUT` | `/api/repositories/{repo_id}` | リポジトリ設定更新 |
| `DELETE` | `/api/repositories/{repo_id}` | リポジトリ登録削除（自宅側） |
| `POST` | `/api/repositories/{repo_id}/verify` | 単一リポジトリの会社環境シミュレーション検証 |
| `POST` | `/api/home/verify-all` | 全リポジトリ一括の会社環境シミュレーション検証 |
| `POST` | `/api/repositories/{repo_id}/reset` | 単一リポジトリのパッチ履歴リセット・リモート削除・#000001再開 |
| `POST` | `/api/repositories/discover` | アプリルートからのリポジトリ自動再検出 |
| `POST` | `/api/home/publish` | 自宅側パッチ作成・署名・公開（事前クリーン検証ガード付き、自宅モードのみ） |
| `GET` | `/api/company/repositories` | 会社側リポジトリ一覧・状態取得 |
| `DELETE` | `/api/company/repositories/{repo_id}` | 会社側リポジトリカード削除（除外設定） |
| `GET` | `/api/company/downloads` | 会社側パッチZIP一覧取得 |
| `POST` | `/api/company/inspect` | パッチZIPの署名・内容検証 |
| `POST` | `/api/company/apply-all` | 全リポジトリへのパッチ適用 |
| `POST` | `/api/company/retry` | 単一リポジトリへのパッチ適用（個別適用・再試行） |
| `POST` | `/api/company/commit-pending` | 未コミット変更のGitコミット（一括または個別） |
| `POST` | `/api/company/repositories/{repo_id}/sequence` | 単一リポジトリの次回適用開始番号指定・state.json生成 |
| `POST` | `/api/company/repositories/sequence-all` | 全リポジトリ一括の次回適用開始番号指定・state.json生成 |
| `POST` | `/api/open-vscode` | 指定ディレクトリをローカルVS Codeで開く（会社側） |
| `POST` | `/api/open-antigravity` | 指定ディレクトリをローカルAntigravityで開く（自宅側） |
| `GET` | `/api/diagnostics` | 環境診断実行 |

`/api/company/apply-all` と `/api/company/retry` は、旧バージョンの画面が送っていた `correction` を受け取るが無視する（現在は常に即時コミットするため、「修正パッチを重ねる」操作は存在しない）。

---

## 6. データ保存場所

- 設定ファイル: `data/settings.local.json`（Git管理外。パスワードとGitHubトークンを平文で含む）
- 会社側適用状態ファイル: `<各リポジトリ>/.git/rep-patch/state.json`（確定済み連番 `confirmed_sequence`）
- 会社側の適用中バックアップ: `<各リポジトリ>/.git/rep-patch/backups/`（適用の成否にかかわらず終了時に削除）
- パッケージ形式: [package-format.md](package-format.md)
