"""HTTP checks of the smoke test, run on the CI runner against the add-on container
(DESIGN v3.2 §12.2; the M1 must-haves (a)–(c), plus the cheap extras of items 3, 4, 5, 9 of §12.2).

    python3 smoke_checks.py local    # boot A: no Supervisor token
    python3 smoke_checks.py ingress  # boot B: Supervisor + HA Ingress emulators, same /data

Inputs (environment): ADDON (container IP), INGRESS (emulator base URL), ENTRY (ingress_entry),
WOOW_MASTER_KEY, WOOW_UI_USER, WOOW_UI_PASSWORD, STATE_FILE (shared between the two runs),
RESULTS (JSON lines appended). Secrets are never printed.
"""

import base64
import http.client
import json
import os
import re
import sys
import time
from urllib.parse import urlencode, urlsplit

ADDON = os.environ.get("ADDON", "172.30.33.10")
API = f"http://{ADDON}:4000"
INGRESS = os.environ.get("INGRESS", "http://172.30.32.2:8080")
ENTRY = os.environ.get("ENTRY", "")
MASTER = os.environ.get("WOOW_MASTER_KEY", "")
UI_USER = os.environ.get("WOOW_UI_USER", "admin")
UI_PASSWORD = os.environ.get("WOOW_UI_PASSWORD", "")
STATE_FILE = os.environ.get("STATE_FILE", "/tmp/woow-smoke-state.json")
RESULTS = os.environ.get("RESULTS", "/tmp/woow-smoke-results.jsonl")

failures = []


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def header(self, name):
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return None

    def all(self, name):
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def json(self):
        return json.loads(self.body or b"{}")

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")


def req(method, url, body=None, headers=None, timeout=60) -> Resp:
    u = urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    hdrs = dict(headers or {})
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=body, headers=hdrs)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return Resp(r.status, r.getheaders(), data)


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def check(name, ok, detail="", required=True):
    status = "PASS" if ok else ("FAIL" if required else "WARN")
    line = {"check": name, "status": status, "detail": str(detail)[:300]}
    for secret in (MASTER, UI_PASSWORD):
        if secret and secret in line["detail"]:
            line["detail"] = line["detail"].replace(secret, "******")
    print(f"[{status}] {name}" + (f" — {line['detail']}" if line["detail"] and not ok else ""), flush=True)
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    if not ok and required:
        failures.append(name)
    return ok


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except (OSError, ValueError):
        return {}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w"))


