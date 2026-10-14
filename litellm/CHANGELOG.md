# Changelog

## 0.1.9

2026-10-14

本版是獨立的安全修補，與 PaaS 1158 及前一版的 M4 API 錯誤契約發版無關。

### 安全修正

- 上游供應商（OpenRouter）的錯誤不再把閘道本身的帳號資訊轉給呼叫端：錯誤回應、串流中途的錯誤片段與用量紀錄（spend log，內部使用者看得到自己的紀錄）都不再含 `user_id`、`error.metadata`（上游原文、供應商名稱、BYOK 旗標、被標記的輸入）、帳號／組織／專案／請求／生成 ID、金鑰片段與 OpenRouter 網址。`openrouter/` 路由與以 `openai/` 指向 OpenRouter 的路由（chat、`/v1/completions`、`/v1/embeddings`，含串流）都適用；其他供應商的錯誤文字不變。HTTP 狀態碼與錯誤的 `type`／`code`／`param` 不變，只改訊息文字；原文只留在伺服器記錄。
- 上游錯誤原文只寫進伺服器記錄：每個被清洗的錯誤（含串流中途的錯誤片段）一筆 ERROR，所以 `log_level: error`（PaaS 的 `LITELLM_LOG=ERROR`）也會保留；先遮蔽金鑰（`sk-…`、Bearer、JWT、`api_key=` 等）再截到 2000 字，不會進回應、用量紀錄或回應標頭。`WOOW_PROVIDER_ERROR_LOG=off` 可不記（見下方退回開關）。
- 上游回 401／402／403／408／429／504 時改用固定說明文字，例如上游憑證被拒時會說明「呼叫端的 API 金鑰有效，請聯絡管理員」；內容審查（403）只列出原因，不帶被標記的輸入。400／404／413／422 與其他 5xx 保留上游訊息本身，400 類若上游只回 `Provider returned error`，會附上清洗並截斷後的具體原因。
- 供應商的回應標頭只轉送 `llm_provider-x-ratelimit-*` 與 `llm_provider-retry-after`；`cf-ray`、`x-request-id` 等其他標頭不再轉給呼叫端（管理端用量紀錄仍保留上游 request id）。
- 非管理員金鑰呼叫 `/health` 時，端點結果不再帶 `raw_request_typed_dict`（內含內部 api_base）與 stack trace；`error` 只留清洗過的第一行，並遮掉該部署的 api_base 與主機名。
- OpenRouter 串流中途錯誤的代碼是字串（如 `server_error`）時，不再出現 `invalid literal for int()`，改回 500；`rate_limit*` 回 429。

### 修正

- 生成上限欄位必須是 1 到 2147483647 的整數：chat 的 `max_tokens`／`max_completion_tokens`、`/v1/completions` 與 `/v1/messages` 的 `max_tokens`、Responses 的 `max_output_tokens`。`-1`、`0`、`1.5`、`"10"`、`true` 等在呼叫上游前就回 400（OpenAI 格式，`param` 為欄位名；`/v1/messages` 為 Anthropic 格式），不計費也不寫用量紀錄。`16.0` 這類整數值照常接受；Responses 的 1–15 仍照上游提高為 16。
- 有預算上限的金鑰（行為變更）：推理模型（`model_info.supports_reasoning: true`）至少預留 2048 個輸出 token 與 256 個輸入 token，因為上游的隱藏推理不受呼叫端 `max_tokens` 限制。剩餘預算連「輸入＋推理預留」都付不起時，在呼叫上游前就拒絕；付得起這一段、但付不起完整預估（例如沒帶 `max_tokens` 時以 16384 token 估算，或 Claude Code 的 `max_tokens: 32000`）時，照上游做法把預留縮到剩餘額度後放行，所以小額度的尾段仍可使用。非推理模型（以及明確關閉推理的請求）只在剩餘預算付不起輸入加 1 個輸出 token 時拒絕，其餘照上游縮小預留。請求明確關閉推理（`reasoning.enabled: false`、`reasoning.effort`／`reasoning_effort` 為 `none`、`reasoning.max_tokens: 0`、`/v1/messages` 的 `thinking.type: disabled`）時不加推理預留；負數、0 或布林值的 `max_tokens` 不再讓預留變小。
- 預算超出的錯誤加上 `x-should-retry: false` 標頭，訊息不再回顯金鑰雜湊或實體 ID；因預估費用被拒時，訊息寫明「預估費用超過剩餘預算」與剩餘額度（例：`Budget has been exceeded! Key budget. Current cost: 0.0, Estimated request cost: 0.00544 exceeds the remaining budget: 0.0001, Max budget: 0.0001`）。
- 預算超出的 HTTP 狀態碼由 422 改為 429（錯誤 `type` 仍是 `budget_exceeded`），由 LiteLLM 設定 `litellm_settings.budget_exceeded_status_code: 429` 控制，可單獨退回。
- OpenRouter 路由的 `reasoning_effort` 對所有模型都會轉送；之前不在 LiteLLM 費用表裡的新模型，在 `drop_params: true` 下會被靜默丟掉。
- 有預算上限的金鑰使用未設定單價的模型時，伺服器記錄 `event=unpriced_budgeted_request` 警告（預設照常放行）。

