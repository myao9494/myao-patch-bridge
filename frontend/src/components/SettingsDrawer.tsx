/**
 * 設定ドロワー（共通）
 *
 * 仕様:
 * - 動作モード（自宅 / 会社）の切り替えと、モードごとのパス設定
 * - 会社モード: 除外中のリポジトリカードの解除
 * - パッチ検証パスワード、GitHubトークン（設定済みの値は表示せず、変更時のみ入力）
 * - 待受ホスト・ポート、分割サイズ
 * - ここで編集した値は「設定を保存」を押すまでサーバーへ送られない
 */
import { Save, Settings as SettingsIcon, X } from "lucide-react";
import type { Settings } from "../types";
import { Field } from "./common";

export function SettingsDrawer(props: { settings: Settings; setSettings: (value: Settings) => void; onSave: () => void; onClose: () => void }) {
  const s = props.settings;
  const patch = (value: Partial<Settings>) => props.setSettings({ ...s, ...value });
  const overridden =
    (s.effective_listen_host !== undefined && s.effective_listen_host !== s.listen_host) ||
    (s.effective_listen_port !== undefined && s.effective_listen_port !== s.listen_port);
  return <div className="drawer-layer"><button className="drawer-scrim" onClick={props.onClose} aria-label="閉じる"/><aside className="drawer">
    <div className="drawer-header"><div><p className="eyebrow">LOCAL SETTINGS</p><h2><SettingsIcon size={21}/>設定</h2></div><button className="icon-button" onClick={props.onClose}><X/></button></div>
    <div className="settings-body">
      <fieldset><legend>動作モード</legend><div className="segmented"><button className={s.mode === "home" ? "active" : ""} onClick={() => patch({mode:"home"})}>自宅</button><button className={s.mode === "company" ? "active" : ""} onClick={() => patch({mode:"company"})}>会社</button></div></fieldset>
      {s.mode === "home" ? <>
        <Field label="アプリルート" value={s.apps_root} onChange={(apps_root) => patch({apps_root})}/>
        <Field label="Obsidian設定リポジトリ" value={s.obsidian_repo} onChange={(obsidian_repo) => patch({obsidian_repo})}/>
        <Field label="パッチ専用リポジトリ" value={s.patch_repo} onChange={(patch_repo) => patch({patch_repo})}/>
        <Field label="GitHub トークン (Releases自動公開用)" type="password" placeholder={s.github_token_configured ? "設定済み（変更時のみ入力）" : "ghp_... (任意)"} value={s.github_token || ""} onChange={(github_token) => patch({github_token})}/>
        <Field label="GitHub リポジトリ名 (owner/repo)" placeholder="未入力時はoriginから自動取得" value={s.github_repo || ""} onChange={(github_repo) => patch({github_repo})}/>
      </> : <>
        <Field label="会社側アプリルート" value={s.company_apps_root} onChange={(company_apps_root) => patch({company_apps_root})}/>
        <Field label="会社側Obsidian設定" value={s.company_obsidian_repo} onChange={(company_obsidian_repo) => patch({company_obsidian_repo})}/>
        <Field label="ZIP検索フォルダ" value={s.download_dir} onChange={(download_dir) => patch({download_dir})}/>
        {s.company_excluded_repo_ids && s.company_excluded_repo_ids.length > 0 && (
          <div className="field">
            <span>除外中のリポジトリカード ({s.company_excluded_repo_ids.length}件)</span>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "6px" }}>
              {s.company_excluded_repo_ids.map((id) => (
                <span key={id} className="badge warning" style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}>
                  {id}
                  <button
                    type="button"
                    style={{ border: 0, background: "transparent", cursor: "pointer", padding: "0 2px", color: "inherit", fontWeight: "bold" }}
                    title="除外を解除してカードを再表示"
                    onClick={() => {
                      const next = s.company_excluded_repo_ids?.filter((x) => x !== id) || [];
                      patch({ company_excluded_repo_ids: next });
                    }}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
            <button
              type="button"
              className="text-action-button"
              style={{ marginTop: "6px" }}
              onClick={() => patch({ company_excluded_repo_ids: [] })}
            >
              すべての除外を解除
            </button>
          </div>
        )}
      </>}
      <Field label="パッチ検証パスワード" type="password" placeholder={s.password_configured ? "設定済み（変更時のみ入力）" : "自宅と会社で同じ値"} value={s.patch_password} onChange={(patch_password) => patch({patch_password})}/>
      <div className="field-row">
        <Field label="待受ホスト (127.0.0.1: ローカルのみ / 0.0.0.0: VPN・LAN許可)" value={s.listen_host} placeholder="127.0.0.1 または 0.0.0.0" onChange={(listen_host) => patch({listen_host})}/>
        <Field label="ローカルポート" type="number" value={String(s.listen_port)} onChange={(value) => patch({listen_port:Number(value)})}/>
      </div>
      {overridden && (
        <small className="field-hint" style={{ marginTop: "-10px", marginBottom: "18px" }}>
          起動引数の指定により、現在は {s.effective_listen_host}:{s.effective_listen_port} で待ち受けています（この指定は保存されません）
        </small>
      )}
      <Field label="分割サイズ (MiB)" type="number" value={String(s.chunk_size_mib)} onChange={(value) => patch({chunk_size_mib:Number(value)})}/>
    </div>
    <div className="drawer-footer"><button className="button primary full" onClick={props.onSave}><Save size={17}/>設定を保存</button></div>
  </aside></div>;
}
