/**
 * Myao Patch Bridge フロントエンド アプリケーションメイン
 *
 * 仕様:
 * - 画面全体の状態（設定、リポジトリ一覧、選択中ZIP、処理結果など）と、API呼び出しを伴う操作をここに集約する
 * - 表示は動作モードごとのダッシュボードへ委譲する
 *   - 自宅モード: components/HomeDashboard（リポジトリ管理、会社環境検証、パッチ作成・公開、履歴リセット）
 *   - 会社モード: components/CompanyDashboard（ZIP検証、適用・コミット、開始番号設定、環境診断）
 * - 共通: 設定ドロワー、PWAインストールプロンプト、通知・エラーバナー、処理中オーバーレイ
 * - 操作はすべて perform() を通し、処理中表示とエラー表示を統一する
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, CircleDot, LoaderCircle, Menu, Smartphone, X } from "lucide-react";
import { api, jsonBody } from "./api";
import { AddRepositoryModal } from "./components/AddRepositoryModal";
import { CompanyDashboard } from "./components/CompanyDashboard";
import { HomeDashboard } from "./components/HomeDashboard";
import { ResetRepositoryModal } from "./components/ResetRepositoryModal";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { formatSequence } from "./format";
import {
  EDITABLE_SETTING_KEYS,
  type CleanVerificationResult,
  type CompanyRepository,
  type DiagnosticsResult,
  type DownloadPackage,
  type OperationResult,
  type PackageSummary,
  type PublishResult,
  type Repository,
  type RepositoryCreatePayload,
  type ResetResult,
  type SequenceInitResult,
  type Settings,
  type VerifyAllSummary,
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
  github_token: "",
  github_token_configured: false,
  github_repo: "",
  listen_host: "127.0.0.1",
  listen_port: 17345,
  chunk_size_mib: 20,
  company_repo_paths: {},
  company_excluded_repo_ids: [],
  repositories: {},
};

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

/** 公開結果の通知へ列挙する注意の件数（多い場合は件数だけを示す） */
const MAX_LISTED_WARNINGS = 5;

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

/** 設定ドロワーで編集できる項目だけを取り出す（リポジトリ登録など、他の操作が管理する値を送り返さない） */
function editableSettings(settings: Settings): Partial<Settings> {
  return Object.fromEntries(EDITABLE_SETTING_KEYS.map((key) => [key, settings[key]]));
}

