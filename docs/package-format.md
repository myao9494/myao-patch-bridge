# パッチパッケージ仕様

## ディレクトリ構成

```text
myao_app_patch-main/
├── package-index.json
└── packages/
    ├── app-a/
    │   ├── 000001/
    │   │   ├── manifest.json
    │   │   ├── changes.patch.part-000001
    │   │   ├── changes.patch.part-000002
    │   │   └── added_files/
    │   │       └── path/to/changed_or_new_file.txt
    └── obsidian-settings/
        └── 000004/
```

ZIPの最上位フォルダ名（`myao_app_patch-main/` など）は任意です。会社側は `package-index.json` の位置を基準に読み込むため、GitHubの「Download ZIP」と今回分ZIP（最上位フォルダなし）のどちらも同じように扱えます。

### リポジトリの自動軽量化仕様
パッチ専用リポジトリのワーキングツリー（最新コミット）には、**今回公開された最新パッチディレクトリのみ** が保持されます。
過去のパッチディレクトリは自動的に削除（Gitコミット履歴には保持）され、`package-index.json` も今回分のみで再署名されます。
これにより、GitHubの「Code → Download ZIP」（`myao_app_patch-main.zip`）でダウンロードされるZIPサイズが常に今回分だけの最小サイズ（数KB〜数MB）に保たれます。

### 配布用軽量ZIP（ローカル生成 / GitHub Releases アセット）
パッチ公開時にダウンロードフォルダへ直接出力される単一ZIP（`myao_app_patch_YYYYMMDD_HHMMSS_ffffff.zip`）です。
リポジトリの「Download ZIP」と同様に、今回作成された連番フォルダと今回分の `package-index.json` のみで構成されます。

### バイナリ保護と改行コード自動復元
Patch Appはパッチ専用リポジトリの `.gitattributes` に `packages/** binary` および `*.patch.part-* binary` を設定し、Gitの改行変換（eol/autocrlf）やフィルターでパッチ分割バイト列および追加ファイル実体（Obsidianプラグインや各種ソースファイル）が変わらないように保護します。
さらに会社側のパッケージ検証・読み込み時にも、Gitやダウンロードアーカイブによる改行コード正規化（CRLF↔LF）の差異を自動検知し、マニフェスト記載のハッシュに合致するよう透過的に元のバイト列を復元して配置する耐性機構を備えています。

## package-index.json

```json
{
  "schema_version": 1,
  "package_type": "myao-patch-bridge",
  "packages": [
    {
      "repo_id": "app-a",
      "sequence": 1,
      "manifest_path": "packages/app-a/000001/manifest.json"
    }
  ],
  "signature": "HMAC-SHA-256"
}
```

## manifest.json

主な項目は次のとおりです。

| 項目 | 意味 |
|---|---|
| `repo_id` | フォルダ名に依存しない安定ID |
| `display_name` | 画面表示と会社側の自動マッピング名 |
| `kind` | `app` または `obsidian` |
| `sequence` | リポジトリ単位の連続番号 |
| `source_branch` | 自宅側の固定ブランチ |
| `source_from_commit` | 差分開始コミット |
| `source_to_commit` | 差分到達コミット |
| `patch_size` | 分割前のバイト数 |
| `patch_sha256` | 分割前パッチのSHA-256 |
| `chunks` | 分割名、サイズ、SHA-256、順序 |
| `changed_paths` | 差分に含まれるパス（バックアップ対象） |
| `added_files` | 新規追加・変更されたファイルの一覧（相対パス、サイズ、SHA-256）。実体は `added_files/` 配下 |
| `deleted_files` | 削除されたファイルの一覧（リネームの旧パスを含む。ただし `added_files` のいずれかと大文字小文字だけが違うパスは含めない） |
| `target_files` | 公開時点の全追跡ファイル（path / blob / mode / type） |
| `signature` | マニフェストのHMAC-SHA-256 |

### 会社側の適用で使う項目

会社側は `added_files`（実体を上書き配置）と `deleted_files`（消去）だけで適用します。`changed_paths` は適用前バックアップの対象決定に使います。

`chunks` / `patch_size` / `patch_sha256`（差分パッチ本体）と `target_files` は、形式v1の互換性のために生成・検証していますが、現在の適用処理では使用しません。形式v1を読む旧バージョンの会社側アプリは `chunks` の存在を必須として検証するため、省略できません。

リネームは「旧パスの削除＋新パスの追加」として記録します。シンボリックリンクはリンク先パスを内容とする通常ファイルとして配置されます。サブモジュールを含む差分は公開できません。

大文字小文字だけを変えたリネーム（`claude.md` → `CLAUDE.md`）では、旧パスを `deleted_files` に載せません。会社PC（Windows）では新旧のパスが同じファイルを指すため、配置の後で削除を行う版の会社側アプリが、書き込んだばかりの実体を消してしまうからです。現在の会社側アプリは削除を先に行い、配置時に名前の大文字小文字も合わせるので、旧パスが載っている過去のパッケージも正しく適用できます。

### 同梱ファイルとパッチ専用リポジトリのGit設定

`added_files/` には各アプリの `.gitignore` や `.gitattributes` もそのまま入ります。これらはパッチ専用リポジトリの中でも設定ファイルとして働きます。

- `.gitignore`: 対象になった同梱ファイルが通常の `git add` で無視され、コミットとZIPから漏れます（会社側で「ZIP内のファイルがありません」）。公開処理はパッケージのフォルダを `git add -f` で強制的に追加します。
- `.gitattributes`: `export-ignore` はGitHubのZIPからファイルを落とし、`eol` / `working-tree-encoding` / `ident` は内容を変換します。これらは防げないため、公開前リハーサルが「Download ZIP」と同じZIPで検出し、pushの前に公開を中止します。


## 連番

連番はリポジトリごとに1から開始し、欠番を許可しません。1つのZIPに同じリポジトリの連続した複数連番が含まれる場合、未適用分を昇順で適用し、`patch-000002-000004` のように1つのコミットにまとめます。

パッチ専用リポジトリの最新コミットには最新の連番だけが残るため、会社側は公開のたびに取り込む運用が前提です。取り込みを飛ばした場合は連番が欠け、適用できません（[トラブルシューティング](troubleshooting.md) を参照）。

## 分割

分割の対象は差分パッチ本体（`changes.patch.part-*`）で、既定は20 MiBです。パッチの生バイト列を順番どおりに切り分けます。同梱ファイル実体（`added_files/`）は分割しません。Git LFSは使用しません。

## 不変性

公開済みの連番を同じ番号で作り直さないでください。新しい変更は必ず次の連番として公開します。過去の連番ディレクトリの削除は、公開処理が行う自動軽量化と「パッチ履歴のリセット」に任せ、手作業では行いません。
