# Woow LiteLLM

> **測試版（experimental）**：尚未正式發佈，設定與資料格式在 0.1.x 期間仍可能調整。

> **非公開散布**：本 add-on 只供 WOOWTECH 自有的 Home Assistant 主機使用。映像放在 GHCR 的**私人** package
> （`ghcr.io/woowtech/woow-ha-litellm-amd64`），安裝前要先在 Supervisor 設定 `ghcr.io` 的唯讀憑證
> （見下方「安裝前：映像憑證」）。映像直接以 BerriAI 的官方映像為底，內含 LiteLLM 的 enterprise 程式碼，
> 與 Woow PaaS 相同，因此不對外散布。

本 add-on 由 WOOWTECH 打包，不是 BerriAI 的官方產品；LiteLLM 是 BerriAI 的專案名稱。

把 [LiteLLM](https://github.com/BerriAI/litellm)（OpenAI 相容的 AI 閘道）連同自己的 PostgreSQL 裝在 Home Assistant 上。
LiteLLM 版本與 Woow PaaS 相同（1.104.0），一起升版；也帶同一套 Woow 外掛（ChatGPT 訂閱、多帳號）。

## 系統需求

- Home Assistant **2026.5.0 以上**（較舊的 Core 會把 add-on 的設定值給一般使用者看，所以不允許安裝）。
- 只支援 **amd64**。
- 記憶體：LiteLLM 加資料庫閒置時約 0.7–1 GB；建議主機有 4 GB 以上，2 GB 的主機不建議。
- 第一次下載映像約 500 MB。

## 安裝前：映像憑證

映像是私人的，Supervisor 要有 `ghcr.io` 的讀取憑證才拉得到。**請先讀完這一段再設定。**

- **影響範圍**：Supervisor 的 registry 憑證是以主機名稱套用的，設了 `ghcr.io` 之後，這台 HA 從 `ghcr.io`
  拉的**所有**映像都會帶這組憑證，包括 HA 自身（Core、Supervisor、外掛基底）、Woow MCP add-on 與商店裡其他放在
  `ghcr.io` 的 add-on。憑證錯了或被撤銷，這些映像的更新都可能失敗。
- **權杖**：只能用**服務帳號**建立的 GitHub **classic** token，權限**只勾 `read:packages`**，**不設到期日**
  （到期後整台 HA 從 `ghcr.io` 的更新都會失敗）。不要用個人帳號的權杖，也不要多給任何權限。
- 權杖只存在 Supervisor，不要寫進 add-on 的設定、自動化或任何檔案。

步驟：

1. 以服務帳號建立上述 classic token（只有 `read:packages`、不設到期）。
2. 在 HA 加入憑證：設定 → 附加元件 → 附加元件商店 → 右上角選單 →「Registries」→ 新增，
   伺服器 `ghcr.io`、使用者名稱填服務帳號、密碼填權杖；或在 HA 的終端機執行
   `ha registries add ghcr.io --username <服務帳號> --password <權杖>`（CLI 版本較舊時為 `ha docker registries add …`）。
3. **設好後立刻試拉兩支公開映像，確認沒有被擋**（例如在 HA 終端機或有 Docker 的主機上，以同一組憑證）：
   - HA 的基底映像 `ghcr.io/home-assistant/amd64-base`；
   - 任一支已安裝的 Woow MCP 映像，例如 `ghcr.io/woowtech/amd64-mcp-odoo`。
   兩者都要拉得到；任何一個失敗就先回復（見下）再查原因。
4. 從 Woow 商店安裝 Woow LiteLLM。

**出事時回復**：在 HA 終端機執行 `ha registries remove ghcr.io`（CLI 版本較舊時為 `ha docker registries remove ghcr.io`），HA 自身與公開的 add-on 會回到匿名拉取；
Woow LiteLLM 之後就無法更新或重新安裝，直到憑證修好為止。

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

- LiteLLM 的 Enterprise 授權功能（SSO、進階稽核等）：映像含 enterprise 程式碼，但不設 `LITELLM_LICENSE`，
  需要授權的功能與後台頁面無法使用（與 Woow PaaS 相同）。Responses API 的背景模式（`background: true`）可以使用。
- 多 worker、Redis、高可用。

## 誰讀得到 master key 與後台密碼

寫回設定分頁時：HA 管理員、HA Core 與在 Core 內執行的自訂整合、角色為 manager 或 admin 的 add-on、
能取得 HA 備份的人。沒寫回時：仍可經 HA 備份取得。所以只安裝可信任的自訂整合與 add-on，並使用加密備份。
