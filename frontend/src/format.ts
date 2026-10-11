/**
 * 表示用の整形ユーティリティ
 *
 * 仕様:
 * - formatBytes: バイト数を B / KiB / MiB / GiB で表示
 * - shortHash: コミットIDを先頭8桁で表示（未設定時は「未設定」）
 * - formatSequence: パッチ連番を #000001 形式で表示
 * - parsePatterns / joinPatterns: カンマ区切りの除外パターン入力と配列の相互変換
 */
export function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(1)} GiB`;
}

export function shortHash(value?: string): string {
  return value ? value.slice(0, 8) : "未設定";
}

export function formatSequence(value: number): string {
  return `#${String(value).padStart(6, "0")}`;
}

export function parsePatterns(text: string): string[] {
  return text
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function joinPatterns(patterns?: string[]): string {
  return (patterns ?? []).join(", ");
}
