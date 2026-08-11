import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Archive,
  Check,
  ChevronRight,
  CircleDot,
  CloudUpload,
  Download,
  FileArchive,
  GitBranch,
  HardDrive,
  LoaderCircle,
  Menu,
  PackageCheck,
  RefreshCw,
  RotateCcw,
  Save,
  Settings as SettingsIcon,
  ShieldCheck,
  Smartphone,
  X,
} from "lucide-react";
import { api, jsonBody } from "./api";
import type {
  DownloadPackage,
  OperationResult,
  PackageSummary,
  Repository,
  Settings,
} from "./types";

const emptySettings: Settings = {
  mode: "home",
  apps_root: "",
  obsidian_repo: "",
  patch_repo: "",
  company_apps_root: "",
  company_obsidian_repo: "",
  download_dir: "",
  patch_password: "",
  password_configured: false,
  listen_host: "127.0.0.1",
  listen_port: 17345,
  chunk_size_mib: 20,
  company_repo_paths: {},
  repositories: {},
};

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(1)} GiB`;
}

function shortHash(value?: string): string {
  return value ? value.slice(0, 8) : "未設定";
}

export default function App() {
  const [settings, setSettings] = useState<Settings>(emptySettings);
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [drawer, setDrawer] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [downloads, setDownloads] = useState<DownloadPackage[]>([]);
  const [selectedZip, setSelectedZip] = useState("");
  const [summary, setSummary] = useState<PackageSummary | null>(null);
  const [results, setResults] = useState<OperationResult[]>([]);
  const [diagnostics, setDiagnostics] = useState<any>(null);
  const [installPrompt, setInstallPrompt] = useState<BeforeInstallPromptEvent | null>(null);

  const perform = useCallback(async <T,>(label: string, work: () => Promise<T>): Promise<T | undefined> => {
    setBusy(label);
    setError("");
    setNotice("");
    try {
      return await work();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      return undefined;
    } finally {
      setBusy("");
    }
  }, []);

  const loadSettings = useCallback(async () => {
    const value = await api<Settings>("/api/settings");
    setSettings(value);
    return value;
  }, []);

  const loadRepositories = useCallback(async () => {
    const value = await api<{ repositories: Repository[] }>("/api/repositories");
    setRepositories(value.repositories);
  }, []);

  const loadDownloads = useCallback(async () => {
    const value = await api<{ packages: DownloadPackage[] }>("/api/company/downloads");
    setDownloads(value.packages);
    if (value.packages.length) setSelectedZip((current) => current || value.packages[0].path);
  }, []);

  useEffect(() => {
    perform("初期化中", async () => {
      const value = await loadSettings();
      if (value.mode === "home") await loadRepositories().catch(() => undefined);
      else await loadDownloads();
    });
  }, [loadDownloads, loadRepositories, loadSettings, perform]);

  useEffect(() => {
    const capture = (event: Event) => {
      event.preventDefault();
      setInstallPrompt(event as BeforeInstallPromptEvent);
    };
    window.addEventListener("beforeinstallprompt", capture);
    return () => window.removeEventListener("beforeinstallprompt", capture);
  }, []);

  const installPwa = async () => {
    if (!installPrompt) return;
    await installPrompt.prompt();
    await installPrompt.userChoice;
    setInstallPrompt(null);
  };

  const saveSettings = async () => {
    const updated = await perform("設定を保存中", () =>
      api<Settings>("/api/settings", {
        method: "PUT",
        ...jsonBody({ values: settings }),
      })
    );
    if (updated) {
      setSettings(updated);
      setDrawer(false);
      setNotice("設定を保存しました。ポート変更は再起動後に反映されます。");
      if (updated.mode === "company") await loadDownloads();
    }
  };

  const discover = async () => {
    const value = await perform("リポジトリを検出中", () =>
      api<{ repositories: Repository[] }>("/api/repositories/discover", { method: "POST" })
    );
    if (value) setRepositories(value.repositories);
  };

  const saveRepository = async (repository: Repository) => {
    const value = await perform(`${repository.display_name}を保存中`, () =>
      api<Repository>(`/api/repositories/${repository.repo_id}`, {
        method: "PUT",
        ...jsonBody({
          values: {
            enabled: repository.enabled,
            branch: repository.branch,
            baseline_commit: repository.baseline_commit,
          },
        }),
      })
    );
    if (value) {
      setNotice(`${repository.display_name}の設定を保存しました`);
      await loadRepositories();
    }
  };

  const publish = async () => {
    if (!confirm("変更パッチを作成し、パッチ専用リポジトリへpushしますか？")) return;
    const value = await perform("パッチを作成・公開中", () =>
      api<{ published: boolean; message: string }>("/api/home/publish", { method: "POST" })
    );
    if (value) {
      setNotice(value.message);
      await loadRepositories();
    }
  };

  const inspect = async () => {
    if (!selectedZip) return;
    const value = await perform("ZIPを検証中", () =>
      api<PackageSummary>("/api/company/inspect", {
        method: "POST",
        ...jsonBody({ zip_path: selectedZip }),
      })
    );
    if (value) {
      setSummary(value);
      setNotice("署名・分割ファイル・SHA-256の検証に成功しました");
    }
  };

  const applyAll = async (correction = false) => {
    const prompt = correction
      ? "前回の未コミット変更へ、修正パッチを重ねて適用しますか？"
      : "確認済みの前回分をコミットし、全アプリへ新しいパッチを適用しますか？";
    if (!selectedZip || !confirm(prompt)) return;
    const value = await perform("全アプリを処理中", () =>
      api<{ results: OperationResult[] }>("/api/company/apply-all", {
        method: "POST",
        ...jsonBody({ zip_path: selectedZip, correction }),
      })
    );
    if (value) setResults(value.results);
  };

  const retry = async (repoId: string) => {
    const value = await perform("アプリを再実行中", () =>
      api<{ results: OperationResult[] }>("/api/company/retry", {
        method: "POST",
        ...jsonBody({ zip_path: selectedZip, repo_id: repoId, correction: false }),
      })
    );
    if (value) setResults((previous) => previous.filter((item) => item.repo_id !== repoId).concat(value.results));
  };

  const commitAll = async () => {
    if (!confirm("動作確認済みの未コミットパッチをすべてコミットしますか？")) return;
    const value = await perform("確認済みパッチをコミット中", () =>
      api<{ results: OperationResult[] }>("/api/company/commit-pending", {
        method: "POST",
        ...jsonBody({ repo_id: null }),
      })
    );
    if (value) setResults(value.results);
  };

  const runChecks = async () => {
    const value = await perform("環境診断中", () => api<any>("/api/diagnostics"));
    if (value) setDiagnostics(value);
  };

  const updateRepo = (repoId: string, patch: Partial<Repository>) => {
    setRepositories((current) =>
      current.map((item) => (item.repo_id === repoId ? { ...item, ...patch } : item))
    );
  };

  const unpublished = useMemo(
    () => repositories.reduce((sum, item) => sum + (item.enabled ? item.unpublished_commits ?? 0 : 0), 0),
    [repositories]
  );

  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="icon-button" onClick={() => setDrawer(true)} aria-label="設定を開く">
          <Menu size={22} />
        </button>
        <div className="brand-mark"><CircleDot size={22} /><span>Myao Rep Patch</span></div>
        {installPrompt && <button className="install-button" onClick={installPwa}><Smartphone size={15}/>アプリとしてインストール</button>}
        <span className={`mode-badge ${settings.mode}`}>{settings.mode === "home" ? "HOME" : "COMPANY"}</span>
      </header>

      <main>
        {error && <div className="banner error"><AlertTriangle size={19} />{error}<button onClick={() => setError("")}><X size={16}/></button></div>}
        {notice && <div className="banner notice"><Check size={19} />{notice}<button onClick={() => setNotice("")}><X size={16}/></button></div>}
        {settings.mode === "home" ? (
          <HomeDashboard
            repositories={repositories}
            unpublished={unpublished}
            onDiscover={discover}
            onRefresh={loadRepositories}
            onPublish={publish}
            onUpdateRepo={updateRepo}
            onSaveRepo={saveRepository}
          />
        ) : (
          <CompanyDashboard
            downloads={downloads}
            selectedZip={selectedZip}
            setSelectedZip={(path) => { setSelectedZip(path); setSummary(null); }}
            summary={summary}
            results={results}
            diagnostics={diagnostics}
            onRefresh={loadDownloads}
            onInspect={inspect}
            onApply={() => applyAll(false)}
            onCorrection={() => applyAll(true)}
            onRetry={retry}
            onCommit={commitAll}
            onDiagnostics={runChecks}
          />
        )}
      </main>

      {drawer && <SettingsDrawer settings={settings} setSettings={setSettings} onSave={saveSettings} onClose={() => setDrawer(false)} />}
      {busy && <div className="busy-overlay"><div className="busy-card"><LoaderCircle className="spin" size={30}/><strong>{busy}</strong><span>この画面を閉じないでください</span></div></div>}
    </div>
  );
}

function HomeDashboard(props: {
  repositories: Repository[];
  unpublished: number;
  onDiscover: () => void;
  onRefresh: () => void;
  onPublish: () => void;
  onUpdateRepo: (id: string, value: Partial<Repository>) => void;
  onSaveRepo: (repo: Repository) => void;
}) {
  return <>
    <section className="hero home-hero">
      <div><p className="eyebrow">PATCH PUBLISHER</p><h1>変更を、確実な<br/>受け渡し単位へ。</h1><p>登録ブランチの差分だけを検出し、検証可能なパッケージとして公開します。</p></div>
      <div className="hero-stat"><span>未公開コミット</span><strong>{props.unpublished}</strong><small>{props.repositories.filter((item) => item.enabled).length} repositories tracked</small></div>
    </section>
    <section className="toolbar">
      <div><h2>リポジトリ</h2><p>初期導入地点と固定ブランチを管理します</p></div>
      <div className="button-row">
        <button className="button ghost" onClick={props.onDiscover}><HardDrive size={17}/>再検出</button>
        <button className="button ghost" onClick={props.onRefresh}><RefreshCw size={17}/>更新</button>
        <button className="button primary" onClick={props.onPublish}><CloudUpload size={17}/>パッチを作成・公開</button>
      </div>
    </section>
    <div className="repo-grid">
      {props.repositories.map((repo) => <article className={`repo-card ${repo.enabled ? "" : "disabled"}`} key={repo.repo_id}>
        <div className="repo-heading">
          <div className={`repo-icon ${repo.kind}`}><GitBranch size={20}/></div>
          <div><h3>{repo.display_name}</h3><p>{repo.path}</p></div>
          <label className="switch"><input type="checkbox" checked={repo.enabled} onChange={(e) => props.onUpdateRepo(repo.repo_id, {enabled: e.target.checked})}/><span/></label>
        </div>
        {repo.error ? <div className="inline-error">{repo.error}</div> : <>
          <div className="repo-metrics"><div><span>公開済み</span><code>{shortHash(repo.published_commit || repo.baseline_commit)}</code></div><div><span>現在</span><code>{shortHash(repo.target_commit)}</code></div><div><span>未公開</span><strong>{repo.unpublished_commits ?? 0}</strong></div></div>
          <label>固定ブランチ<input value={repo.branch} onChange={(e) => props.onUpdateRepo(repo.repo_id, {branch: e.target.value})}/></label>
          <label>会社の初期導入地点<input placeholder="コミットID、タグ、ブランチ" value={repo.baseline_commit} onChange={(e) => props.onUpdateRepo(repo.repo_id, {baseline_commit: e.target.value})}/></label>
          <button className="text-button" onClick={() => props.onSaveRepo(repo)}><Save size={15}/>この設定を保存</button>
        </>}
      </article>)}
      {!props.repositories.length && <EmptyState icon={<Archive/>} title="リポジトリが未登録です" text="設定を保存してから「再検出」を押してください"/>}
    </div>
  </>;
}

function CompanyDashboard(props: {
  downloads: DownloadPackage[];
  selectedZip: string;
  setSelectedZip: (path: string) => void;
  summary: PackageSummary | null;
  results: OperationResult[];
  diagnostics: any;
  onRefresh: () => void;
  onInspect: () => void;
  onApply: () => void;
  onCorrection: () => void;
  onRetry: (id: string) => void;
  onCommit: () => void;
  onDiagnostics: () => void;
}) {
  return <>
    <section className="hero company-hero">
      <div><p className="eyebrow">OFFLINE APPLIER</p><h1>確認してから、<br/>次へ進める。</h1><p>DownloadsのZIPを直接検証し、アプリ単位で安全に適用・復元します。</p></div>
      <div className="shield"><ShieldCheck size={54}/><span>LOCAL ONLY</span><small>127.0.0.1</small></div>
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
          <div className="summary-repos">{props.summary.repositories.map((repo) => <div key={repo.repo_id}><PackageCheck size={18}/><span><strong>{repo.display_name}</strong><small>#{String(repo.first_sequence).padStart(6,"0")} → #{String(repo.last_sequence).padStart(6,"0")} · {formatBytes(repo.total_patch_size)}</small>{repo.mapping_error && <em>{repo.mapping_error}</em>}</span></div>)}</div>
        </> : <EmptyState icon={<ShieldCheck/>} title="まだ検証されていません" text="左のZIPを選び、署名と内容を検証してください"/>}
      </article>
    </section>
    <section className="action-panel">
      <div><p className="step">STEP 3</p><h2>全アプリへ適用</h2><p>前回分を検証・コミットしてから、新しい変更を未コミット状態で適用します。</p></div>
      <div className="button-row wrap"><button className="button ghost" onClick={props.onDiagnostics}><ShieldCheck size={17}/>会社環境診断</button><button className="button ghost" onClick={props.onCommit}><PackageCheck size={17}/>確認済みをコミット</button><button className="button warning" disabled={!props.summary} onClick={props.onCorrection}><RotateCcw size={17}/>修正パッチを重ねる</button><button className="button primary" disabled={!props.summary} onClick={props.onApply}><PackageCheck size={17}/>前回分をコミットして全適用</button></div>
    </section>
    {props.results.length > 0 && <section className="results"><h2>処理結果</h2>{props.results.map((item) => <div className={`result ${item.status}`} key={item.repo_id}><span className="result-icon">{item.status === "failed" ? <X/> : item.status === "unchanged" ? <CircleDot/> : <Check/>}</span><div><strong>{item.display_name}</strong><p>{item.message}</p></div>{item.status === "failed" && <button className="button ghost" onClick={() => props.onRetry(item.repo_id)}>このアプリだけ再実行</button>}</div>)}</section>}
    {props.diagnostics && <section className="results"><h2>会社環境診断</h2>{props.diagnostics.checks.map((item: any) => <div className={`result ${item.status === "ok" ? "applied" : "failed"}`} key={item.name}><span className="result-icon">{item.status === "ok" ? <Check/> : <X/>}</span><div><strong>{item.name}</strong><p>{item.detail}</p></div></div>)}</section>}
  </>;
}

function SettingsDrawer(props: { settings: Settings; setSettings: (value: Settings) => void; onSave: () => void; onClose: () => void }) {
  const s = props.settings;
  const patch = (value: Partial<Settings>) => props.setSettings({ ...s, ...value });
  return <div className="drawer-layer"><button className="drawer-scrim" onClick={props.onClose} aria-label="閉じる"/><aside className="drawer">
    <div className="drawer-header"><div><p className="eyebrow">LOCAL SETTINGS</p><h2><SettingsIcon size={21}/>設定</h2></div><button className="icon-button" onClick={props.onClose}><X/></button></div>
    <div className="settings-body">
      <fieldset><legend>動作モード</legend><div className="segmented"><button className={s.mode === "home" ? "active" : ""} onClick={() => patch({mode:"home"})}>自宅</button><button className={s.mode === "company" ? "active" : ""} onClick={() => patch({mode:"company"})}>会社</button></div></fieldset>
      {s.mode === "home" ? <>
        <Field label="アプリルート" value={s.apps_root} onChange={(apps_root) => patch({apps_root})}/>
        <Field label="Obsidian設定リポジトリ" value={s.obsidian_repo} onChange={(obsidian_repo) => patch({obsidian_repo})}/>
        <Field label="パッチ専用リポジトリ" value={s.patch_repo} onChange={(patch_repo) => patch({patch_repo})}/>
      </> : <>
        <Field label="会社側アプリルート" value={s.company_apps_root} onChange={(company_apps_root) => patch({company_apps_root})}/>
        <Field label="会社側Obsidian設定" value={s.company_obsidian_repo} onChange={(company_obsidian_repo) => patch({company_obsidian_repo})}/>
        <Field label="ZIP検索フォルダ" value={s.download_dir} onChange={(download_dir) => patch({download_dir})}/>
      </>}
      <Field label="パッチ検証パスワード" type="password" placeholder={s.password_configured ? "設定済み（変更時のみ入力）" : "自宅と会社で同じ値"} value={s.patch_password} onChange={(patch_password) => patch({patch_password})}/>
      <div className="field-row"><Field label="ローカルポート" type="number" value={String(s.listen_port)} onChange={(value) => patch({listen_port:Number(value)})}/><Field label="分割サイズ (MiB)" type="number" value={String(s.chunk_size_mib)} onChange={(value) => patch({chunk_size_mib:Number(value)})}/></div>
    </div>
    <div className="drawer-footer"><button className="button primary full" onClick={props.onSave}><Save size={17}/>設定を保存</button></div>
  </aside></div>;
}

function Field(props: { label: string; value: string; onChange: (value: string) => void; type?: string; placeholder?: string }) {
  return <label className="field"><span>{props.label}</span><input type={props.type ?? "text"} value={props.value} placeholder={props.placeholder} onChange={(e) => props.onChange(e.target.value)}/></label>;
}

function EmptyState(props: { icon: React.ReactNode; title: string; text: string }) {
  return <div className="empty-state"><span>{props.icon}</span><strong>{props.title}</strong><p>{props.text}</p></div>;
}
