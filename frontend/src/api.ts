/**
 * バックエンドREST API通信クライアント
 * 
 * 仕様:
 * - セッショントークンの自動取得と X-Rep-Patch-Token ヘッダー付与
 * - エラーハンドリングおよびJSONリクエスト/レスポンス処理
 */
let token = "";

async function sessionToken(): Promise<string> {
  if (token) return token;
  const response = await fetch("/api/session", { cache: "no-store" });
  if (!response.ok) throw new Error("セッションを開始できません");
  token = (await response.json()).token;
  return token;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = options.method?.toUpperCase() ?? "GET";
  const headers = new Headers(options.headers);
  if (method !== "GET" && method !== "HEAD") {
    headers.set("X-Rep-Patch-Token", await sessionToken());
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || "処理に失敗しました");
  }
  return response.json();
}

export function jsonBody(value: unknown): Pick<RequestInit, "body"> {
  return { body: JSON.stringify(value) };
}
