/**
 * 複数の画面で使う小さな共通部品
 *
 * 仕様:
 * - Field: ラベル付きの1行入力
 * - EmptyState: 一覧が空のときの案内表示
 */
import type { ReactNode } from "react";

export function Field(props: { label: string; value: string; onChange: (value: string) => void; type?: string; placeholder?: string }) {
  return <label className="field"><span>{props.label}</span><input type={props.type ?? "text"} value={props.value} placeholder={props.placeholder} onChange={(e) => props.onChange(e.target.value)}/></label>;
}

export function EmptyState(props: { icon: ReactNode; title: string; text: string }) {
  return <div className="empty-state"><span>{props.icon}</span><strong>{props.title}</strong><p>{props.text}</p></div>;
}
