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
  listen_host: string;
  listen_port: number;
  chunk_size_mib: number;
  company_repo_paths: Record<string, string>;
  repositories: Record<string, Repository>;
}

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
  pending_sequences?: number[];
}
