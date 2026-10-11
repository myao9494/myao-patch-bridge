/**
 * リポジトリ手動追加モーダル（自宅モード）
 *
 * 仕様:
 * - Gitリポジトリのパス（必須）、表示名、種類、固定ブランチ、初期導入地点、検証の除外パターンを入力
 * - 省略した項目はサーバー側で補完される（表示名=フォルダ名、ブランチ=現在のHEAD）
 */
import { useState, type FormEvent } from "react";
import { FolderPlus, Plus, X } from "lucide-react";
import { parsePatterns } from "../format";
import type { RepositoryCreatePayload } from "../types";

export function AddRepositoryModal(props: {
  onClose: () => void;
  onAdd: (payload: RepositoryCreatePayload) => Promise<boolean>;
}) {
  const [path, setPath] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [kind, setKind] = useState<"app" | "obsidian">("app");
  const [branch, setBranch] = useState("");
  const [baselineCommit, setBaselineCommit] = useState("");
  const [cleanVerifyIgnorePatterns, setCleanVerifyIgnorePatterns] = useState("");

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!path.trim()) return;
    const ignoreList = parsePatterns(cleanVerifyIgnorePatterns);
    await props.onAdd({
      path: path.trim(),
      display_name: displayName.trim() || undefined,
      kind,
      branch: branch.trim() || undefined,
      baseline_commit: baselineCommit.trim() || undefined,
      clean_verify_ignore_patterns: ignoreList.length > 0 ? ignoreList : undefined,
      enabled: true,
    });
  };

  return (
    <div className="modal-layer">
      <button className="modal-scrim" onClick={props.onClose} aria-label="閉じる" />
      <div className="modal-card">
        <div className="modal-header">
          <div>
            <p className="eyebrow">ADD REPOSITORY</p>
            <h2><FolderPlus size={20} />リポジトリを追加</h2>
          </div>
          <button className="icon-button" onClick={props.onClose} aria-label="閉じる">
            <X size={18} />
          </button>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="modal-body">
            <label className="field">
              <span>リポジトリのパス <strong className="required">*</strong></span>
              <input
                type="text"
                placeholder="/Users/mine/000_work/app/my-app"
                value={path}
                onChange={(e) => setPath(e.target.value)}
                required
                autoFocus
              />
              <small className="field-hint">Gitリポジトリの絶対パスを入力してください</small>
            </label>

            <label className="field">
              <span>表示名（省略時はフォルダ名）</span>
              <input
                type="text"
                placeholder="my-app"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
              />
            </label>

            <div className="field">
              <span>種類</span>
              <div className="segmented">
                <button
                  type="button"
                  className={kind === "app" ? "active" : ""}
                  onClick={() => setKind("app")}
                >
                  通常アプリ (app)
                </button>
                <button
                  type="button"
                  className={kind === "obsidian" ? "active" : ""}
                  onClick={() => setKind("obsidian")}
                >
                  Obsidian設定 (obsidian)
                </button>
              </div>
            </div>

            <label className="field">
              <span>固定ブランチ（省略時は現在のHEAD）</span>
              <input
                type="text"
                placeholder="main"
                value={branch}
                onChange={(e) => setBranch(e.target.value)}
              />
            </label>

            <label className="field">
              <span>会社の初期導入地点（省略可）</span>
              <input
                type="text"
                placeholder="コミットID、タグ、ブランチ"
                value={baselineCommit}
                onChange={(e) => setBaselineCommit(e.target.value)}
              />
            </label>

            <label className="field">
              <span>会社環境検証の除外パターン（カンマ区切り・省略可）</span>
              <input
                type="text"
                placeholder="backend/data/*, custom.txt 等"
                value={cleanVerifyIgnorePatterns}
                onChange={(e) => setCleanVerifyIgnorePatterns(e.target.value)}
              />
              <small className="field-hint">Git管理外のローカル固有データ等を誤検知から除外します</small>
            </label>
          </div>
          <div className="modal-footer">
            <button type="button" className="button ghost" onClick={props.onClose}>
              キャンセル
            </button>
            <button type="submit" className="button primary" disabled={!path.trim()}>
              <Plus size={16} />リポジトリを追加
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
