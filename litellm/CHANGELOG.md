# Changelog

## 0.1.1

- ChatGPT Responses 接受字串與訊息陣列輸入。
- `stream: false` 回傳 JSON；保留 ChatGPT 上游必要的 SSE 傳輸。
- 非串流 Chat Completions 復用上游事件輸出回復，避免完成事件省略 output 時遺失答案。
- 保留限流、provider 錯誤及不完整回應的狀態；截斷串流回安全的 502。
- 建置時檢查兩個上游來源檔雜湊；新增不連外的同步、非同步、串流及錯誤回歸驗證。

## 0.1.0

尚未發佈（M1 核心實作，分支 `feat/m1-core`）。

- LiteLLM **1.104.0**：直接以 `ghcr.io/berriai/litellm-non_root:1.104.0` 為底（與 WOOW PaaS 同一顆映像，amd64 manifest
  `sha256:843a35c7…56a0`），保留 enterprise 程式碼。私人映像，只供 WOOWTECH 自有主機（負責人 2026-10-09 決定）。
- 內建 PostgreSQL 18（公開 Wolfi，18.6-r5）與 nginx，由 s6-overlay v3.2.3.2 管理（13 個 s6-rc 服務）。
- 首次啟動自動產生 master key、salt key、資料庫密碼與後台密碼（存在 `/data/secrets`），版本達標時寫回設定分頁一次。
- 側邊欄（Ingress）直接顯示 LiteLLM 管理介面；取不到側邊欄位址的那次開機改顯示說明頁。
- Woow 外掛（ChatGPT 訂閱、多帳號、後台 Add Credential 子畫面），來源 woow-paas-charts chart 0.3.0
  （main 合併 commit `dcd929e`，#175），逐位元複製。
- 用量紀錄預設保留 30 天、每天 03:30 清理；`/docs` 預設關閉；ChatGPT 訂閱預設開啟。
- 只支援 amd64。
