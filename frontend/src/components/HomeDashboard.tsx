/**
 * 自宅モードのダッシュボード
 *
 * 仕様:
 * - 未公開コミット数と公開対象数の表示
 * - ツールバー: リポジトリ追加、会社環境検証（一括）、再検出、更新、パッチ作成・公開
 * - 公開対象の一括選択・解除
 * - リポジトリカード: 有効/無効トグル、固定ブランチ・初期導入地点・検証の除外パターンの編集と保存、
 *   検証結果の表示、個別検証、パッチ履歴リセット、Antigravityで開く、カード削除
 */
import { useState } from "react";
import {
  AlertTriangle,
  Archive,
  Check,
  CheckSquare,
  CloudUpload,
  GitBranch,
  HardDrive,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldCheck,
  Sparkles,
  Square,
  Trash2,
} from "lucide-react";
import { joinPatterns, parsePatterns, shortHash } from "../format";
import type { CleanVerificationResult, Repository } from "../types";
import { EmptyState } from "./common";

export function HomeDashboard(props: {
  repositories: Repository[];
  unpublished: number;
  verificationResults: Record<string, CleanVerificationResult>;
  onDiscover: () => void;
  onRefresh: () => void;
  onVerifyAll: () => void;
  onVerifyRepo: (repo: Repository) => void;
  onPublish: () => void;
  onUpdateRepo: (id: string, value: Partial<Repository>) => void;
  onSaveRepo: (repo: Repository) => void;
  onDeleteRepo: (repo: Repository) => void;
  onResetRepo: (repo: Repository) => void;
  onOpenAddModal: () => void;
  onToggleAll: (enable: boolean) => void;
  onOpenAntigravity: (path: string) => void;
}) {
  const allEnabled = props.repositories.length > 0 && props.repositories.every((r) => r.enabled);
  const someEnabled = props.repositories.some((r) => r.enabled);

  return <>
    <section className="hero home-hero">
      <div><p className="eyebrow">PATCH PUBLISHER</p><h1>変更を、確実な<br/>受け渡し単位へ。</h1><p>対象リポジトリを選択し、登録ブランチの差分だけを安全にパッケージとして公開します。</p></div>
      <div className="hero-stat"><span>未公開コミット</span><strong>{props.unpublished}</strong><small>{props.repositories.filter((item) => item.enabled).length} / {props.repositories.length} repos selected</small></div>
    </section>
    <section className="toolbar">
      <div>
        <h2>対象リポジトリ</h2>
        <p>カードの追加・削除やトグルで対象を選択し、会社環境検証・ブランチ・導入地点を管理します</p>
      </div>
      <div className="button-row wrap">
        <button className="button primary" onClick={props.onOpenAddModal}>
          <Plus size={17} />リポジトリを追加
        </button>
        <button className="button secondary" onClick={props.onVerifyAll} title="Git追跡ファイルのみを展開したクリーン環境で、設定ファイルの欠落やパッチ適用エラーがないか一括検証します">
          <ShieldCheck size={17} />会社環境検証
        </button>
        <button className="button ghost" onClick={props.onDiscover}>
          <HardDrive size={17} />再検出
        </button>
        <button className="button ghost" onClick={props.onRefresh}>
          <RefreshCw size={17} />更新
        </button>
        <button className="button dark" onClick={props.onPublish} disabled={!someEnabled}>
          <CloudUpload size={17} />パッチを作成・公開
        </button>
      </div>
    </section>

    {props.repositories.length > 0 && (
      <div className="selection-bar">
        <span className="selection-count">
          <strong>{props.repositories.filter((r) => r.enabled).length}</strong> / {props.repositories.length} 件が公開対象
        </span>
        <div className="selection-actions">
          <button
            className="text-action-button"
            onClick={() => props.onToggleAll(true)}
            disabled={allEnabled}
          >
            <CheckSquare size={14} />すべて選択
          </button>
          <button
            className="text-action-button"
            onClick={() => props.onToggleAll(false)}
            disabled={!someEnabled}
          >
            <Square size={14} />すべて解除
          </button>
        </div>
      </div>
    )}

    <div className="repo-grid">
      {props.repositories.map((repo) => (
        <RepositoryCard
          key={repo.repo_id}
          repo={repo}
          verification={props.verificationResults[repo.repo_id]}
          onUpdateRepo={props.onUpdateRepo}
          onSaveRepo={props.onSaveRepo}
          onDeleteRepo={props.onDeleteRepo}
          onResetRepo={props.onResetRepo}
          onVerifyRepo={props.onVerifyRepo}
          onOpenAntigravity={props.onOpenAntigravity}
        />
      ))}
      {!props.repositories.length && (
        <EmptyState
          icon={<Archive />}
          title="リポジトリが登録されていません"
          text="「リポジトリを追加」ボタンから手動登録するか、設定のアプリルートを指定して「再検出」を押してください"
        />
      )}
    </div>
  </>;
}

