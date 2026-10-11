/**
 * パッチ履歴リセットの確認モーダル（自宅モード）
 *
 * 仕様:
 * - リセットの影響（リモートを含む全パッチ削除、連番 #000001 への初期化、公開済みコミットのクリア）を表示
 * - 「現在の最新コミットを会社の初期導入地点にする」オプションを選択可能
 */
import { useState } from "react";
import { RotateCcw, X } from "lucide-react";
import { shortHash } from "../format";
import type { Repository } from "../types";

export function ResetRepositoryModal(props: {
  repo: Repository;
  onClose: () => void;
  onReset: (repo: Repository, newBaseline?: string) => Promise<void>;
}) {
  const [resetBaselineToHead, setResetBaselineToHead] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const handleConfirm = async () => {
    setSubmitting(true);
    try {
      const newBaseline = resetBaselineToHead ? (props.repo.target_commit || undefined) : undefined;
      await props.onReset(props.repo, newBaseline);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-layer">
      <button className="modal-scrim" onClick={props.onClose} aria-label="閉じる" />
      <div className="modal-card">
        <div className="modal-header">
          <div>
            <p className="eyebrow">RESET PATCHES</p>
            <h2><RotateCcw size={20} />パッチ履歴をリセット</h2>
          </div>
          <button className="icon-button" onClick={props.onClose} aria-label="閉じる">
            <X size={18} />
          </button>
        </div>
        <div className="modal-body">
          <div style={{ marginBottom: "16px" }}>
            <p style={{ margin: "0 0 4px", fontSize: "14px", fontWeight: 600 }}>
              対象: {props.repo.display_name}
            </p>
            <p style={{ margin: 0, fontSize: "12px", color: "#6b746f" }}>
              {props.repo.path}
            </p>
          </div>

          <div style={{
            background: "#fffbeb",
            border: "1px solid #fef3c7",
            borderRadius: "4px",
            padding: "12px 14px",
            fontSize: "13px",
            lineHeight: 1.6,
            color: "#92400e",
            marginBottom: "16px",
          }}>
            <strong>【注意事項】</strong>
            <ul style={{ margin: "6px 0 0", paddingLeft: "18px" }}>
              <li>パッチ専用リポジトリ（リモート含む）からこのリポジトリの全パッチファイルが削除されます。</li>
              <li>次回のパッチ作成番号は <strong>#000001</strong> にリセットされます。</li>
              <li>公開済みコミットの記録がクリアされます。</li>
            </ul>
          </div>

          <label style={{ display: "flex", gap: "10px", alignItems: "flex-start", cursor: "pointer", fontSize: "13px" }}>
            <input
              type="checkbox"
              checked={resetBaselineToHead}
              onChange={(e) => setResetBaselineToHead(e.target.checked)}
              style={{ marginTop: "3px" }}
            />
            <div>
              <strong>現在の最新コミット ({shortHash(props.repo.target_commit)}) を会社の初期導入地点にする</strong>
              <p style={{ margin: "4px 0 0", fontSize: "12px", color: "#6b746f" }}>
                {resetBaselineToHead
                  ? "現在のHEADを初期導入地点に更新し、今後追加された変更から新しく #000001 を作成します。"
                  : `既存の初期導入地点 (${shortHash(props.repo.baseline_commit)}) を維持し、ここからの差分で次回 #000001 を作成します。`}
              </p>
            </div>
          </label>
        </div>
        <div className="modal-footer">
          <button type="button" className="button ghost" onClick={props.onClose} disabled={submitting}>
            キャンセル
          </button>
          <button
            type="button"
            className="button danger"
            onClick={handleConfirm}
            disabled={submitting}
          >
            <RotateCcw size={16} />
            {submitting ? "リセット中..." : "パッチ履歴をリセット"}
          </button>
        </div>
      </div>
    </div>
  );
}
