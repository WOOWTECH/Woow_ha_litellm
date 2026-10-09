# Woow LiteLLM

> **測試版（experimental）**：尚未正式發佈，設定與資料格式在 0.1.x 期間仍可能調整。

本 add-on 由 WOOWTECH 打包，不是 BerriAI 的官方產品；LiteLLM 是 BerriAI 的專案名稱。

把 [LiteLLM](https://github.com/BerriAI/litellm)（OpenAI 相容的 AI 閘道）連同自己的 PostgreSQL 裝在 Home Assistant 上。
LiteLLM 版本與 Woow PaaS 相同（1.104.0），一起升版；也帶同一套 Woow 外掛（ChatGPT 訂閱、多帳號）。

## 系統需求

- Home Assistant **2026.5.0 以上**（較舊的 Core 會把 add-on 的設定值給一般使用者看，所以不允許安裝）。
- 只支援 **amd64**。
- 記憶體：LiteLLM 加資料庫閒置時約 0.7–1 GB；建議主機有 4 GB 以上，2 GB 的主機不建議。
- 第一次下載映像約 500 MB。

## 第一次啟動

不必填任何設定，直接啟動：

1. 自動產生 master key、salt key、資料庫密碼、後台密碼，存在 `/data/secrets`。
2. 建立資料庫並套用 LiteLLM 的全部 migration（第一次約 2–5 分鐘，期間側邊欄顯示「LiteLLM 啟動中」）。
3. 啟動 LiteLLM。

**後台帳號**預設 `admin`；**後台密碼**與 **master key** 在 Home Assistant 2026.5.0、Supervisor 2026.07.0
以上時，會自動寫回 add-on 的「設定」分頁（只寫一次）。只有 HA 管理員看得到設定分頁。

- 寫回後想讓設定裡不留秘密：抄下來之後清空欄位即可，之後不會再寫回；值仍保存在 `/data/secrets`。
- 要換 master key 或後台密碼：在設定填新的值並重新啟動。所有後台登入會被登出。
- **salt key 永遠不會顯示、也不能更換**：它加密了你存在 LiteLLM 裡的所有 provider 金鑰。遺失就只能從備份還原。

## 側邊欄

側邊欄的「LiteLLM」直接開啟 LiteLLM 管理介面，用上面的後台帳號密碼登入（登入有效 4 小時，可在設定調整）。

- 某次開機若在 60 秒內取不到側邊欄位址，那次會改顯示說明頁；**重新啟動 add-on** 即可恢復。
- 安全提醒：側邊欄與 HA 前端同源，HA 前端上的其他腳本（自訂卡片、其他 add-on 的頁面）理論上讀得到後台登入。
  請只安裝可信任的自訂元件與 add-on；需要長時間操作時改用區網或 Cloudflare Tunnel 開啟。

## 給同一台 HA 的其他 add-on

內部位址：`http://<add-on 主機名>:4000`（例如從 Woow 商店安裝時是 `http://1b7b4ce7-woow-litellm:4000`），
OpenAI 相容端點在 `/v1`。**請在 LiteLLM 為每個用途建立虛擬 key**（可限制模型、預算、效期），不要把 master key 給其他 add-on。

## 區網與對外

- 4000 埠預設不對區網開放。要開時在 add-on 的「網路」設定填主機埠，再開 `http://<HA 主機>:<埠>/`，
  會自動轉到管理介面。**直接開 `/ui` 會是 404，這是預期行為**（介面只在帶側邊欄前綴的路徑下運作）。
- 區網連線是 HTTP 明文，金鑰可能被竊聽；要對外請用 Cloudflare Tunnel。
- add-on 沒有出站限制：LiteLLM 管理員可以把模型的 `api_base` 指向區網或 HA 內部的服務。

## ChatGPT 訂閱（Woow 外掛）

設定 `chatgpt_subscription` 預設開啟。在管理介面「LLM Credentials → Add Credential」選「ChatGPT Subscription」，
或打開 `/woow/chatgpt`，用 ChatGPT Plus／Pro 帳號登入後就能把 ChatGPT 的模型加進 LiteLLM。可以連多個帳號。

- 登入資料放在記憶體（tmpfs），資料庫另存一份，重新啟動不會登出。
- 同一個 ChatGPT 帳號可以在 Woow PaaS 與這裡各自登入；但**不要把這裡的備份還原到第二台主機繼續用**，
  兩邊會共用同一個登入而互相登出。
- 關掉 `chatgpt_subscription` 不會刪除已連線的帳號；要刪請先在 ChatGPT 訂閱頁中斷連線。
- 已知小問題：從區網不帶前綴直接開 `/woow/chatgpt` 時，「回 LiteLLM 後台」連結會 404；從後台進入則正常。

## 備份與還原

- 備份方式是冷備份：HA 備份時會先停止 add-on（約 1–2 分鐘），再打包 `/data`。每天的自動備份都會造成這段停機。
- 備份內含 master key、salt key、資料庫與 ChatGPT 訂閱的登入：**請使用 HA 的加密備份**。
- 還原後每次開機都會自動修正檔案擁有者與權限。
- LiteLLM 換版前會自動 `pg_dump` 到 `/data/pre-upgrade-dumps`（保留 2 份，不進 HA 備份）。dump 失敗就不會升級資料庫。
- 不支援只降映像版本（資料庫 migration 只能往前）；要回退請還原 HA 備份。
- **解除安裝會刪除所有資料（金鑰與資料庫），無法復原。**

## 設定

| 選項 | 預設 | 說明 |
|---|---|---|
| `master_key` | 空白 | 空白＝沿用已存的值；填 `sk-…` 換新的 |
| `ui_username` | `admin` | 後台帳號 |
| `ui_password` | 空白 | 空白＝沿用已存的值；至少 12 字 |
| `ui_session_duration` | `4h` | 後台登入有效時間，1h–24h |
| `log_level` | `warning` | `error` 或 `warning`；網址一律不寫進記錄 |
| `spend_logs_retention` | `30d` | 用量紀錄保留期間 |
| `spend_logs_cleanup_cron` | `30 3 * * *` | 清理時間（HA 時區）；請避開自動備份時段，星期用 `mon`、`sun` 等縮寫 |
| `password_breach_check` | 開 | 後台資料庫使用者的密碼是否檢查外洩（HIBP k-anonymity） |
| `chatgpt_subscription` | 開 | ChatGPT 訂閱外掛 |
| `api_docs` | 關 | `/docs` 等 API 文件頁 |
| `env_vars` | 無 | 額外的環境變數；add-on 自己控制的變數會被拒絕 |

## 不支援的功能

映像不含 LiteLLM 的 enterprise 程式碼（授權不允許散布），也不設 `LITELLM_LICENSE`。實測（LiteLLM 1.104.0）影響如下：

- **Responses API 的背景模式（`background: true`）**：回 HTTP 500（`No module named 'litellm_enterprise'`）。
  **注意：錯誤發生在供應商已經收下請求之後**，供應商那邊的背景工作照樣建立並計費，但拿不到 response id，
  無法查詢或取消。請不要使用背景模式。前景（一般）的 Responses API 正常。
- **`generic_api` 回呼**：變成不做事的空殼（與 enterprise 在同一段匯入裡，一起失效）。
- Email（Resend、SendGrid、SMTP）與 PagerDuty 回呼。
- enterprise 的動態回呼控制、路由停用、金鑰管理參數。
- batch／responses 成本檢查的排程工作。
- Model hub 的自訂說明文件。
- 管理介面中需要授權的頁面（SSO、進階稽核等）。

另外不支援：多 worker、Redis、高可用。

## 誰讀得到 master key 與後台密碼

寫回設定分頁時：HA 管理員、HA Core 與在 Core 內執行的自訂整合、角色為 manager 或 admin 的 add-on、
能取得 HA 備份的人。沒寫回時：仍可經 HA 備份取得。所以只安裝可信任的自訂整合與 add-on，並使用加密備份。