function RepositoryCard(props: {
  repo: Repository;
  verification?: CleanVerificationResult;
  onUpdateRepo: (id: string, value: Partial<Repository>) => void;
  onSaveRepo: (repo: Repository) => void;
  onDeleteRepo: (repo: Repository) => void;
  onResetRepo: (repo: Repository) => void;
  onVerifyRepo: (repo: Repository) => void;
  onOpenAntigravity: (path: string) => void;
}) {
  const { repo, verification } = props;
  // 入力途中のカンマや空白を保つため、除外パターンは入力中の文字列をそのまま保持する
  const [patternText, setPatternText] = useState(() => joinPatterns(repo.clean_verify_ignore_patterns));

  return (
    <article className={`repo-card ${repo.enabled ? "" : "disabled"}`}>
      <div className="repo-heading">
        <div className={`repo-icon ${repo.kind}`}><GitBranch size={20} /></div>
        <div>
          <h3>{repo.display_name}</h3>
          <p>{repo.path}</p>
        </div>
        <div className="repo-header-actions">
          <label className="switch" title={repo.enabled ? "パッチ作成の対象（クリックで無効化）" : "パッチ作成対象外（クリックで有効化）"}>
            <input
              type="checkbox"
              checked={repo.enabled}
              onChange={(e) => {
                const nextVal = e.target.checked;
                props.onUpdateRepo(repo.repo_id, { enabled: nextVal });
                props.onSaveRepo({ ...repo, enabled: nextVal });
              }}
            />
            <span />
          </label>
          <button
            className="icon-button delete-repo-btn"
            title="このリポジトリカードを削除"
            aria-label={`${repo.display_name} を削除`}
            onClick={() => props.onDeleteRepo(repo)}
          >
            <Trash2 size={16} />
          </button>
        </div>
      </div>
      {repo.error ? (
        <div className="inline-error">{repo.error}</div>
      ) : (
        <>
          <div className="repo-metrics">
            <div>
              <span>公開済み</span>
              <code>{shortHash(repo.published_commit || repo.baseline_commit)}</code>
            </div>
            <div>
              <span>現在</span>
              <code>{shortHash(repo.target_commit)}</code>
            </div>
            <div>
              <span>未公開</span>
              <strong>{repo.unpublished_commits ?? 0}</strong>
            </div>
          </div>
          <label>
            固定ブランチ
            <input
              value={repo.branch}
              onChange={(e) => props.onUpdateRepo(repo.repo_id, { branch: e.target.value })}
            />
          </label>
          <label>
            会社の初期導入地点
            <input
              placeholder="コミットID、タグ、ブランチ"
              value={repo.baseline_commit}
              onChange={(e) => props.onUpdateRepo(repo.repo_id, { baseline_commit: e.target.value })}
            />
          </label>
          <label>
            会社環境検証の除外パターン（カンマ区切り）
            <input
              placeholder="backend/data/*, custom.txt 等（省略可）"
              value={patternText}
              onChange={(e) => {
                setPatternText(e.target.value);
                props.onUpdateRepo(repo.repo_id, {
                  clean_verify_ignore_patterns: parsePatterns(e.target.value),
                });
              }}
            />
          </label>
          {verification && (
            <div style={{ marginTop: "10px", padding: "8px 12px", borderRadius: "6px", fontSize: "0.85rem", backgroundColor: verification.valid ? "#e6f4ea" : "#fce8e6", color: verification.valid ? "#137333" : "#c5221f" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "6px", fontWeight: "bold" }}>
                {verification.valid ? (
                  <><Check size={16} />会社環境検証OK（設定ファイル欠落なし）</>
                ) : (
                  <><AlertTriangle size={16} />会社環境でファイル不在エラーが検知されました</>
                )}
              </div>
              {!verification.valid && verification.errors.length > 0 && (
                <ul style={{ margin: "6px 0 0 16px", padding: 0, fontSize: "0.8rem" }}>
                  {verification.errors.map((err, i) => (
                    <li key={i}>{err}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
          <div className="button-row" style={{ marginTop: "12px", justifyContent: "space-between", alignItems: "center" }}>
            <button className="text-button" onClick={() => props.onSaveRepo(repo)}>
              <Save size={15} />この設定を保存
            </button>
            <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
              <button
                className="button ghost small"
                title="会社側クリーン環境シミュレーション検証を実行"
                onClick={() => props.onVerifyRepo(repo)}
              >
                <ShieldCheck size={14} />検証
              </button>
              <button
                className="button ghost small"
                title="パッチ履歴をリセットして#000001から再作成可能にします"
                onClick={() => props.onResetRepo(repo)}
              >
                <RotateCcw size={14} />リセット
              </button>
              <button
                className="button ghost small"
                title="Antigravityで開く"
                onClick={() => props.onOpenAntigravity(repo.path)}
              >
                <Sparkles size={14} />Antigravityで開く
              </button>
            </div>
          </div>
        </>
      )}
    </article>
  );
}