export default function App() {
  const [settings, setSettings] = useState<Settings>(emptySettings);
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [drawer, setDrawer] = useState(false);
  const [addModalOpen, setAddModalOpen] = useState(false);
  const [resetModalRepo, setResetModalRepo] = useState<Repository | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [downloads, setDownloads] = useState<DownloadPackage[]>([]);
  const [selectedZip, setSelectedZip] = useState("");
  const [summary, setSummary] = useState<PackageSummary | null>(null);
  const [results, setResults] = useState<OperationResult[]>([]);
  const [companyRepos, setCompanyRepos] = useState<CompanyRepository[]>([]);
  const [diagnostics, setDiagnostics] = useState<DiagnosticsResult | null>(null);
  const [verificationResults, setVerificationResults] = useState<Record<string, CleanVerificationResult>>({});
  const [installPrompt, setInstallPrompt] = useState<BeforeInstallPromptEvent | null>(null);

  const perform = useCallback(async <T,>(label: string, work: () => Promise<T>): Promise<T | undefined> => {
    setBusy(label);
    setError("");
    setNotice("");
    try {
      return await work();
    } catch (reason) {
      setError(errorMessage(reason));
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

  const loadCompanyRepositories = useCallback(async () => {
    const value = await api<{ repositories: CompanyRepository[] }>("/api/company/repositories");
    setCompanyRepos(value.repositories);
  }, []);

  /** 動作モードに応じた一覧を読み込む（片方の失敗でもう片方を止めない） */
  const loadModeData = useCallback(
    async (mode: Settings["mode"]) => {
      if (mode === "home") await loadRepositories().catch(() => undefined);
      else {
        await loadDownloads().catch(() => undefined);
        await loadCompanyRepositories().catch(() => undefined);
      }
    },
    [loadCompanyRepositories, loadDownloads, loadRepositories]
  );

  useEffect(() => {
    perform("初期化中", async () => {
      const value = await loadSettings();
      await loadModeData(value.mode);
    });
  }, [loadModeData, loadSettings, perform]);

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
        ...jsonBody({ values: editableSettings(settings) }),
      })
    );
    if (updated) {
      setSettings(updated);
      setDrawer(false);
      setNotice("設定を保存しました。待受ホスト・ポートの変更は再起動後に反映されます。");
      await loadModeData(updated.mode);
    }
  };

  const closeDrawer = () => {
    setDrawer(false);
    // 保存せずに閉じた場合は、編集途中の値を保存済みの内容へ戻す
    loadSettings().catch(() => undefined);
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
            clean_verify_ignore_patterns: repository.clean_verify_ignore_patterns ?? [],
          },
        }),
      })
    );
    if (value) {
      setNotice(`${repository.display_name}の設定を保存しました`);
      await loadRepositories();
    }
  };

  const addRepository = async (payload: RepositoryCreatePayload): Promise<boolean> => {
    const value = await perform("リポジトリを追加中", () =>
      api<Repository>("/api/repositories", {
        method: "POST",
        ...jsonBody(payload),
      })
    );
    if (value) {
      setNotice(`リポジトリ「${value.display_name}」を追加しました`);
      await loadRepositories();
      setAddModalOpen(false);
      return true;
    }
    return false;
  };

  const deleteRepository = async (repository: Repository) => {
    const ok = window.confirm(
      `リポジトリ「${repository.display_name}」のカードを削除しますか？\n\n※ローカルのファイルやGitリポジトリは削除されず、本ツールの管理対象からのみ除外されます。`
    );
    if (!ok) return;

    const value = await perform(`${repository.display_name}を削除中`, () =>
      api<{ deleted: boolean; repo_id: string }>(`/api/repositories/${repository.repo_id}`, {
        method: "DELETE",
      })
    );
    if (value) {
      setNotice(`リポジトリ「${repository.display_name}」を削除しました`);
      await loadRepositories();
    }
  };

  const resetRepository = async (repository: Repository, newBaseline?: string) => {
    const value = await perform(`${repository.display_name}のパッチ履歴をリセット中`, () =>
      api<ResetResult>(`/api/repositories/${repository.repo_id}/reset`, {
        method: "POST",
        ...jsonBody({ new_baseline_commit: newBaseline }),
      })
    );
    if (value) {
      setNotice(value.message);
      setResetModalRepo(null);
      await loadRepositories();
    }
  };

  const deleteCompanyRepository = async (repository: CompanyRepository) => {
    const ok = window.confirm(
      `リポジトリ「${repository.display_name}」のカードを削除しますか？\n\n※ローカルのファイルやGitリポジトリは削除されず、本ツールの管理対象からのみ除外されます。`
    );
    if (!ok) return;

    const value = await perform(`${repository.display_name}を削除中`, () =>
      api<{ deleted: boolean; repo_id: string }>(`/api/company/repositories/${repository.repo_id}`, {
        method: "DELETE",
      })
    );
    if (value) {
      setNotice(`リポジトリ「${repository.display_name}」のカードを削除しました`);
      await loadCompanyRepositories();
      await loadSettings();
    }
  };

  const toggleAllRepositories = async (enable: boolean) => {
    if (!repositories.length) return;
    const value = await perform(enable ? "全リポジトリを選択中" : "全リポジトリの選択を解除中", async () => {
      await Promise.all(
        repositories.map((repo) =>
          api<Repository>(`/api/repositories/${repo.repo_id}`, {
            method: "PUT",
            ...jsonBody({ values: { enabled: enable } }),
          })
        )
      );
      await loadRepositories();
      return true;
    });
    if (value) {
      setNotice(enable ? "すべてのリポジトリを選択しました" : "すべてのリポジトリの選択を解除しました");
    }
  };

  const verifyRepo = async (repo: Repository) => {
    const value = await perform(`【${repo.display_name}】会社環境シミュレーション検証中`, () =>
      api<CleanVerificationResult>(`/api/repositories/${repo.repo_id}/verify`, { method: "POST" })
    );
    if (value) {
      setVerificationResults((prev) => ({ ...prev, [repo.repo_id]: value }));
      if (value.valid) {
        const caution = value.warnings.length > 0 ? `\n注意が${value.warnings.length}件あります。カードの表示を確認してください` : "";
        setNotice(`【${repo.display_name}】会社環境シミュレーション検証に合格しました（ファイル欠落なし）${caution}`);
      } else {
        setError(`【${repo.display_name}】ファイル欠落・検証エラーが検知されました: ${value.errors.join(" / ")}`);
      }
    }
  };

  const verifyAll = async () => {
    const value = await perform("全リポジトリの会社環境シミュレーション検証中", () =>
      api<VerifyAllSummary>("/api/home/verify-all", { method: "POST" })
    );
    if (value) {
      setVerificationResults(value.results);
      if (value.all_valid) {
        const caution = value.total_warnings > 0 ? `\n注意が${value.total_warnings}件あります。各カードの表示を確認してください` : "";
        setNotice(`すべてのリポジトリが会社側クリーン環境で正常に検証されました（設定ファイル欠落なし）${caution}`);
      } else {
        setError(`会社環境検証で ${value.total_errors} 件のエラーが検知されました。.gitignoreによる設定ファイル漏れ等を確認してください`);
      }
    }
  };

  const publish = async () => {
    const invalidEntries = Object.entries(verificationResults).filter(([, r]) => !r.valid);
    let skipVerification = false;
    if (invalidEntries.length > 0) {
      const ok = confirm(
        `【警告】以下のリポジトリで会社環境シミュレーション検証エラーが検知されています:\n\n` +
          invalidEntries.map(([id, r]) => `・${id}: ${r.errors.join(", ")}`).join("\n") +
          `\n\n会社側でファイル不在エラーが発生する可能性があります。\n検証をスキップして公開を強行しますか？`
      );
      if (!ok) return;
      skipVerification = true;
    } else {
      if (!confirm("変更パッチを作成し、パッチ専用リポジトリへpushしますか？")) return;
    }

    const executePublish = async (skip: boolean) => {
      return await api<PublishResult>("/api/home/publish", {
        method: "POST",
        ...jsonBody({ skip_verification: skip }),
      });
    };

    setBusy("パッチを作成・公開中");
    setError("");
    setNotice("");
    try {
      let res: PublishResult;
      try {
        res = await executePublish(skipVerification);
      } catch (err: unknown) {
        const errMsg = err instanceof Error ? err.message : String(err);
        if (errMsg.includes("会社環境検証") && !skipVerification) {
          const force = confirm(
            `${errMsg}\n\n会社環境シミュレーション検証でエラーが検知されました。\n検証をスキップしてパッチ作成・公開を強行しますか？`
          );
          if (!force) {
            setError(errMsg);
            return;
          }
          res = await executePublish(true);
        } else {
          throw err;
        }
      }
      let msg = res.message;
      if (res.bundle_error) {
        msg += `\n(今回分ZIPを作成できませんでした: ${res.bundle_error})`;
      }
      if (res.release_url) {
        msg += `\nGitHub Releasesへ公開しました: ${res.release_url}`;
      } else if (res.release_error) {
        msg += `\n(GitHub Releases公開警告: ${res.release_error})`;
      }
      if (res.bundle_name && res.bundle_size) {
        const sizeKb = Math.round(res.bundle_size / 1024);
        msg += `\n今回分ZIP: ${res.bundle_name} (${sizeKb} KB)`;
      }
      const warnings = res.warnings ?? [];
      if (warnings.length > 0) {
        msg += `\n注意（${warnings.length}件）:\n` + warnings.slice(0, MAX_LISTED_WARNINGS).map((warning) => `・${warning}`).join("\n");
        if (warnings.length > MAX_LISTED_WARNINGS) {
          msg += `\nほか${warnings.length - MAX_LISTED_WARNINGS}件（各カードの「検証」で確認できます）`;
        }
      }
      setNotice(msg);
      await loadRepositories();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy("");
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

  const applyAll = async () => {
    const message = "未コミットの変更があれば先にコミットし、全アプリへ新しいパッチを適用・コミットしますか？";
    if (!selectedZip || !confirm(message)) return;
    const value = await perform("全アプリを処理中", () =>
      api<{ results: OperationResult[] }>("/api/company/apply-all", {
        method: "POST",
        ...jsonBody({ zip_path: selectedZip }),
      })
    );
    if (value) {
      setResults(value.results);
      await loadCompanyRepositories();
    }
  };

  const retry = async (repoId: string) => {
    const value = await perform("アプリを再実行中", () =>
      api<{ results: OperationResult[] }>("/api/company/retry", {
        method: "POST",
        ...jsonBody({ zip_path: selectedZip, repo_id: repoId }),
      })
    );
    if (value) {
      setResults((previous) => previous.filter((item) => item.repo_id !== repoId).concat(value.results));
      await loadCompanyRepositories();
    }
  };

  const commitAll = async () => {
    if (!confirm("全アプリの未コミットの変更をコミットしますか？")) return;
    const value = await perform("未コミットの変更をコミット中", () =>
      api<{ results: OperationResult[] }>("/api/company/commit-pending", {
        method: "POST",
        ...jsonBody({ repo_id: null }),
      })
    );
    if (value) {
      setResults(value.results);
      await loadCompanyRepositories();
    }
  };

  const commitSingle = async (repoId: string) => {
    const value = await perform("リポジトリをコミット中", () =>
      api<{ results: OperationResult[] }>("/api/company/commit-pending", {
        method: "POST",
        ...jsonBody({ repo_id: repoId }),
      })
    );
    if (value) {
      setResults((previous) => previous.filter((item) => item.repo_id !== repoId).concat(value.results));
      await loadCompanyRepositories();
    }
  };

  const setRepoSequence = async (repoId: string, displayName: string, currentConfirmed: number) => {
    const defaultNext = currentConfirmed + 1;
    const input = prompt(
      `【${displayName}】\n次回適用開始番号（パッチ連番）を指定してください。\n※ 連番 ${defaultNext} を指定すると、直前の ${formatSequence(defaultNext - 1)} まで確定済みとして state.json が生成・保存されます。`,
      String(defaultNext > 1 ? defaultNext : 2)
    );
    if (!input) return;
    const startSeq = parseInt(input, 10);
    if (isNaN(startSeq) || startSeq < 1) {
      alert("1以上の整数を入力してください");
      return;
    }
    const value = await perform("適用開始番号を設定中", () =>
      api<SequenceInitResult>(
        `/api/company/repositories/${repoId}/sequence`,
        {
          method: "POST",
          ...jsonBody({ start_sequence: startSeq }),
        }
      )
    );
    if (value) {
      setNotice(`【${displayName}】次回適用開始番号を ${formatSequence(value.start_sequence)} に設定し、state.json を作成しました`);
      await loadCompanyRepositories();
    }
  };

  const setAllRepoSequence = async () => {
    const input = prompt(
      "全アプリの次回適用開始番号（パッチ連番）を一括指定してください。\n各リポジトリの state.json が直ちに作成・初期化されます。\n例: 2 を指定すると全リポジトリが連番2から適用可能になります。",
      "2"
    );
    if (!input) return;
    const startSeq = parseInt(input, 10);
    if (isNaN(startSeq) || startSeq < 1) {
      alert("1以上の整数を入力してください");
      return;
    }
    const value = await perform("全アプリの適用開始番号を一括設定中", () =>
      api<{ results: SequenceInitResult[] }>("/api/company/repositories/sequence-all", {
        method: "POST",
        ...jsonBody({ start_sequence: startSeq }),
      })
    );
    if (value) {
      setNotice(`全リポジトリの次回適用開始番号を ${formatSequence(startSeq)} に一括設定しました`);
      await loadCompanyRepositories();
    }
  };

  const runChecks = async () => {
    const value = await perform("環境診断中", () => api<DiagnosticsResult>("/api/diagnostics"));
    if (value) setDiagnostics(value);
  };

  const updateRepo = (repoId: string, patch: Partial<Repository>) => {
    setRepositories((current) =>
      current.map((item) => (item.repo_id === repoId ? { ...item, ...patch } : item))
    );
  };

  const openVsCode = async (path: string) => {
    const value = await perform("VS Codeを起動中", () =>
      api<{ status: string; path: string }>("/api/open-vscode", {
        method: "POST",
        ...jsonBody({ path }),
      })
    );
    if (value) {
      setNotice("VS Codeで開きました");
    }
  };

  const openAntigravity = async (path: string) => {
    const value = await perform("Antigravityを起動中", () =>
      api<{ status: string; path: string }>("/api/open-antigravity", {
        method: "POST",
        ...jsonBody({ path }),
      })
    );
    if (value) {
      setNotice("Antigravityで開きました");
    }
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
        <div className="brand-mark"><CircleDot size={22} /><span>Myao Patch Bridge</span></div>
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
            verificationResults={verificationResults}
            onDiscover={discover}
            onRefresh={loadRepositories}
            onVerifyAll={verifyAll}
            onVerifyRepo={verifyRepo}
            onPublish={publish}
            onUpdateRepo={updateRepo}
            onSaveRepo={saveRepository}
            onDeleteRepo={deleteRepository}
            onResetRepo={setResetModalRepo}
            onOpenAddModal={() => setAddModalOpen(true)}
            onToggleAll={toggleAllRepositories}
            onOpenAntigravity={openAntigravity}
          />
        ) : (
          <CompanyDashboard
            downloads={downloads}
            selectedZip={selectedZip}
            setSelectedZip={(path) => { setSelectedZip(path); setSummary(null); }}
            summary={summary}
            results={results}
            companyRepos={companyRepos}
            diagnostics={diagnostics}
            onRefresh={loadDownloads}
            onRefreshRepos={loadCompanyRepositories}
            onInspect={inspect}
            onApply={applyAll}
            onRetry={retry}
            onCommit={commitAll}
            onCommitSingle={commitSingle}
            onSetSequence={setRepoSequence}
            onSetAllRepoSequence={setAllRepoSequence}
            onDeleteRepo={deleteCompanyRepository}
            onDiagnostics={runChecks}
            onOpenVsCode={openVsCode}
            listenHost={settings.effective_listen_host ?? settings.listen_host}
          />
        )}
      </main>

      {addModalOpen && (
        <AddRepositoryModal
          onClose={() => setAddModalOpen(false)}
          onAdd={addRepository}
        />
      )}

      {resetModalRepo && (
        <ResetRepositoryModal
          repo={resetModalRepo}
          onClose={() => setResetModalRepo(null)}
          onReset={resetRepository}
        />
      )}

      {drawer && <SettingsDrawer settings={settings} setSettings={setSettings} onSave={saveSettings} onClose={closeDrawer} />}
      {busy && <div className="busy-overlay"><div className="busy-card"><LoaderCircle className="spin" size={30}/><strong>{busy}</strong><span>この画面を閉じないでください</span></div></div>}
    </div>
  );
}
