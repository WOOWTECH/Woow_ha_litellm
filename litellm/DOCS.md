# Woow LiteLLM

> **測試版（experimental）**：已提供內部版本，設定與資料格式在 0.1.x 期間仍可能調整。

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
2. 在 HA 加入憑證：設定 → 應用程式 → 安裝應用程式 → 右上角選單 →「Registries」→ 新增，
   伺服器 `ghcr.io`、使用者名稱填服務帳號、密碼填權杖；或在 HA 的終端機執行
   `ha docker registries add ghcr.io --username <服務帳號> --password <權杖>`（CLI 2026.09.0）。
3. **設好後立刻試拉兩支公開映像，確認沒有被擋**（例如在 HA 終端機或有 Docker 的主機上，以同一組憑證）：
   - HA 的基底映像 `ghcr.io/home-assistant/amd64-base`；
   - 任一支已安裝的 Woow MCP 映像，例如 `ghcr.io/woowtech/amd64-mcp-odoo`。
   兩者都要拉得到；任何一個失敗就先回復（見下）再查原因。
4. 加入本 repository（不在 Woow 商店上架）：設定 → 應用程式 → 安裝應用程式 → 右上角選單 →「Repositories」，
   加入 `https://github.com/WOOWTECH/Woow_ha_litellm`，再從商店清單安裝 Woow LiteLLM。

**出事時回復**：在 HA 終端機執行 `ha docker registries remove ghcr.io`，HA 自身與公開的 add-on 會回到匿名拉取；
Woow LiteLLM 之後就無法更新或重新安裝，直到憑證修好為止。

## 第一次啟動

不必填任何設定，直接啟動：

1. 自動產生 master key、salt key、資料庫密碼、後台密碼，存在 `/data/secrets`。
2. 建立資料庫並套用 LiteLLM 的全部 migration（第一次約 2–5 分鐘，期間側邊欄顯示「LiteLLM 啟動中」）。
3. 啟動 LiteLLM。

**後台帳號**預設 `admin`；**後台密碼**與 **master key** 在 Home Assistant 2026.5.0、Supervisor 2026.07.0
以上時，會自動寫回 add-on 的「設定」分頁（只寫一次）。只有 HA 管理員看得到設定分頁。

- 建議抄下後清空 `master_key` 與 `ui_password`，之後不會再寫回；值仍保存在 `/data/secrets`。
  Supervisor 選項驗證失敗時，錯誤橫幅與 Supervisor log 可能帶出整份選項。分享前請遮蔽秘密；若已外流，請換值並重啟。
- 要換 master key 或後台密碼：在設定填新的值並重新啟動。所有後台登入會被登出。
- **salt key 永遠不會顯示、也不能更換**：它加密了你存在 LiteLLM 裡的所有 provider 金鑰。遺失就只能從備份還原。

## 側邊欄

側邊欄的「LiteLLM」直接開啟 LiteLLM 管理介面，用上面的後台帳號密碼登入（HA 預設登入有效 4 小時，Woow PaaS 預設 24 小時；可在設定調整）。

- 某次開機若在 60 秒內取不到側邊欄位址，那次會改顯示說明頁；**重新啟動 add-on** 即可恢復。
- 安全提醒：側邊欄與 HA 前端同源，HA 前端上的其他腳本（自訂卡片、其他 add-on 的頁面）理論上讀得到後台登入。
  請只安裝可信任的自訂元件與 add-on；需要長時間操作時改用區網或 Cloudflare Tunnel 開啟。

- Ingress 單次請求上限為 **64 MB**（nginx `client_max_body_size 64m`，超過回 413；與下方 4000 埠的上限相同）；更大的請求請改用受保護的 API 連線。
- 建議在應用程式資訊頁開啟 **Watchdog**。容器健康檢查會同時檢查 LiteLLM 與 nginx，並保留啟動寬限期。
- 經 Cloudflare Tunnel 或 LAN userland-proxy 的使用者可能共用來源 IP，因此共用登入節流額度；
  重複輸入錯誤密碼可能暫時影響其他人的登入。請等待節流解除，不要停用登入節流。
