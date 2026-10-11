/**
 * Myao Patch Bridge UI 共通型定義
 * 
 * 仕様:
 * - Mode: 自宅（home）または会社（company）の動作モード
 * - Repository: リポジトリの各種設定・Git状態（未公開コミット数等）
 * - RepositoryCreatePayload: リポジトリ手動追加リクエスト
 * - Settings: アプリケーション全体設定
 * - PackageSummary / DownloadPackage / OperationResult: パッチ送受信・適用関連型定義
 * - CompanyRepository / SequenceInitResult: 会社側リポジトリ個別状態および連番初期化型定義
 * - ResetResult: リポジトリパッチ履歴リセット結果型定義
 * - PublishResult / PublishedPackage: パッチ公開結果型定義
 * - CleanVerificationResult / VerifyAllSummary: 会社環境シミュレーション検証結果型定義
 * - DiagnosticsResult: 環境診断結果型定義
 */

export type Mode = "home" | "company";

export interface Repository {
  repo_id: string;
  display_name: string;
  path: string;
  kind: "app" | "obsidian";
  enabled: boolean;
  branch: string;
  baseline_commit: string;
  published_commit: string;
  target_commit?: string;
  clean?: boolean;
  changes?: number;
  unpublished_commits?: number;
  error?: string;
  clean_verify_ignore_patterns?: string[];
}

export interface RepositoryCreatePayload {
  path: string;
  display_name?: string;
  kind?: "app" | "obsidian";
  branch?: string;
  baseline_commit?: string;
  enabled?: boolean;
  clean_verify_ignore_patterns?: string[];
}

export interface Settings {
  mode: Mode;
  apps_root: string;
  obsidian_repo: string;
  patch_repo: string;
  company_apps_root: string;
  company_obsidian_repo: string;
  download_dir: string;
  patch_password: string;
  password_configured: boolean;
  github_token?: string;
  github_token_configured?: boolean;
  github_repo?: string;
  listen_host: string;
  listen_port: number;
  chunk_size_mib: number;
  company_repo_paths: Record<string, string>;
  company_excluded_repo_ids?: string[];
  repositories: Record<string, Repository>;
  /** 起動引数（--host / --allow-vpn / --port）を反映した実際の待受設定。表示専用で保存対象ではない */
  effective_listen_host?: string;
  effective_listen_port?: number;
}

/** 設定ドロワーから保存する項目。リポジトリ登録（repositories）は専用APIで管理するため含めない */
export const EDITABLE_SETTING_KEYS = [
  "mode",
  "apps_root",
  "obsidian_repo",
  "patch_repo",
  "company_apps_root",
  "company_obsidian_repo",
  "download_dir",
  "patch_password",
  "github_token",
  "github_repo",
  "listen_host",
  "listen_port",
  "chunk_size_mib",
  "company_excluded_repo_ids",
] as const satisfies readonly (keyof Settings)[];

export interface DownloadPackage {
  path: string;
  name: string;
  size: number;
  modified_at: string;
}

export interface PackageSummary {
  path: string;
  package_count: number;
  repository_count: number;
  repositories: Array<{
    repo_id: string;
    display_name: string;
    kind: string;
    first_sequence: number;
    last_sequence: number;
    total_patch_size: number;
    company_path: string;
    mapping_error?: string;
  }>;
}

export interface OperationResult {
  repo_id: string;
  display_name: string;
  status: "applied" | "failed" | "unchanged" | "committed";
  message: string;
  confirmed_sequence?: number;
}

export interface CompanyRepository {
  repo_id: string;
  display_name: string;
  path: string;
  kind: "app" | "obsidian";
  clean: boolean;
  changes: number;
  head: string;
  branch: string;
  confirmed_sequence: number;
  error?: string;
}

export interface SequenceInitResult {
  repo_id: string;
  display_name: string;
  start_sequence: number;
  confirmed_sequence: number;
}

export interface ResetResult {
  reset: boolean;
  repo_id: string;
  display_name: string;
  message: string;
  repository: Repository;
}

export interface PublishedPackage {
  repo_id: string;
  display_name: string;
  sequence: number;
  source_from_commit: string;
  source_to_commit: string;
}

export interface PublishResult {
  published: boolean;
  message: string;
  packages: PublishedPackage[];
  bundle_path?: string;
  bundle_name?: string;
  bundle_size?: number;
  bundle_error?: string;
  release_url?: string;
  download_url?: string;
  release_error?: string;
}

export interface CleanVerificationResult {
  valid: boolean;
  repo_id: string;
  branch: string;
  target_commit: string;
  errors: string[];
  warnings: string[];
  checked_files: string[];
}

export interface VerifyAllSummary {
  all_valid: boolean;
  total_errors: number;
  total_warnings: number;
  results: Record<string, CleanVerificationResult>;
}

export interface DiagnosticsResult {
  python: string;
  platform: string;
  passed: boolean;
  checks: Array<{ name: string; status: "ok" | "failed"; detail: string }>;
}
