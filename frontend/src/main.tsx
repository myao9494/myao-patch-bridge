/**
 * React アプリケーションのエントリポイント
 * 
 * 仕様:
 * - ServiceWorker の登録（本番ビルド時）
 * - ルートコンポーネント App のレンダリング
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js"));
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