- 手機畫面狹窄時，先關閉提示橫幅、收合側欄；表格操作可改用橫向或桌面。
- 「LiteLLM 啟動中」頁只會在 LiteLLM 尚未就緒時出現；就緒後若 Ingress 無法轉送某個回應（例如回應標頭過大、連線中斷），
  會改回 JSON 錯誤（`ingress_proxy_error`）。這時 LiteLLM 可能已完成並計費，**請先查看紀錄再重試**。
- **名稱不可含「%」「/」「#」「?」「\」**：後台會把名稱原樣放進網址，瀏覽器會在 `#`、`?` 截斷網址，並把 `\` 換成 `/`；
  HA 的 Ingress 還會把網址路徑再解碼一次，名稱裡字面的 `%2F`、`%20` 會被當成 `/`、空白，`%23`、`%3F` 會變成 `#`、`?`。
  這類名稱經側邊欄刪除或修改時，可能改到別的憑證或模型（例如刪「x#y」實際刪掉「x」）。因此新建或改名時，
  憑證名稱與自訂模型 ID 不可含 `%`、`/`、`#`、`?`、`\`，也不可以只是 `.` 或 `..`；模型名稱（model_name）不可含 `%`
  （`chatgpt/…` 這類 `/` 是正常寫法）；其他字元（中文、空白、`-`、`_`、`.`、`:` 等）都可以。
  升級前就存在的這類名稱照常可用：add-on 每次啟動會在 log 列出（`woow-name-scan`），
  只要有任何憑證名稱或模型 ID 含 `%`、`#` 或 `?`，側邊欄中路徑含 `%` 的刪除／修改（DELETE、PATCH、PUT）一律回 409，
  請改用 4000 埠（見「區網與對外」）直接操作，或先在那裡把名稱改掉；改完重新啟動 add-on 即解除。
  含 `#`、`?` 的舊名稱不要在後台畫面刪除或修改：瀏覽器送出前就截斷了網址，409 擋不到，會改到名稱較短的另一個憑證或模型。

## 給同一台 HA 的其他 add-on

內部位址：`http://<add-on 主機名>:4000`（主機名是 `<repository 前綴>-woow-litellm`，在 add-on 資訊頁可看到），
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
- 同一個 ChatGPT 帳號可以同時登入多個 LiteLLM（2026-10-23 實測 HA add-on 與 PaaS 共存，兩邊各自換發 token 後仍可用），
  但每個 LiteLLM 必須各自完成登入；refresh token 會輪替且只能用一次，請勿複製或共用登入檔。同一台 LiteLLM 內不要用兩個憑證連同一個帳號。
  訂閱額度由所有使用該帳號的地方共用。
  **不可把備份或 dump 還原到第二台並同時使用**，因為兩邊會共用輪替的 refresh token。
- 多個帳號加同名模型時成為同一個模型群組、自動分流。某帳號的**週額度用完**（429 `usage_limit_reached`，通常數小時到數天才重置，且上游不帶 `Retry-After`）時，外掛會替它補 `Retry-After`（至少 61、最多 900 秒），路由器讓該帳號冷卻最長 15 分鐘，不再每 6–9 秒就打一次；`Retry-After` 也會回給呼叫端。冷卻存在記憶體，重新啟動 add-on 會清掉；額度提早恢復時最晚晚 15 分鐘才會再用到。只有一個帳號的群組不冷卻（LiteLLM 的規則）。
- 訂閱模型的 cost 記為 **0**；金額預算不能限制訂閱用量，請使用 RPM／TPM 限流與模型權限控制。
- 同一個外掛也帶來下列行為（只在 `chatgpt_subscription` 開啟時載入；與 Woow PaaS 的預設相同，另有說明者除外）：
  - 推論模型（o 系列、gpt-5 以上、DeepSeek R1、`:thinking` 等，或模型資訊標了 `supports_reasoning`）的輸出上限小於 512 時自動提高到 512，回應帶 `x-woow-max-tokens-raised: 16->512`；
    隱藏推理用完上限造成空回覆時帶 `x-woow-warning: reasoning_exhausted_max_tokens`。請求關閉推理（`reasoning_effort: none`）或帶 `thinking` 則不動。
  - 有預算上限的金鑰使用沒有單價的模型時，回應帶 `x-woow-warning: model_unpriced_budget_not_enforced`（照常放行；與 `WOOW_BUDGET_UNPRICED=block_budgeted_keys` 的拒絕並存，後者先擋）。
  - 上游回 408／409／429／5xx 時，把上游的 `Retry-After`、`retry-after-ms`、`x-should-retry` 以標準名稱轉給呼叫端；上游逾時改回 504（`type: timeout`）而不是 408。
  - 請求本體上限 64 MB（與側邊欄 Ingress 相同），超過回 413 `request_too_large`；4000 埠也適用。
  - 後台 session cookie 的 HttpOnly 加固在此 add-on 預設關閉：它會強制 `Secure`，區網用 HTTP 開 HA（或直連 4000 埠）時瀏覽器會丟掉 cookie 而登入不了。全程 HTTPS 時可用 `WOOW_UI_TOKEN_HTTPONLY=true` 開啟。
