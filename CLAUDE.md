# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## これは何か

インターネットに接続できない会社PCへ、自宅で開発した複数のGitリポジトリの変更を、署名付きパッチZIPとして持ち込み適用するためのローカルWeb/PWAアプリ。1つのコードベースが設定の `mode` によって「自宅モード（パッチを作って公開）」と「会社モード（ZIPを検証して適用）」のどちらかとして動く。

機能と振る舞いの仕様は [docs/specification.md](docs/specification.md)（API一覧を含む）にある。ドキュメントは実装を正とし、機能を変えたら同じ変更の中で `docs/` も直す。

## コマンド

```bash
uv sync --extra dev                      # Python依存の導入
uv run pytest                            # 全テスト
uv run pytest tests/test_company_flow.py # 1ファイル
uv run pytest -k end_to_end              # 名前で絞る
uv run pytest --cov=rep_patch --cov-report=term-missing
uv run patch-bridge --no-browser         # API起動（127.0.0.1:17345、data/settings.local.json の実設定を使う）

cd frontend
npm install
npm run lint     # 型チェック（tsc -b）
npm run dev      # Vite開発サーバー（/api を 127.0.0.1:17345 へプロキシ）
npm run build    # frontend/dist を再生成
```

- **`frontend/dist` はGit管理対象**。会社PCにはNode.jsが無く、ビルド済み成果物をそのまま配るため。`frontend/src` を変えたら必ず `npm run build` し、`dist` の差分（ハッシュ付きファイル名が変わる）も一緒にコミットする。
- Pythonのリンター設定は無い。フロントエンドのテストも無い（`npm run lint` の型チェックのみ）。
- Python依存を変えるときは `pyproject.toml`、`requirements.in`、`requirements.lock`（`uv pip compile requirements.in --generate-hashes -o requirements.lock`）を揃える。会社側は `requirements.lock` をハッシュ検証付きでインストールする。

## 全体像

```
自宅PC（home）                 GitHub                    会社PC（company・オフライン）
各アプリのGitリポジトリ ──publish──▶ パッチ専用リポジトリ ──ZIPを人が運ぶ──▶ Downloads ──apply──▶ 独自に git init されたリポジトリ
```

- 自宅と会社は**コミット履歴を共有しない**。そのため運ぶのは履歴ではなく、差分に含まれる**ファイルの実体**（`added_files/`）と削除一覧。会社側は `git apply` を使わず、ファイルを直接上書き・削除してコミットする。
- パッケージ形式（v1）は自宅側と会社側の間の契約である。会社側のアプリは別のタイミングで手作業更新されるため、**古い会社側アプリが新しいZIPを読める／新しい会社側アプリが古いZIPを読める**状態を壊さないこと。マニフェストの `chunks`（分割パッチ）と `target_files` は現在の適用処理では使っていないが、形式v1の検証が要求するので生成し続けている。形式を変えるなら `SCHEMA_VERSION` を上げて移行を設計する。
- パッチ専用リポジトリは「最新のパッチだけ」を作業ツリーに持つ（GitHubの Download ZIP を小さく保つため、公開のたびに過去分を削除する）。連番は自宅側設定の `last_sequence` で引き継ぐ。

### バックエンド（`src/rep_patch/`）

| モジュール | 役割 |
| :--- | :--- |
| `api.py` | FastAPIアプリ。アクセス制御ミドルウェア、全エンドポイント、SPA配信。業務ロジックは持たず下の各モジュールへ委譲 |
| `home.py` | 自宅モード: リポジトリ登録・検出・一覧、`publish`、パッチ履歴リセット |
| `company.py` | 会社モード: ZIP検証、適用（バックアップ→配置→コミット→失敗時復元）、`state.json` |
| `verifier.py` | 公開前の「会社環境シミュレーション検証」（自宅側リポジトリの内容を調べる） |
| `rehearsal.py` | 公開前リハーサル。コミット済みの内容から「Download ZIP」と同じZIPを作り、会社側と同じ検証・適用を一時フォルダで実行する |
| `packages.py` | パッケージ形式: インデックス、分割、`PatchArchive`（ZIPを展開せず検証）、今回分ZIP生成 |
| `security.py` | HMAC署名、安全なパス解決、接続元IP・Hostヘッダーの判定 |
| `git.py` | gitサブプロセスの唯一の入口（`run_git`）と状態・差分・blob取得。LFSフィルターは常に無効（実体の扱いは `lfs.py`） |
| `lfs.py` | Git LFSポインタの判定と実体の取り出し。公開時とリハーサルで使う |
| `config.py` | `Settings` / `RepositoryConfig` と `SettingsStore`（`data/settings.local.json`） |
| `github.py` / `editors.py` / `diagnostics.py` / `fileio.py` | GitHub Releases、エディタ起動、環境診断、原子的書き込み |

依存の向きは `api → home / company → verifier / packages → security / git / config`。これに加えて、公開前リハーサルのために `home → rehearsal → company` がある。`company.py` は `home.py` を読み込まない（会社側の起動にGitHub連携や公開処理を巻き込まない）。

### フロントエンド（`frontend/src/`）

