# LiteLLM 1.104.0 登入安全候選

僅修正 SEC10/11 已確認問題：保留原本 `count > limit`，在造成封鎖的
`LoginAttempt.failed()` 立即拋出原生 `ProxyException(429)`，保留 `Retry-After`。
前 10 個來源失敗／5 個帳號配對失敗仍回 401，第 11／6 次即為 429。

`apply_login_throttle.py` 支援 `apply(Path)` 及 CLI positional package root，
只寫 `proxy/auth/login_throttle.py`，無其他 payload。未知 hash、已套用與
symlink 輸入均拒絕，不做 fuzzy patch。基準與結果 SHA256 由 installer 常數固定。

block/refuse 兩個事件用 `woow.security.login_throttle` 的獨立 WARNING stderr
handler，`propagate=False`。帳號／來源只輸出固定 `[redacted]`，LogRecord args
也沒有原始值。不改 verbose logger、全域 handler 或 Redis exception 診斷。

## 映像內驗證

```sh
python3 -I -B /path/to/tests/login_security/run_image_login_security.py
```

入口要求 installed LiteLLM 1.104.0、精確 patched module hash、精確 test hash，
不接受 CLI source override；覆蓋主機傳入的開發用 module 環境變數。
執行 16 個測試，0 skip 才通過。需要映像本來的 proxy dependencies，僅用 unittest，
不需額外 wheels。應在 `--network none --read-only` 映像中執行；測試內另用 audit
hook 禁止網路、child process、寫入 bytecode。只改測試 process 中的 env／logging／cache。

測試直接載入 pinned 完整 auth module、真實 `InMemoryCache` 與
`login_utils.authenticate_user()`；不重寫節流計數。涵蓋超標當次、封鎖期間不驗密碼、
不延長封鎖、到期、大小寫、不同來源、成功清除配對、disabled、自訂 TTL、
ERROR logging 下的安全 WARNING 與原始 LogRecord 遮罩。
Redis 只 stub I/O 的錯誤／TTL 返回值，**未執行 Lua 或跨 worker Redis 實測**。
成功登入僅驗 `LoginAttempt.succeeded()` 清除計數；不宣稱 mint token 或 live login。

## 本機 RED/GREEN 與 installer unit tests

使用已安裝完整 LiteLLM 1.104.0 dependencies 的 Python，對同一測試分別傳入原始／候選
module 路徑。此 seam 僅給直接執行 `test_contract.py`，不適用 installed-image gate。

```sh
LITELLM_LOGIN_SECURITY_MODULE=/path/to/pristine/login_throttle.py \
  python3 -B tests/login_security/test_contract.py
LITELLM_LOGIN_SECURITY_MODULE=/path/to/patched/login_throttle.py \
  python3 -B tests/login_security/test_contract.py
LITELLM_LOGIN_BASELINE=/path/to/pristine/login_throttle.py \
  python3 -B -m unittest discover -s tests/login_security -p check_installer.py -v
```

Installer 測試共 7 個。若複製到 HA，預設 installer 相對位置需用
`LITELLM_LOGIN_INSTALLER=/path/to/litellm/patches/apply_login_throttle.py` 指定；
image runner 不讀 installer。HTTP 路由、300 秒實時等待、實際 Docker image 與
HA/PaaS sandbox 驗收由 parent 接續，本地結果不能替代。

HA: `tools/ci-login-security-contracts.sh <built-image>` runs the 16 installed-source contracts in CI and publishing. `check_installer.py` is an explicit developer check with `LITELLM_LOGIN_BASELINE` pointing to pristine pinned source; it is not collected by the lightweight unit job.
