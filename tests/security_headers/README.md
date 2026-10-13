# Offline security headers regression

Scope: the real LiteLLM 1.104.0 `SecurityHeadersMiddleware`, loaded by path; real Starlette ASGI requests/responses, with a synthetic downstream auth gate. No proxy-server/auth/provider monkeypatch, DB, live identity or inference. The baseline fixture is byte-identical to upstream module SHA256 `961034ee861a8833b564f1921413a82e8e41e17daf561f9f6db99f3e053176a5` (LiteLLM MIT source).

Run from repository root with Python containing Starlette/httpx:

```sh
python -m unittest discover -s tests/security_headers -p 'test_*.py' -v
```

This runs 15 ASGI contract methods and 5 installer controls. To reproduce baseline header failures, set `LITELLM_SECURITY_MODULE` to the absolute path of `tests/security_headers/fixtures/upstream_security_headers_middleware.py` and run `test_headers.py`. The baseline is intentionally missing no-store, HTML object/base CSP and Referrer-Policy defaults.

The browser fixture starts two task-owned loopback Uvicorn servers, launches a fresh Chromium context, denies external browser requests and stops both servers/context in `finally`. It uses real pinned LiteLLM Next static files for the login shell. It never submits that login form. Configure:

```sh
export LITELLM_UI_ASSETS=/absolute/path/to/litellm/proxy/_experimental/out
export CHROMIUM_EXECUTABLE=/absolute/path/to/chromium
export BROWSER_RECEIPT=/absolute/path/to/browser-result.json
python tests/security_headers/browser_fixture.py
```

Baseline browser: object request allowed, injected base URI accepted, cross-origin Referer sent. Patched browser: all three blocked while inline JavaScript, onclick handlers and real Next login inputs remain operational. Routes are local fixtures, not production. This is a focused compatibility test, not a full admin UI/browser-cache/authentication acceptance suite. Playwright routing disables browser caching; no HTTP-cache behavioral claim is made. One identical Next fixture page-error hash occurs on baseline and candidate; login hydration/input behavior passes on both. The fixture backend is deliberately incomplete.

Existing route CSP policies/directives (including nonces and SAMEORIGIN framing) and explicit Referrer-Policy remain authoritative. Only missing HTML object/base directives are appended as an additional enforcing policy. No script-src/default-src is introduced. Credential-path, Authorization-bearing and Set-Cookie responses force no-store even if the route accidentally supplies public caching. Anonymous static asset cache settings remain unchanged. Cookies, status, body chunks, auth challenges and Retry-After are preserved. HSTS remains the upstream opt-in; edge-generated responses are outside this middleware.

Build application is explicit and one-shot:

```sh
python litellm/patches/apply_security_headers.py /path/to/installed/litellm
```

The installer checks baseline hash, payload hash and syntax before writing the single target file; repeated application/drift/symlinks fail without writing. Do not use it against a running installation. Parent image integration must vendor the installer plus `security_headers/security_headers_middleware.py`, invoke it at image build time, and add its final installed hash to image validation. No Dockerfile/workflow/plugin changes are included, to avoid concurrent ownership conflicts.

Exact composition verified offline: Boole API patch commit `f5ab93c3829bba41f61dcf50338e030a13f0fc07`, SSE patch `f3fb064ec04210a21febcb8c66aa8fe3b8ab4625`; security target is disjoint. Security→API→SSE and SSE→API→security produce identical hashes. Re-run the parent built-image gates after vendoring; these local tests do not establish image publication or deployment.
