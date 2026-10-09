"""WOOW PaaS litellm chart: run at start-up by every python process of the proxy
container (PYTHONPATH=/woow/py). Only installs lazy import hooks (ChatGPT sign-in
guard on LiteLLM's Authenticator; the /woow/chatgpt page on the proxy app); nothing
of LiteLLM is imported here, and a failure never stops the proxy from starting."""
import os
import sys

if os.environ.get("WOOW_CHATGPT_ENABLED") == "true":
    try:
        import woow_chatgpt

        woow_chatgpt.install_import_hook()
    except Exception as exc:  # noqa: BLE001 — start the proxy regardless
        print(f"woow sitecustomize: ChatGPT sign-in guard NOT installed: {exc!r}", file=sys.stderr)
