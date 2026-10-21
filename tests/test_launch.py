"""Launch readiness: access requests, contact config, usage caps, HEAD routes, backups, maintenance, doctor."""
import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sia_devtools import cli  # noqa: E402

ADMIN = "admin-key-for-tests-0123456789abcdef"
ADMIN_H = {"X-Admin-Key": ADMIN}


def make(tmp_path, monkeypatch, **env):
    for k in ("SIA_LOCAL_MODE", "SIA_ALLOWED_HOSTS", "SIA_CONTACT_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in {"SIA_DB": str(tmp_path / "s.db"), "SIA_ADMIN_KEY": ADMIN, **env}.items():
        monkeypatch.setenv(k, v)
    import sia.api
    api = importlib.reload(sia.api)
    return api, TestClient(api.app, raise_server_exceptions=False)


def ask(c, email="ada@example.com", **kw):
    return c.post("/v1/access-requests", json={"email": email, "name": "Ada", **kw})


# --- Task 2: request access -------------------------------------------------------------------------------------
def test_access_request_is_stored_once_and_never_reveals_anything(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch)
    r = ask(c, company="Acme", use_case="Support bot")
    assert r.status_code == 202 and r.json() == {"status": "received"}
    assert ask(c, "ADA@example.com ").json() == {"status": "received"}  # same email, still pending: no new row
    rows = api.store.access_requests()
    assert len(rows) == 1 and rows[0]["email"] == "ada@example.com" and rows[0]["company"] == "Acme"


def test_access_request_honeypot_stores_nothing(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch)
    r = ask(c, "bot@example.com", website="http://spam.example")
    assert r.status_code == 202 and r.json() == {"status": "received"}
    assert api.store.access_requests() == []


def test_access_request_validation(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch)
    bad = [{"email": "not-an-email", "name": "A"}, {"email": "a@b.co", "name": "   "}, {"email": "a@b.co"},
           {"email": "a" * 250 + "@b.co", "name": "A"}, {"email": "a@b.co", "name": "A" * 201},
           {"email": "a@b.co", "name": "A", "company": "C" * 201},
           {"email": "a@b.co", "name": "A", "use_case": "u" * 2001},
           {"email": "a@b.co", "name": "A", "plan": "team"}]  # unknown fields are refused too
    for body in bad:
        assert c.post("/v1/access-requests", json=body).status_code == 422, body
    assert api.store.access_requests() == []


def test_access_requests_are_rate_limited_per_ip(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch)
    codes = [ask(c, f"user{i}@example.com").status_code for i in range(6)]
    assert codes == [202] * 5 + [429]
    assert len(api.store.access_requests()) == 5


def test_admin_approves_or_rejects_access_requests(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch)
    ask(c, company="Acme")
    ask(c, "eve@example.com")
    pending = c.get("/v1/admin/access-requests?status=pending", headers=ADMIN_H).json()
    assert [r["email"] for r in pending] == ["ada@example.com", "eve@example.com"]
    ada, eve = pending[0]["id"], pending[1]["id"]

    r = c.post(f"/v1/admin/access-requests/{ada}/approve", headers=ADMIN_H).json()
    assert r["status"] == "approved" and r["org_name"] == "Acme"
    me = c.get("/v1/me", headers={"Authorization": f"Bearer {r['api_key']}"}).json()
    assert me["org_id"] == r["org_id"] and me["name"] == "Acme" and me["scope"] == "full"
    assert c.post(f"/v1/admin/access-requests/{ada}/approve", headers=ADMIN_H).status_code == 404  # decided once

    assert c.post(f"/v1/admin/access-requests/{eve}/reject", headers=ADMIN_H).json()["status"] == "rejected"
    assert c.get("/v1/admin/access-requests?status=pending", headers=ADMIN_H).json() == []
    assert [r["status"] for r in c.get("/v1/admin/access-requests", headers=ADMIN_H).json()] == ["approved",
                                                                                                "rejected"]
    # a decided email can ask again
    assert ask(c, "eve@example.com").status_code == 202 and len(api.store.access_requests("pending")) == 1


def test_access_request_admin_routes_need_the_admin_key(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch)
    ask(c)
    rid = api.store.access_requests()[0]["id"]
    for h in ({}, {"X-Admin-Key": "wrong"}):
        assert c.get("/v1/admin/access-requests", headers=h).status_code == 403
        assert c.post(f"/v1/admin/access-requests/{rid}/approve", headers=h).status_code == 403
        assert c.post(f"/v1/admin/access-requests/{rid}/reject", headers=h).status_code == 403
    assert api.store.access_requests()[0]["status"] == "pending"


def test_cli_admin_commands(tmp_path, monkeypatch, capsys):
    api, c = make(tmp_path, monkeypatch, SIA_BASE_URL="http://testserver")
    monkeypatch.setattr(cli.httpx, "request", lambda method, url, **kw: c.request(method, url, headers=kw["headers"]))
    ask(c, company="Acme")
    assert cli.main(["admin", "requests", "--status", "pending"]) == 0
    rid = api.store.access_requests()[0]["id"]
    assert rid in capsys.readouterr().out
    assert cli.main(["admin", "approve", rid]) == 0
    out = capsys.readouterr().out
    assert "approved" in out and "sia_" in out
    assert cli.main(["admin", "reject", rid]) == 4  # already decided
    monkeypatch.delenv("SIA_ADMIN_KEY")
    assert cli.main(["admin", "requests"]) == 4


# --- Task 3: contact address ------------------------------------------------------------------------------------
def test_site_config_without_contact_email(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch)
    r = c.get("/site-config.json")
    assert r.status_code == 200 and r.json() == {"contact_email": None}
    assert r.headers["cache-control"] == "public, max-age=300"


def test_site_config_with_contact_email(tmp_path, monkeypatch):
    api, c = make(tmp_path, monkeypatch, SIA_CONTACT_EMAIL=" hello@example.com ")
    assert c.get("/site-config.json").json() == {"contact_email": "hello@example.com"}