### 退回開關（在附加元件設定的 `env_vars` 設定，不需重建映像；PaaS 用 chart 的 `extraEnv`）

`WOOW_` 開頭的其他名稱仍是保留字；下列開關的值不合法時，附加元件會拒絕啟動並指出欄位。

- `WOOW_PROVIDER_HEADERS=all`：恢復轉送所有供應商回應標頭。
- `WOOW_PROVIDER_ERROR_LOG=raw`（預設，伺服器記錄保留遮蔽後的上游原文）｜`off`（不記原文）。
- `WOOW_TOKEN_LIMIT_VALIDATION=enforce`（預設）｜`warn`（照舊轉送並記警告）｜`off`（與前一版相同）。
- `WOOW_BUDGET_ADMISSION=strict`（預設，見上方「輸入＋推理預留」的准入）｜`full`（完整預估超過剩餘預算就拒絕，沒帶 `max_tokens` 以 16384 token 估算，小額度的尾段會用不到）｜`resize`（恢復上游：只在剩餘預算為 0 時拒絕）。
- `WOOW_BUDGET_REASONING_RESERVE_TOKENS`（預設 2048）與 `WOOW_BUDGET_MIN_INPUT_TOKENS`（預設 256）；也可在模型的 `model_info.woow_reasoning_output_reserve_tokens`／`model_info.woow_min_input_tokens` 逐模型設定。
- `WOOW_BUDGET_UNPRICED=allow`（預設）｜`block_budgeted_keys`（有預算的金鑰不得使用未定價模型，回 400）。

### 建議的模型設定

- OpenRouter 推理模型的路由：用頂層 `litellm_params.reasoning: {"enabled": false}` 預設關閉推理（不要放在 `extra_body`，否則呼叫端無法覆寫），並設 `model_info.supports_reasoning: true` 與單價；需要推理的另開 `<模型>:thinking` 路由。
- OpenRouter 路由一律填上單價；小預算的金鑰設 `max_parallel_requests: 1`。單一 worker 下預算保證是「每筆最多超出約推理預留的費用」；多 worker 或多副本前須先接 Redis。

## 0.1.8

2026-10-14

### 修正

- Woow 外掛同步到 PaaS chart 0.3.3：ChatGPT 模型的上下文長度改以帳號清單為準（例：`chatgpt/gpt-5.5` 由 1050000 改為 272000），手動設定的值不受影響。
- 請求帶 `mock_response`／`mock_tool_calls` 時回 400，不再悄悄移除後照樣送到模型供應商（金鑰或團隊 metadata 設 `allow_client_mock_response: true` 時照舊允許）。
- 在請求驗證階段就被拒絕的請求（缺必要參數、JSON 無法解析、mock 參數、tool_call_id 對不上）不再寫入用量紀錄。
- 對話串流在結束片段之後不再多送一個空的 choices 片段；用量照常計算與回傳。
- `role: tool` 訊息的 `tool_call_id` 必須對應前面 assistant 訊息的 `tool_calls`，否則回 400。
- OpenAI 相容路由的 404／405／422／500 錯誤一律回 `{"error": {...}}`，`code` 等於 HTTP 狀態碼；`/v1/completions` 缺 `prompt` 回 400；未處理的內部錯誤只回通用訊息，不外露內部內容。管理 API、後台、`/v1/messages` 與 Woow 外掛頁面的錯誤格式不變。

## 0.1.7

2026-10-14

### 修正

- Woow 外掛同步到 PaaS chart 0.3.2（woow-paas-charts main `82388ef`），與 PaaS 同版：
  - 模型資訊（`/model/info`）不再因 Router 先建出只有 responses 的空項目而缺少上下文長度、價格等欄位；明確設定的值不會被覆寫。
  - ChatGPT 帳號綁定與登入在部分失敗時保留，不會因單一帳號出錯而遺失其他帳號；啟動時以較新的 token 為準，不會回滾已輪換的 token。
  - 手機版 ChatGPT 頁面分頁恢復正常。
- 原有「資料庫寫入失敗時回報錯誤並保留本機登入」修補改套在新版外掛上，行為不變。

## 0.1.6

2026-10-14

### 修正

- 修正未登入即可讀取 `/routes` 路由清單的問題，改由既有 API 金鑰驗證與授權流程保護；健康檢查端點維持原有行為。
- 建置時驗證修補前後來源雜湊；CI 與發布流程新增實際映像的路由驗證回歸及修補器拒絕異常輸入檢查。

## 0.1.5

2026-10-14

### 修正

- 修正登入失敗達封鎖門檻的當次回應：同來源第 11 次、同帳號與來源第 6 次即回 429，保留 Retry-After 與固定封鎖期限。
- 登入封鎖安全警告使用獨立 WARNING 輸出，遮罩使用者與來源；不提升其他診斷日誌等級。
- CI 與發布流程新增實際映像內登入節流合約檢查。

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
