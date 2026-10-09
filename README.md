# Woow LiteLLM（Home Assistant add-on）

**狀態：開發中，尚未發佈。**

> **非公開散布**：映像（`ghcr.io/woowtech/woow-ha-litellm-amd64`）是 GHCR 的私人 package，只供 WOOWTECH
> 自有的 Home Assistant 主機使用（負責人 2026-10-09 決定）。映像直接以 BerriAI 官方的 `litellm-non_root`
> 為底、保留 enterprise 程式碼（與 Woow PaaS 相同），所以不公開散布。這個 repo 是公開的，只放我們自己的
> MIT 程式碼與 Woow 外掛副本，不含任何上游檔案。

把 [LiteLLM](https://github.com/BerriAI/litellm) 打包成 Home Assistant add-on。功能與 WOOW PaaS 上的 LiteLLM 對齊：

- 使用與 PaaS 相同版本的 LiteLLM（1.104.0），之後一起升版。
- 使用同一套 Woow 外掛，例如連結多個 ChatGPT 訂閱。
- 在 Home Assistant 側邊欄直接開啟 LiteLLM 管理畫面。

本 add-on 由 WOOWTECH 打包，不是 BerriAI 的官方產品；LiteLLM 是 BerriAI 的專案名稱。

## 安裝

映像預先建好（`ghcr.io/woowtech/woow-ha-litellm-amd64`，私人），不在 Home Assistant 上建置。
HA 主機要先在 Supervisor 設定 `ghcr.io` 的唯讀憑證才拉得到（見 `litellm/DOCS.md`）。

## 目錄

| 路徑 | 內容 |
|---|---|
| `litellm/` | add-on 本身：`config.yaml`、`Dockerfile`、`rootfs/`（s6 服務、初始化、nginx、Woow 外掛副本）、翻譯、文件 |
| `litellm/upstream.lock.json` | 上游映像 digest、上游 Env、Wolfi 套件（38 個確切版本）、s6-overlay sha256 |
| `litellm/woow-plugin.lock.json` | Woow 外掛的來源（woow-paas-charts）、commit 與雜湊；副本不得修改 |
| `tests/` | `test_config_contract.py`、`test_init_scripts.py`（本機可跑）；`smoke/`（CI 的容器冒煙） |
| `tools/` | `sync-woow-plugin.sh`、`verify-woow-plugin.sh` |

## 開發

```sh
uv run --no-project --python 3.13 --with pytest==8.3.4 --with pyyaml==6.0.2 --with apscheduler==3.11.2 pytest -q tests
tools/verify-woow-plugin.sh            # 副本與 woow-paas-charts 的來源 commit 逐位元比對（需要本機 clone）
tools/sync-woow-plugin.sh <commit>     # 外掛改版：先改 woow-paas-charts，再同步到這裡
```

映像建置與容器冒煙只在 GitHub Actions 跑（`.github/workflows/ci.yml`）：映像只載入 runner 本機，不推送、不用建置快取、
不上傳 artifact（repo 與 Actions log 是公開的）。

## 授權

本 repo 的程式碼為 MIT（`LICENSE`）。映像含上游 LiteLLM（MIT，enterprise 目錄另有專屬授權）等第三方軟體，
只在 WOOWTECH 內部使用。
