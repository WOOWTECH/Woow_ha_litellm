# Changelog

## 0.1.4

2026-10-13

- 修正 ChatGPT 帳號復原時，資料庫寫入失敗卻回報成功的問題；現在回報 `db_save_failed`，CLI 以非零狀態結束，既有啟動流程記錄警告後繼續。
- 資料庫寫入失敗時保留最新本機登入資料，繼續處理其他帳號；權限修復後可重新執行復原。錯誤訊息不包含 token 或資料庫細節。

## 0.1.3

2026-10-13

- 修正停用 ChatGPT Subscription 時，資料庫既有訂閱模型仍觸發登入、造成服務無法就緒的問題；保留模型與帳號資料，重新啟用後恢復載入。
- 修正含斜線／冒號的模型 ID 路由、串流最後 usage 區塊，以及零成本回應標頭，保留既有權限檢查。
- 金鑰／登入與已驗證回應禁止快取；HTML 補上 object-src、base-uri 與 Referrer-Policy 預設值，保留既有 CSP 指令。
- 更新說明採用 Home Assistant 可正確截取版本區間的標題格式；建置及發布增加實際映像 API 與安全標頭檢查。

## 0.1.2

2026-10-13

### 修正

- 從已觀察到的 SSE 事件回復合法輸出；遇到尚未完整的工具呼叫時拒絕部分成功，保留真正空回應及 provider 錯誤。

- 資料庫查詢異常時禁止略過備份；中斷 dump 清理暫存檔。
- 長時間啟動移出 s6-rc 啟動鎖；停止與逾時會終止整組啟動子程序，索引改為獨立監督服務。
- 健康檢查涵蓋 nginx；移除 Ingress 下無效的 Web UI URL 與重複預設值。
- 啟動摘要不印 env 值；拒絕無法原樣傳遞的值與繞過設定的保留變數。migration 只取得資料庫憑證。
- 補齊商店圖示、巢狀翻譯、CI app linter 與備份／節流／求助文件；PostgreSQL 不再印例行 checkpoint。

## 0.1.1

2026-10-13

### 修正

- ChatGPT Responses 接受字串與訊息陣列輸入。
- `stream: false` 回傳 JSON；保留 ChatGPT 上游必要的 SSE 傳輸。
- 非串流 Chat Completions 復用上游事件輸出回復，避免完成事件省略 output 時遺失答案。
- 保留限流、provider 錯誤及不完整回應的狀態；截斷串流回安全的 502。
- 建置時檢查兩個上游來源檔雜湊；新增不連外的同步、非同步、串流及錯誤回歸驗證。

## 0.1.0

2026-10-09

### 新增

- LiteLLM **1.104.0**：直接以 `ghcr.io/berriai/litellm-non_root:1.104.0` 為底（與 WOOW PaaS 同一顆映像，amd64 manifest
  `sha256:843a35c7…56a0`），保留 enterprise 程式碼。私人映像，只供 WOOWTECH 自有主機（負責人 2026-10-09 決定）。
- 內建 PostgreSQL 18（公開 Wolfi，18.6-r5）與 nginx，由 s6-overlay v3.2.3.2 管理（13 個 s6-rc 服務）。
- 首次啟動自動產生 master key、salt key、資料庫密碼與後台密碼（存在 `/data/secrets`），版本達標時寫回設定分頁一次。
- 側邊欄（Ingress）直接顯示 LiteLLM 管理介面；取不到側邊欄位址的那次開機改顯示說明頁。
- Woow 外掛（ChatGPT 訂閱、多帳號、後台 Add Credential 子畫面），來源 woow-paas-charts chart 0.3.0
  （main 合併 commit `dcd929e`，#175），逐位元複製。
- 用量紀錄預設保留 30 天、每天 03:30 清理；`/docs` 預設關閉；ChatGPT 訂閱預設開啟。
- 只支援 amd64。