`App.tsx` が全状態とAPI呼び出しを持ち、表示は `components/HomeDashboard.tsx` と `components/CompanyDashboard.tsx` に分かれる。API呼び出しは `api.ts` の `api()` を通す（変更系に自動でセッショントークンを付ける）。操作は `App.tsx` の `perform()` を通して、処理中オーバーレイとエラーバナーを統一する。サーバーとやり取りする型は `types.ts`。

## 守るべき不変条件

過去に実際に壊れた箇所。変更するときはここを崩さないこと（それぞれ回帰テストがある）。

- **設定の保存**: `SettingsStore.load()` は保存済みの値、`load_effective()` は起動引数（`--host` など）を重ねた値を返す。**保存するのは `load()` で得た値だけ**。`load_effective()` の結果を `save()` すると、一時的な `--allow-vpn` が設定ファイルへ恒久的に書き込まれる。
- **排他**: 設定やリポジトリを書き換えるエンドポイントには `api.py` の `@exclusive` を付ける（読み込み→変更→保存の競合と、二重の公開・適用を防ぐ）。`PUT /api/settings` は `repositories` を無視する。リポジトリ登録は専用APIだけが変更する。
- **パッチ専用リポジトリを汚さない**: `publish` は書き出し前に全リポジトリの設定を検証し、途中で失敗したら `_restore_patch_worktree` で公開前のコミットへ戻す。未コミットの残骸があると次回の公開が「未コミットの変更があります」で止まる。
- **パッケージ内のファイルは `git add -f` でステージする**（`_stage_packages`）: `added_files/` には各アプリの `.gitignore` も入り、パッチ専用リポジトリの中で本物の除外設定として働く。通常の `git add` では対象ファイルがコミットから漏れ、会社側でZIP全体が「ZIP内のファイルがありません」で拒否される。
- **pushの前に必ずリハーサルを通す**（`_rehearse`）: 会社へ届くのは作業ツリーではなくコミット済みの内容なので、確認は `git archive` で作ったZIPに対して行う。`skip_verification` でも省略しない。リハーサルは会社側の実コード（`apply_archive`）を呼ぶので、適用処理を変えたらリハーサルも同じ変更を受ける。
- **LFSのポインタを会社へ運ばない**: `added_files` にはLFSの実体を入れる（`lfs.resolve_lfs_pointer`）。実体を取れなければ公開を中止する。チャンクサイズを超える実体は `added_parts/` へ分割し、会社側が結合・照合する。`run_git` がLFSフィルターを無効にするのは、同梱された `.gitattributes` がパッチ専用リポジトリ内で実体をポインタへ戻すため。
- **会社側の適用はリポジトリ単位のトランザクション**: 失敗したら適用前のコミットとバックアップへ戻し、他のリポジトリは続行する。差分が無いパッチ（内容が既に一致）は失敗にせず、連番を進める。
- **適用は「削除 → 配置」の順**: 会社PC（Windows）は大文字小文字を区別しない。`claude.md` → `CLAUDE.md` のような名前変更で配置の後に削除すると、書き込んだばかりの実体を消す。自宅側も、この形の旧パスを `deleted_files` に載せない（配置の後に削除する旧版の会社側アプリを守るため）。
- **パスは必ず `safe_relative_path` / `safe_repo_path` を通す**: マニフェスト由来のパスでリポジトリ外や `.git` 配下へ書き込ませない。署名済みであっても信用しない。
- **gitは `run_git` 経由で呼ぶ**: 端末の入力待ちで固まらない設定（`GIT_TERMINAL_PROMPT=0`、標準入力の切り離し）が入っている。pushなど通信を伴う呼び出しにはタイムアウトを付ける。
- **`.bat` はASCIIのみ**: 日本語を含めると、会社PCのcmd.exe（日本語コードページ）が行を誤解釈することがある。

## テスト

- 実際のgitリポジトリを一時ディレクトリに作って動かす統合テストが中心。`tests/conftest.py` が全テストで `HOME` とGitのグローバル設定を一時領域へ差し替えるので、開発者のGit設定や `~/Downloads` には影響しない／されない。
- リポジトリ作成やAPIクライアントは `tests/helpers.py`（`init_repo`、`commit_all`、`make_patch_repo`、`make_client`、`auth_headers`）を使う。APIテストは `make_client(store, client_host)` で接続元IPを指定できる。
- 自宅→会社の通しは `publish()` の出力を `apply_archive()` に渡して確認する（`test_home_flow.py::test_publish_then_apply_end_to_end`）。パッケージ形式に触れたらここを通すこと。
- 画面を実際に動かして確認するときは、実設定（`data/settings.local.json`）を使う `patch-bridge` をそのまま操作しない。「パッチを作成・公開」は本物のパッチ専用リポジトリへpushする。一時ディレクトリのリポジトリと `SettingsStore(一時パス)` を `create_app()` へ渡したサーバーを立てて確認する。

## 実機固有の事情

- 自宅はmacOS、会社は64bit Windows。Windows固有の経路（`start_patch_app.bat`、`install_company.bat`、`code.cmd`、改行コード変換）はこのMac上では実行確認できない。これらを変更したら、未確認であることを明記する。
- `data/settings.local.json` にはパッチ検証パスワードとGitHubトークンが平文で入っている。内容を表示・コミットしない。
