"""Server hardening: body limits, throttles, admin key strength, headers, Host allowlist, error hiding."""
import asyncio
import base64
import hashlib
import hmac
import importlib
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sia import security as sec  # noqa: E402
from sia.llm import LLM, LLMError  # noqa: E402

ADMIN = "admin-key-for-tests-0123456789abcdef"


def make(tmp_path, monkeypatch, **env):
    monkeypatch.delenv("SIA_LOCAL_MODE", raising=False)
    monkeypatch.delenv("SIA_ALLOWED_HOSTS", raising=False)
    for k, v in {"SIA_DB": str(tmp_path / "s.db"), "SIA_ADMIN_KEY": ADMIN, **env}.items():
        monkeypatch.setenv(k, v)
    import sia.api
    api = importlib.reload(sia.api)
    return api, TestClient(api.app, raise_server_exceptions=False)


def org(c):
    return {"Authorization": "Bearer " + c.post("/v1/tenants", json={"name": "o"},
                                                 headers={"X-Admin-Key": ADMIN}).json()["api_key"]}


def test_body_limit_counts_chunked_uploads(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch, SIA_MAX_BODY_BYTES="1000")
    h = org(c)
    pid = c.post("/v1/projects", json={"name": "p"}, headers=h).json()["id"]
    chunks = (b'{"runs": [{"id": "r1", "name": "n", "input": "' + b"x" * 400, b"x" * 400, b"x" * 400 + b'"}]}')
    r = c.post(f"/v1/projects/{pid}/runs", content=iter(chunks), headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 413  # no Content-Length header was sent, the bytes were counted
    assert c.post(f"/v1/projects/{pid}/runs", json={"runs": [{"id": "r1", "name": "n"}]}, headers=h).status_code == 202


def test_body_limit_rejects_bad_content_length():
    async def app(scope, receive, send):
        raise AssertionError("must not be reached")

    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request", "body": b""}

    for value, status in ((b"abc", 400), (b"5000", 413)):
        sent.clear()
        asyncio.run(sec.BodyLimit(app, 1000)({"type": "http", "headers": [(b"content-length", value)]}, receive, send))
        assert sent[0]["status"] == status


def test_weak_admin_key_disables_the_admin_api(tmp_path, monkeypatch):
    for weak in ("change-me", "admin", "short-but-not-random"):
        api, c = make(tmp_path, monkeypatch, SIA_ADMIN_KEY=weak)
        assert c.post("/v1/tenants", json={"name": "o"}, headers={"X-Admin-Key": weak}).status_code == 503
    assert sec.admin_key_problem("x" * 32) is None and sec.admin_key_problem("") is not None


def test_repeated_bad_keys_lock_out_the_address(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch, SIA_AUTH_FAIL_LIMIT="3")
    good = org(c)
    for _ in range(3):
        assert c.get("/v1/me", headers={"Authorization": "Bearer sia_wrong"}).status_code == 401
    r = c.get("/v1/me", headers=good)  # even a valid key waits: the address is guessing
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert c.post("/v1/tenants", json={"name": "x"}, headers={"X-Admin-Key": ADMIN}).status_code == 429


def test_admin_key_guessing_is_throttled(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch, SIA_AUTH_FAIL_LIMIT="2")
    for _ in range(2):
        assert c.post("/v1/tenants", json={"name": "x"}, headers={"X-Admin-Key": "guess"}).status_code == 403
    assert c.post("/v1/tenants", json={"name": "x"}, headers={"X-Admin-Key": ADMIN}).status_code == 429


def test_per_ip_rate_limit_spares_health(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch, SIA_IP_RATE_LIMIT="5")
    assert [c.get("/llms.txt").status_code for _ in range(6)] == [200] * 5 + [429]
    assert c.get("/health").status_code == 200


def test_security_headers(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch)
    page = c.get("/")
    csp = page.headers["content-security-policy"]
    script_src = re.search(r"script-src ([^;]+)", csp).group(1)
    inline = re.search(r"<script>(.*?)</script>", page.text, re.S).group(1)
    assert "'sha256-%s'" % base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode() in script_src
    assert "unsafe-inline" not in script_src and "frame-ancestors 'none'" in csp
    dash = c.get("/dashboard").headers["content-security-policy"]
    assert "unsafe-inline" not in re.search(r"script-src ([^;]+)", dash).group(1)
    assert "https://fonts.gstatic.com" in dash  # the dashboard uses the landing page's web fonts
    me = c.get("/v1/me")
    assert me.headers["cache-control"] == "no-store" and me.headers["content-security-policy"] == sec.API_CSP
    for name in ("x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy",
                 "cross-origin-opener-policy", "x-request-id"):
        assert name in me.headers
    assert "strict-transport-security" not in me.headers  # only over https
    assert "content-security-policy" not in c.get("/docs").headers  # Swagger UI loads its own scripts
    assert c.get("/health", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    assert c.get("/health", headers={"X-Request-ID": "<script>"}).headers["x-request-id"] != "<script>"
    # a revalidated page (304) must not pick up the API policy, or browsers apply it to the cached page
    again = c.get("/", headers={"If-None-Match": page.headers["etag"]})
    assert again.status_code == 304 and "content-security-policy" not in again.headers
    https = TestClient(api.app, base_url="https://testserver")
    assert https.get("/health").headers["strict-transport-security"].startswith("max-age=")


def test_allowed_hosts(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch, SIA_ALLOWED_HOSTS="api.example.com")
    assert c.get("/health").status_code == 400  # Host: testserver
    assert TestClient(api.app, base_url="https://api.example.com").get("/health").status_code == 200


def test_local_mode_refuses_proxied_requests(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch, SIA_LOCAL_MODE="1")
    assert c.get("/v1/me").status_code == 200
    assert c.get("/v1/me", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 403


def test_errors_are_hidden_behind_a_request_id(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch)

    def boom(key):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(api.store, "tenant_by_key", boom)
    r = c.get("/v1/me", headers={"Authorization": "Bearer sia_x"})
    assert r.status_code == 500 and "secret" not in r.text
    assert r.json()["request_id"] == r.headers["x-request-id"]


def test_model_calls_are_capped_per_org_and_in_total():
    slots = sec.Slots(per_key=1, total=2)
    with slots.take("a"):
        with pytest.raises(Exception) as e:
            with slots.take("a"):
                pass
        assert e.value.status_code == 429
        with slots.take("b"):
            with pytest.raises(Exception):
                with slots.take("c"):
                    pass
    with slots.take("a"):  # released after use
        pass


def test_window_memory_is_bounded():
    w = sec.Window(limit=5, seconds=60, max_keys=10)
    for i in range(50):
        w.add(f"ip{i}")
    assert len(w._q) <= 11


def test_llm_inputs_are_validated_and_errors_scrubbed():
    for bad in ("../../v1/x", "gemini?key=1", "a b", ",".join(["m"] * 9)):
        with pytest.raises(LLMError):
            LLM("gemini", "k", bad)
    assert LLM("openrouter", "k", "meta-llama/llama-3.1-8b-instruct:free").model.endswith(":free")
    m = LLM("gemini", "AIzaSECRET")
    assert "AIzaSECRET" not in m._clean("Invalid key AIzaSECRET")