- 關掉 `chatgpt_subscription` 並重新啟動後，ChatGPT 訂閱模型不會載入執行中的路由，也不會觸發上游裝置登入；其他供應商模型照常載入。
- 資料庫中的模型、憑證與已連線帳號均保留；重新開啟並啟動後恢復載入。要刪除帳號請先在 ChatGPT 訂閱頁中斷連線。
- 已知小問題：從區網不帶前綴直接開 `/woow/chatgpt` 時，「回 LiteLLM 後台」連結會 404；從後台進入則正常。

## 備份與還原

- 備份方式是冷備份：HA 備份時會先停止 add-on（約 1–2 分鐘），再打包 `/data`。每天的自動備份都會造成這段停機。
- 請在 HA 設定每日自動備份、確認包含本應用程式，並保留一份離機／異地副本；add-on 不會自動替 HA 建立排程。
- 備份內含 master key、salt key、資料庫與 ChatGPT 訂閱的登入：**請使用 HA 的加密備份**。
- 還原後每次開機都會自動修正檔案擁有者與權限。
- LiteLLM 換版前會自動 `pg_dump` 到 `/data/pre-upgrade-dumps`（保留 2 份，不進 HA 備份）。資料庫狀態查詢、dump 或 migration 失敗／逾時都不會繼續啟動 API。
  中斷的暫存 dump 會在停止或下次啟動時清除；完整 dump 保留兩份。
- 升級後首次啟動可能需要數分鐘。停止會先中斷啟動子程序，再停止 PostgreSQL；不要以強制斷電代替正常停止。
- 不支援只降映像版本（資料庫 migration 只能往前）；要回退請還原 HA 備份。
- **解除安裝會刪除所有資料（金鑰與資料庫），無法復原。**

## 設定

| 選項 | 預設 | 說明 |
|---|---|---|
| `master_key` | 空白 | 空白＝沿用已存的值；填 `sk-…` 換新的 |
| `ui_username` | `admin` | 後台帳號 |
| `ui_password` | 空白 | 空白＝沿用已存的值；至少 12 字，前後不可有空白 |
| `ui_session_duration` | `4h` | 後台登入有效時間，1h–24h |
| `log_level` | `warning` | `error` 或 `warning`；網址一律不寫進記錄 |
| `spend_logs_retention` | `30d` | 用量紀錄保留期間 |
| `spend_logs_cleanup_cron` | `30 3 * * *` | 清理時間（HA 時區）；請避開自動備份時段，星期用 `mon`、`sun` 等縮寫 |
| `password_breach_check` | 開 | 後台資料庫使用者的密碼是否檢查外洩（HIBP k-anonymity） |
| `chatgpt_subscription` | 開 | ChatGPT 訂閱外掛 |
| `api_docs` | 關 | `/docs` 等 API 文件頁 |
| `env_vars` | 無 | 額外的環境變數；不可空值、換行、結尾空格／tab，最多 4095 bytes；啟動摘要只列名稱 |

