"""Headless Chromium through the HA Ingress emulator (DESIGN v3.2 §8.3 SP3 "瀏覽器", M1 must-have (d)).

1. Open <ingress>/<entry>/ui/ : the admin UI renders, the Woow bridge (ui-bridge.js) loads and runs,
   no uncaught JavaScript error.
2. Sign in with the UI credentials in the page itself; the dashboard loads under the prefix; the bridge
   is still there; no uncaught JavaScript error.
3. The HA panel (an iframe of the same origin, like the HA frontend): the UI renders inside the
   frame (X-Frame-Options / CSP frame-ancestors were rewritten for port 8099).

Inputs: INGRESS, ENTRY, WOOW_UI_USER, WOOW_UI_PASSWORD, RESULTS (JSON lines appended).
"""

import json
import os
import re
import sys

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

INGRESS = os.environ["INGRESS"]
ENTRY = os.environ["ENTRY"]
UI_USER = os.environ.get("WOOW_UI_USER", "admin")
UI_PASSWORD = os.environ["WOOW_UI_PASSWORD"]
RESULTS = os.environ.get("RESULTS", "/tmp/woow-smoke-results.jsonl")
failures = []


def check(name, ok, detail="", required=True):
    status = "PASS" if ok else ("FAIL" if required else "WARN")
    detail = str(detail).replace(UI_PASSWORD, "******")[:600]
    print(f"[{status}] {name}" + (f" — {detail}" if detail and (not ok or status == 'WARN') else ""), flush=True)
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"check": name, "status": status, "detail": detail[:300]}) + "\n")
    if not ok and required:
        failures.append(name)


def watch(page, sink):
    page.on("pageerror", lambda e: sink["pageerrors"].append(str(e)[:300]))
    page.on("console", lambda m: m.type == "error" and sink["console"].append(m.text[:300]))
    page.on("response", lambda r: "ui-bridge.js" in r.url and sink["bridge"].append(r.status))
    page.on("requestfailed", lambda r: sink["failed"].append(f"{r.url} {r.failure}"[:300]))


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        page = ctx.new_page()
        sink = {"pageerrors": [], "console": [], "bridge": [], "failed": []}
        watch(page, sink)

        page.goto(f"{INGRESS}{ENTRY}/ui/", wait_until="load", timeout=60_000)
        try:
            page.get_by_placeholder("Enter your username").wait_for(timeout=60_000)
            login_form = True
        except PWTimeout:
            login_form = False
        check("d/admin UI under the prefix shows the sign-in form", login_form, page.url)
        check("d/ui-bridge.js loaded (200)", 200 in sink["bridge"], sink["bridge"])
        check("d/bridge script ran (window.__woowChatgptBridge)", page.evaluate("window.__woowChatgptBridge === true"))
        check("d/no uncaught JS error on the sign-in page", not sink["pageerrors"], sink["pageerrors"])

        if login_form:
            page.get_by_placeholder("Enter your username").fill(UI_USER)
            page.get_by_placeholder("Enter your password").fill(UI_PASSWORD)
            page.get_by_role("button", name=re.compile(r"^\s*(log ?in|sign ?in)\s*$", re.I)).click()
            try:
                page.wait_for_url(re.compile(re.escape(ENTRY) + r"/ui/?(\?.*)?$"), timeout=60_000)
                page.wait_for_load_state("networkidle", timeout=60_000)
                signed_in = "login" not in page.url.split("?")[0].rsplit("/ui", 1)[-1]
            except PWTimeout:
                signed_in = False
            check("d/sign-in in the page lands on the dashboard under the prefix", signed_in, page.url)
            check("d/bridge present on the dashboard", page.evaluate("window.__woowChatgptBridge === true"))
            has_cookie = any(c["name"] == "token" for c in ctx.cookies())
            check("d/session cookie set", has_cookie)
        check("d/no uncaught JS error (sign-in + dashboard)", not sink["pageerrors"], sink["pageerrors"])
        check("d/console errors (informational)", not sink["console"], sink["console"][:8], required=False)
        page.close()

        # HA panel: the add-on UI in a same-origin iframe
        panel = ctx.new_page()
        psink = {"pageerrors": [], "console": [], "bridge": [], "failed": []}
        watch(panel, psink)
        panel.goto(f"{INGRESS}/__ha_panel.html?src={ENTRY}/ui/", wait_until="load", timeout=60_000)
        frame_ok, frame_url = False, ""
        for _ in range(60):
            frames = [f for f in panel.frames if f != panel.main_frame]
            if frames:
                frame_url = frames[0].url
                try:
                    if frames[0].evaluate("document.body && document.body.innerText.length > 0 && !!window.__woowChatgptBridge"):
                        frame_ok = True
                        break
                except Exception:  # noqa: BLE001 — frame still navigating
                    pass
            panel.wait_for_timeout(1000)
        check("d/admin UI renders inside the HA panel iframe (not blocked by framing headers)", frame_ok, frame_url)
        check("d/no uncaught JS error in the panel", not psink["pageerrors"], psink["pageerrors"])
        browser.close()


if __name__ == "__main__":
    main()
    if failures:
        print(f"{len(failures)} required browser check(s) failed: {failures}", file=sys.stderr)
        sys.exit(1)