def jwt_key(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("key")
    except (IndexError, ValueError):
        return None


# ── boot A: local mode ────────────────────────────────────────────────────────
def local():
    r = req("GET", f"{API}/health/readiness")
    check("a/readiness: db connected", r.status == 200 and r.json().get("db") == "connected", r.text)
    check("a/v1/models without key → 401", req("GET", f"{API}/v1/models").status == 401)
    check("a/v1/models with master key → 200", req("GET", f"{API}/v1/models", headers=bearer(MASTER)).status == 200)
    r = req("GET", f"{API}/")
    check("a/4000 / redirects to /ui/ (ROOT_REDIRECT_URL)", r.status in (301, 302, 303, 307, 308)
          and r.header("location") == "/ui/", f"{r.status} {r.header('location')}")
    r = req("GET", f"{API}/ui/")
    check("a/4000 /ui/ → 200 HTML (no prefix in local mode)", r.status == 200 and "text/html" in (r.header("content-type") or ""), r.status)
    check("a/4000 keeps X-Frame-Options DENY", (r.header("x-frame-options") or "").upper() == "DENY", r.header("x-frame-options"))
    check("a/dashboard HTML loads ui-bridge.js", '<script src="/woow/chatgpt/ui-bridge.js" defer></script>' in r.text)
    for path in ("/docs", "/openapi.json", "/redoc"):
        check(f"a/api_docs off: {path} → 404", req("GET", f"{API}{path}").status == 404, required=False)
    check("a/plugin page /woow/chatgpt → 200", req("GET", f"{API}/woow/chatgpt").status == 200)
    check("a/plugin API without key → 401", req("GET", f"{API}/woow/chatgpt/api/accounts").status == 401)
    r = req("GET", f"{API}/woow/chatgpt/api/accounts", headers=bearer(MASTER))
    check("a/plugin API with master key → 200", r.status == 200, r.status)

    state = load_state()
    r = req("POST", f"{API}/team/new", {"team_alias": "woow-smoke-team"}, headers=bearer(MASTER))
    ok = check("a/team/new", r.status == 200 and r.json().get("team_id"), r.text)
    if ok:
        state["team_id"] = r.json()["team_id"]
    model = {
        "model_name": "woow-smoke-mock",
        "litellm_params": {"model": "openai/gpt-4o-mini", "mock_response": "hello from the woow smoke mock",
                           "api_base": "https://mock.invalid/v1"},
    }
    r = req("POST", f"{API}/model/new", model, headers=bearer(MASTER))
    if check("a/model/new (mock model, no api_key)", r.status == 200, r.text):
        time.sleep(2)
        r = req("POST", f"{API}/v1/chat/completions",
                {"model": "woow-smoke-mock", "messages": [{"role": "user", "content": "hi"}]}, headers=bearer(MASTER))
        content = ""
        try:
            content = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            pass
        check("a/chat completion answers from the mock model", content == "hello from the woow smoke mock",
              f"{r.status} {r.text[:200]}", required=False)
    responses_background()
    save_state(state)


def responses_background():
    """Owner decision 2026-10-09: enterprise code is kept, so the Responses API background mode works
    (the M0 pruned image answered 500 "No module named 'litellm_enterprise'")."""
    mock = os.environ.get("MOCK_UPSTREAM")
    if not mock:
        return
    model = {"model_name": "woow-smoke-upstream",
             "litellm_params": {"model": "openai/mock-bg", "api_base": f"{mock}/v1", "api_key": "sk-mock-placeholder"}}
    r = req("POST", f"{API}/model/new", model, headers=bearer(MASTER))
    if not check("a/model/new (mock OpenAI-compatible upstream)", r.status == 200, r.text):
        return
    time.sleep(2)
    r = req("POST", f"{API}/v1/responses", {"model": "woow-smoke-upstream", "input": "ping"}, headers=bearer(MASTER))
    check("a/Responses API (foreground) → 200", r.status == 200, f"{r.status} {r.text[:200]}")
    r = req("POST", f"{API}/v1/responses", {"model": "woow-smoke-upstream", "input": "ping", "background": True},
            headers=bearer(MASTER))
    body = r.json() if r.status == 200 else {}
    check("a/Responses API background: true → 200 queued (enterprise code present, no ImportError)",
          r.status == 200 and body.get("status") == "queued" and "litellm_enterprise" not in r.text,
          f"{r.status} {r.text[:300]}")


# ── boot B: Supervisor + Ingress emulators ────────────────────────────────────
def ingress():
    state = load_state()
    base = f"{INGRESS}{ENTRY}"
    host = urlsplit(INGRESS).netloc

    # (c) persistence across the restart
    if state.get("team_id"):
        r = req("GET", f"{API}/team/info?team_id={state['team_id']}", headers=bearer(MASTER))
        check("c/team created in boot A still there", r.status == 200, r.status)
    else:
        check("c/team created in boot A still there", False, "boot A did not create it")
    r = req("GET", f"{API}/model/info", headers=bearer(MASTER))
    names = [m.get("model_name") for m in (r.json().get("data") or [])] if r.status == 200 else []
    check("c/mock model from boot A still there", "woow-smoke-mock" in names, names)

    # (b) direct 4000: prefix rules
    r = req("GET", f"{API}/")
    check("b/4000 / redirects to <entry>/ui/", r.status in (301, 302, 303, 307, 308) and r.header("location") == f"{ENTRY}/ui/",
          f"{r.status} {r.header('location')}")
    check("b/4000 /ui/ without prefix → 404 (by design)", req("GET", f"{API}/ui/").status == 404)
    r = req("GET", f"{API}{ENTRY}/ui/")
    check("b/4000 <entry>/ui/ → 200", r.status == 200, r.status)
    check("b/4000 still answers X-Frame-Options DENY", (r.header("x-frame-options") or "").upper() == "DENY",
          r.header("x-frame-options"))
    check("b/4000 unprefixed API /v1/models → 401 / 200", req("GET", f"{API}/v1/models").status == 401
          and req("GET", f"{API}/v1/models", headers=bearer(MASTER)).status == 200)
    check("b/4000 unprefixed /health/readiness → 200", req("GET", f"{API}/health/readiness").status == 200)
    check("b/8099 refuses other sources (403)", req("GET", f"http://{ADDON}:8099/").status == 403)

    # (b) through the Ingress emulator
    r = req("GET", f"{base}/")
    check("b/ingress / → 302 ui/", r.status == 302 and r.header("location") == "ui/", f"{r.status} {r.header('location')}")
    r = req("GET", f"{base}/ui/")
    html = r.text
    check("b/ingress /ui/ → 200 HTML", r.status == 200 and "text/html" in (r.header("content-type") or ""), r.status)
    check("b/ingress X-Frame-Options → SAMEORIGIN", r.header("x-frame-options") == "SAMEORIGIN", r.header("x-frame-options"))
    csp = r.header("content-security-policy") or ""
    check("b/ingress CSP frame-ancestors → 'self'", csp == "frame-ancestors 'self'", csp)
    check("b/ingress HTML has no /litellm-asset-prefix left", "/litellm-asset-prefix" not in html)
    tag = f'<script src="{ENTRY}/woow/chatgpt/ui-bridge.js" defer></script>'
    check("b/bridge <script> URL carries the prefix", tag in html, re.findall(r"<script[^>]*ui-bridge[^>]*>", html))
    assets = sorted(set(re.findall(r'(?:src|href)="(' + re.escape(ENTRY) + r'/_next/[^"]+)"', html)))
    check("b/ingress HTML references prefixed _next assets", len(assets) >= 3, len(assets))
    bad = []
    for asset in assets[:12]:
        a = req("GET", f"{INGRESS}{asset}")
        if a.status != 200:
            bad.append(f"{asset} {a.status}")
    check(f"b/ingress _next assets → 200 ({min(len(assets), 12)} fetched)", assets and not bad, bad)
    r = req("GET", f"{base}/woow/chatgpt/ui-bridge.js")
    check("b/ingress ui-bridge.js → 200", r.status == 200 and "__woowChatgptBridge" in r.text, r.status)
    r = req("GET", f"{base}/woow/chatgpt")
    check("b/ingress plugin page /woow/chatgpt → 200", r.status == 200, r.status)
    check("b/ingress plugin page keeps SAMEORIGIN / 'self'", r.header("x-frame-options") == "SAMEORIGIN"
          and r.header("content-security-policy") == "frame-ancestors 'self'",
          f"{r.header('x-frame-options')} | {r.header('content-security-policy')}")
    check("b/ingress plugin API without key → 401", req("GET", f"{base}/woow/chatgpt/api/accounts").status == 401)
    r = req("GET", f"{base}/woow/chatgpt/api/accounts", headers=bearer(MASTER))
    check("b/ingress plugin API with master key → 200", r.status == 200, r.status)

    # (b) sign-in through the Ingress (HA served over https: X-Forwarded-Proto kept by Core)
    https = {"X-Forwarded-Proto": "https", "Host": host}
    r = req("POST", f"{base}/v2/login", {"username": UI_USER, "password": UI_PASSWORD}, headers=https)
    want = f"https://{host}{ENTRY}/ui?login=success"
    body = r.json() if r.status == 200 else {}
    check("b/v2/login → 200", r.status == 200, r.status)
    check("b/v2/login redirect_url carries the prefix", body.get("redirect_url") == want, body.get("redirect_url"))
    cookies = r.all("set-cookie")
    token_cookie = next((c for c in cookies if c.startswith("token=")), "")
    check("b/session cookie Path=<entry>/", f"Path={ENTRY}/" in token_cookie, re.sub(r"token=[^;]+", "token=…", token_cookie))
    check("b/session cookie Secure under https", "secure" in token_cookie.lower(), required=False)
    token = token_cookie.split(";")[0].split("=", 1)[1] if token_cookie else body.get("token", "")
    key = jwt_key(token) if token else None
    if key:
        r = req("GET", f"{base}/woow/chatgpt/api/accounts", headers=bearer(key))
        check("b/plugin API with the UI session key → 200", r.status == 200, r.status)
    else:
        check("b/plugin API with the UI session key → 200", False, "no key in the JWT")
    form = urlencode({"username": UI_USER, "password": UI_PASSWORD}).encode()
    r = req("POST", f"{base}/login", form, headers={**https, "Content-Type": "application/x-www-form-urlencoded"})
    loc = r.header("location") or ""
    check("b/login form → 303 inside the ingress path", r.status == 303 and loc.startswith(f"{ENTRY}/ui"), f"{r.status} {loc}")
    if key:
        r = req("POST", f"{base}/session/logout", headers={**https, "Cookie": f"token={token}", **bearer(key)})
        cleared = [c for c in r.all("set-cookie") if c.startswith("token=")]
        check("b/logout clears the cookie at <entry>/", r.status in (200, 204) and any(f"Path={ENTRY}/" in c for c in cleared),
              f"{r.status} {[re.sub(r'token=[^;]*', 'token=…', c) for c in cleared]}", required=False)
        time.sleep(1)
        check("b/logged-out session key → 401", req("GET", f"{API}/v1/models", headers=bearer(key)).status == 401,
              required=False)


if __name__ == "__main__":
    {"local": local, "ingress": ingress}[sys.argv[1]]()
    if failures:
        print(f"{len(failures)} required check(s) failed: {failures}", file=sys.stderr)
        sys.exit(1)
