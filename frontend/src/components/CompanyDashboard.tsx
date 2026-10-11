/**
 * 会社モードのダッシュボード
 *
 * 仕様:
 * - STEP 1: ダウンロードフォルダ内のパッチZIPを選択
 * - STEP 2: 署名・SHA-256・会社側リポジトリとの対応を検証して結果を表示
 * - STEP 3: 全アプリへ一括適用（未コミット変更の事前コミット → 適用 → 即時コミット）
 * - リポジトリ個別操作: 状態表示、手動変更のコミット、個別適用、開始番号設定、VS Codeで開く、カード削除
 * - 処理結果と会社環境診断の表示
 */
import {
  Check,
  ChevronRight,
  CircleDot,
  Code,
  Download,
  FileArchive,
  GitBranch,
  HardDrive,
  Hash,
  PackageCheck,
  RefreshCw,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { formatBytes, formatSequence } from "../format";
import type {
  CompanyRepository,
  DiagnosticsResult,
  DownloadPackage,
  OperationResult,
  PackageSummary,
} from "../types";
import { EmptyState } from "./common";

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

export function CompanyDashboard(props: {
  downloads: DownloadPackage[];
  selectedZip: string;
  setSelectedZip: (path: string) => void;
  summary: PackageSummary | null;
  results: OperationResult[];
  companyRepos: CompanyRepository[];
  diagnostics: DiagnosticsResult | null;
  onRefresh: () => void;
  onRefreshRepos: () => void;
  onInspect: () => void;
  onApply: () => void;
  onRetry: (id: string) => void;
  onCommit: () => void;
  onCommitSingle: (id: string) => void;
  onSetSequence: (repoId: string, displayName: string, currentConfirmed: number) => void;
  onSetAllRepoSequence: () => void;
  onDeleteRepo: (repo: CompanyRepository) => void;
  onDiagnostics: () => void;
  onOpenVsCode: (path: string) => void;
  listenHost?: string;
}) {
  const isRemoteAllowed = Boolean(props.listenHost) && !LOOPBACK_HOSTS.has(props.listenHost ?? "");
  return <>
    <section className="hero company-hero">
      <div><p className="eyebrow">OFFLINE APPLIER</p><h1>確認してから、<br/>次へ進める。</h1><p>DownloadsのZIPを直接検証し、アプリ単位で安全に適用・復元します。</p></div>
      <div className="shield">
        <ShieldCheck size={54}/>
        <span>{isRemoteAllowed ? "VPN / LAN" : "LOCAL ONLY"}</span>
        <small>{props.listenHost || "127.0.0.1"}</small>
      </div>
    </section>
    <section className="workspace-grid">
      <article className="panel package-panel">
        <div className="panel-heading"><div><p className="step">STEP 1</p><h2>パッチZIPを選択</h2></div><button className="icon-button subtle" onClick={props.onRefresh}><RefreshCw size={18}/></button></div>
        <div className="download-list">
          {props.downloads.map((item) => <label className={`download-item ${props.selectedZip === item.path ? "selected" : ""}`} key={item.path}>
            <input type="radio" name="zip" checked={props.selectedZip === item.path} onChange={() => props.setSelectedZip(item.path)}/>
            <FileArchive size={24}/><span><strong>{item.name}</strong><small>{new Date(item.modified_at).toLocaleString("ja-JP")} · {formatBytes(item.size)}</small></span><ChevronRight size={18}/>
          </label>)}
          {!props.downloads.length && <EmptyState icon={<Download/>} title="ZIPがありません" text="Downloadsへ myao_app_patch のZIPを保存してください"/>}
        </div>
        <button className="button dark full" disabled={!props.selectedZip} onClick={props.onInspect}><ShieldCheck size={18}/>署名と内容を検証</button>
      </article>
      <article className="panel verification-panel">
        <div className="panel-heading"><div><p className="step">STEP 2</p><h2>検証結果</h2></div>{props.summary && <span className="verified"><Check size={15}/>VERIFIED</span>}</div>
        {props.summary ? <>
          <div className="summary-strip"><div><span>Repositories</span><strong>{props.summary.repository_count}</strong></div><div><span>Patch sets</span><strong>{props.summary.package_count}</strong></div></div>
          <div className="summary-repos">{props.summary.repositories.map((repo) => <div key={repo.repo_id}><PackageCheck size={18}/><span><strong>{repo.display_name}</strong><small>{formatSequence(repo.first_sequence)} → {formatSequence(repo.last_sequence)} · {formatBytes(repo.total_patch_size)}</small>{repo.mapping_error && <em>{repo.mapping_error}</em>}</span></div>)}</div>
        </> : <EmptyState icon={<ShieldCheck/>} title="まだ検証されていません" text="左のZIPを選び、署名と内容を検証してください"/>}
      </article>
    </section>
    <section className="action-panel">
      <div><p className="step">STEP 3</p><h2>全アプリへ一括適用・コミット</h2><p>未コミット変更があれば事前に自動コミットし、パッチを適用して最新コミットまで完了させます（常にクリーン状態を維持）。</p></div>
      <div className="button-row wrap"><button className="button ghost" onClick={props.onDiagnostics}><ShieldCheck size={17}/>会社環境診断</button><button className="button ghost" onClick={props.onCommit}><PackageCheck size={17}/>手動変更をすべてコミット</button><button className="button primary" disabled={!props.summary} onClick={props.onApply}><PackageCheck size={17}/>全アプリへ適用（即時コミット）</button></div>
    </section>

    <section className="company-repos-section">
      <div className="section-heading">
        <div>
          <h2>会社側リポジトリ個別操作</h2>
          <p>各リポジトリの未コミット変更と適用済み連番を確認し、個別に適用やコミットを実行できます</p>
        </div>
        <div className="button-row">
          <button className="button ghost" onClick={props.onSetAllRepoSequence} title="全アプリの適用開始番号を一括指定してstate.jsonを生成">
            <Hash size={17} />全アプリの開始番号設定
          </button>
          <button className="button ghost" onClick={props.onRefreshRepos}>
            <RefreshCw size={17} />状態更新
          </button>
        </div>
      </div>
      <div className="repo-grid">
        {props.companyRepos.map((repo) => (
          <article className="repo-card" key={repo.repo_id}>
            <div className="repo-heading">
              <div className={`repo-icon ${repo.kind}`}>
                <GitBranch size={20} />
              </div>
              <div>
                <h3>{repo.display_name}</h3>
                <p>{repo.path}</p>
              </div>
              <div className="repo-header-actions">
                <div className="repo-status-badge">
                  {!repo.clean ? (
                    <span className="badge warning">未コミット変更 ({repo.changes}件)</span>
                  ) : (
                    <span className="badge success">同期済み ({formatSequence(repo.confirmed_sequence)})</span>
                  )}
                </div>
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
              <div>
                <div className="repo-metrics">
                  <div><span>ブランチ</span><code>{repo.branch || "-"}</code></div>
                  <div><span>HEAD</span><code>{repo.head ? repo.head.slice(0, 7) : "-"}</code></div>
                  <div><span>次回適用</span><code>{formatSequence(repo.confirmed_sequence + 1)}〜</code></div>
                  <div><span>変更数</span><strong>{repo.changes}</strong></div>
                </div>
                <div className="button-row wrap" style={{ marginTop: "14px" }}>
                  <button
                    className="button ghost small"
                    disabled={repo.clean}
                    onClick={() => props.onCommitSingle(repo.repo_id)}
                  >
                    <Check size={14} />手動変更をコミット
                  </button>
                  <button
                    className="button primary small"
                    disabled={!props.selectedZip}
                    onClick={() => props.onRetry(repo.repo_id)}
                  >
                    <PackageCheck size={14} />このアプリに適用
                  </button>
                  <button
                    className="button ghost small"
                    title="適用開始番号を指定してstate.jsonを生成"
                    onClick={() => props.onSetSequence(repo.repo_id, repo.display_name, repo.confirmed_sequence)}
                  >
                    <Hash size={14} />開始番号設定
                  </button>
                  <button
                    className="button ghost small"
                    title="VS Codeで開く"
                    onClick={() => props.onOpenVsCode(repo.path)}
                  >
                    <Code size={14} />VS Codeで開く
                  </button>
                </div>
              </div>
            )}
          </article>
        ))}
        {!props.companyRepos.length && (
          <EmptyState
            icon={<HardDrive />}
            title="会社側リポジトリが見つかりません"
            text="設定メニューで「会社側アプリルート」または「会社側Obsidian設定」のパスを設定してください"
          />
        )}
      </div>
    </section>

    {props.results.length > 0 && <section className="results"><h2>処理結果</h2>{props.results.map((item) => <div className={`result ${item.status}`} key={item.repo_id}><span className="result-icon">{item.status === "failed" ? <X/> : item.status === "unchanged" ? <CircleDot/> : <Check/>}</span><div><strong>{item.display_name}</strong><p>{item.message}</p></div>{item.status === "failed" && <button className="button ghost" onClick={() => props.onRetry(item.repo_id)}>このアプリだけ再実行</button>}</div>)}</section>}
    {props.diagnostics && <section className="results"><h2>会社環境診断</h2>{props.diagnostics.checks.map((item) => <div className={`result ${item.status === "ok" ? "applied" : "failed"}`} key={item.name}><span className="result-icon">{item.status === "ok" ? <Check/> : <X/>}</span><div><strong>{item.name}</strong><p>{item.detail}</p></div></div>)}</section>}
  </>;
}
