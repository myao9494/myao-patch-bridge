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
    │   │       └── path/to/new_file.txt
    │   └── 000002/
    └── obsidian-settings/
```

GitHubのソースZIPによる最上位フォルダ名は任意です。ZIP内で `package-index.json` が1つだけ見つかることを条件にします。

Patch Appはパッチ専用リポジトリの `.gitattributes` に `*.patch.part-* binary` を追加し、Gitの改行変換やフィルターで分割バイト列が変わらないようにします。

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
| `changed_paths` | バックアップ対象パス |
| `added_files` | 新規追加ファイルの一覧（相対パス、サイズ、SHA-256） |
| `deleted_files` | 削除されたファイルの一覧 |
| `target_files` | 適用後の全tracked path/blob |
| `signature` | マニフェストのHMAC-SHA-256 |


## 連番

連番はリポジトリごとに1から開始し、欠番を許可しません。会社が複数週分をまとめて持ち込んだ場合、未適用連番を昇順で適用し、最後にまとめて未コミット状態にします。

## 分割

既定は20 MiBです。分割はパッチの生バイト列を順番どおりに切り分けるだけで、会社側で完全に結合してから `git apply` へ渡します。Git LFSは使用しません。

## 不変性

公開済みの連番ディレクトリは上書き・削除しないでください。新しい変更は必ず次の連番として追加します。