## 不支援的功能

- LiteLLM 的 Enterprise 授權功能（SSO、進階稽核等）：映像含 enterprise 程式碼，但不設 `LITELLM_LICENSE`，
  需要授權的功能與後台頁面無法使用（與 Woow PaaS 相同）。Responses API 的背景模式（`background: true`）可以使用。
- 多 worker、Redis、高可用。

## 誰讀得到 master key 與後台密碼

寫回設定分頁時：HA 管理員、HA Core 與在 Core 內執行的自訂整合、角色為 manager 或 admin 的 add-on、
能取得 HA 備份的人。沒寫回時：仍可經 HA 備份取得。所以只安裝可信任的自訂整合與 add-on，並使用加密備份。


`env_vars` 不可覆寫 add-on 的金鑰、資料庫、設定檔／雲端設定桶、TLS 路徑、登入節流、worker 與監聽埠。
HA 設定頁的格式驗證不等於啟動驗證；保留字、cron 語意與無法原樣傳遞的值會在啟動時被拒絕，訊息會指出欄位。
`WOOW_` 開頭的名稱也是保留字，只有下列退回開關可以設定，而且值會檢查：`WOOW_PROVIDER_HEADERS`（`allowlist`／`all`）、`WOOW_PROVIDER_ERROR_LOG`（`raw`／`off`）、`WOOW_TOKEN_LIMIT_VALIDATION`（`enforce`／`warn`／`off`）、`WOOW_BUDGET_ADMISSION`（`strict`／`full`／`resize`）、`WOOW_BUDGET_UNPRICED`（`allow`／`block_budgeted_keys`）、`WOOW_BUDGET_REASONING_RESERVE_TOKENS` 與 `WOOW_BUDGET_MIN_INPUT_TOKENS`（0 到 9999999 的整數）。
Woow 外掛（0.3.14，隨 `chatgpt_subscription` 載入）的行為也可以退回，預設值寫在括號內：`WOOW_REASONING_MIN_OUTPUT_MODE`（`raise`／`reject`／`off`；`raise`）、`WOOW_REASONING_MIN_OUTPUT_TOKENS`（整數；512，0＝關閉）、`WOOW_UNPRICED_MODEL_MODE`（`warn`／`reject`／`off`；`warn`）、`WOOW_FORWARD_RETRY_AFTER`（`true`／`false`；`true`）、`WOOW_UPSTREAM_TIMEOUT_STATUS`（`504`／`408`；`504`）、`WOOW_MAX_REQUEST_SIZE_MB`（MB，可含小數，0＝不限制；64）、`WOOW_UI_TOKEN_HTTPONLY`（`true`／`false`；`false`）、`WOOW_USAGE_LIMIT_COOLDOWN_MAX_SECONDS`（秒數，0 到 86400，0＝關閉；900）。
OpenRouter 錯誤回給呼叫端前會清洗；上游原文只寫進 add-on 記錄，每個錯誤一筆 ERROR（`log_level: error` 也看得到），先遮蔽金鑰（`sk-…`、Bearer、JWT、`api_key=` 等）再截到 2000 字，不會進回應、用量紀錄或回應標頭；`WOOW_PROVIDER_ERROR_LOG=off` 可不記。
PostgreSQL 使用 UTC 時間且不記錄例行 checkpoint；LiteLLM 的清理排程依 HA 時區。
未帶 API key 的請求可能留下 LiteLLM 上游的 ERROR／traceback，但 HTTP 回應仍是 401。

## 求助與授權

請向 WOOWTECH 維護者或 [GitHub issues](https://github.com/WOOWTECH/Woow_ha_litellm/issues) 提供
add-on／LiteLLM／HA 版本、重現步驟與已遮蔽的記錄。不要附金鑰、登入權杖、完整 options 或未加密備份。
本 add-on 的授權見 [LICENSE](LICENSE)；映像所含第三方軟體依各自授權，LiteLLM enterprise 功能仍需相應授權。
