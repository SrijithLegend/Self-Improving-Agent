"""Razorpay billing with a fake Razorpay: plans change only on a verified, fully paid order."""
import hashlib
import hmac
import importlib
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SECRET, HOOK = "test_key_secret", "test_webhook_secret"


class FakeRazorpay:
    orders = {}

    def __init__(self, *a, **k):
        pass

    def create_order(self, amount, currency, receipt, notes):
        oid = f"order_{len(self.orders) + 1}"
        self.orders[oid] = {"id": oid, "amount": amount, "currency": currency, "status": "created", "amount_paid": 0,
                            "notes": notes}
        return self.orders[oid]

    def order(self, order_id):
        return self.orders[order_id]

    @classmethod
    def pay(cls, oid, amount=None):
        o = cls.orders[oid]
        o.update(status="paid", amount_paid=o["amount"] if amount is None else amount)


def sign(msg, secret):
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {"SIA_DB": str(tmp_path / "b.db"), "SIA_ADMIN_KEY": "admin-key-for-tests-0123456789abcdef", "RAZORPAY_KEY_ID": "rzp_test_x",
                 "RAZORPAY_KEY_SECRET": SECRET, "RAZORPAY_WEBHOOK_SECRET": HOOK}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SIA_LOCAL_MODE", raising=False)
    import sia.api
    api = importlib.reload(sia.api)
    FakeRazorpay.orders = {}
    monkeypatch.setattr(api.billing, "Razorpay", FakeRazorpay)
    c = TestClient(api.app)
    org = c.post("/v1/tenants", json={"name": "acme"}, headers={"X-Admin-Key": "admin-key-for-tests-0123456789abcdef"}).json()
    return api, c, org, {"Authorization": f"Bearer {org['api_key']}"}


def checkout(c, h, plan="pro"):
    o = c.post("/v1/billing/orders", json={"plan": plan}, headers=h)
    assert o.status_code == 201
    return o.json()


def test_paid_order_upgrades_plan_once(env):
    api, c, org, h = env
    assert c.get("/v1/billing", headers=h).json()["plan"] == "free"
    o = checkout(c, h)
    assert o["amount"] == 199900 and o["currency"] == "INR" and o["key_id"] == "rzp_test_x"
    good = {"order_id": o["order_id"], "payment_id": "pay_1", "signature": sign(f"{o['order_id']}|pay_1".encode(),
                                                                                 SECRET)}
    # authorized but not captured yet: no upgrade
    assert c.post("/v1/billing/verify", json=good, headers=h).json()["status"] == "pending"
    assert c.get("/v1/me", headers=h).json()["plan"] == "free"
    FakeRazorpay.pay(o["order_id"])
    r = c.post("/v1/billing/verify", json=good, headers=h).json()
    assert r["status"] == "paid" and r["plan"] == "pro" and r["paid_until"] > time.time() + 29 * 86400
    until = r["paid_until"]
    assert c.post("/v1/billing/verify", json=good, headers=h).json()["paid_until"] == until  # replay: no extension
    assert c.get("/v1/me", headers=h).json()["plan"] == "pro"
    assert c.get("/v1/billing", headers=h).json()["payments"][0]["status"] == "paid"


def test_forged_or_foreign_payments_are_refused(env):
    api, c, org, h = env
    o = checkout(c, h)
    FakeRazorpay.pay(o["order_id"])
    bad = {"order_id": o["order_id"], "payment_id": "pay_1", "signature": "0" * 64}
    assert c.post("/v1/billing/verify", json=bad, headers=h).status_code == 400
    other = c.post("/v1/tenants", json={"name": "b"}, headers={"X-Admin-Key": "admin-key-for-tests-0123456789abcdef"}).json()
    hb = {"Authorization": f"Bearer {other['api_key']}"}
    good = {**bad, "signature": sign(f"{o['order_id']}|pay_1".encode(), SECRET)}
    assert c.post("/v1/billing/verify", json=good, headers=hb).status_code == 404  # someone else's order
    o2 = checkout(c, h)
    FakeRazorpay.pay(o2["order_id"], amount=100)  # underpaid
    sig = sign(f"{o2['order_id']}|pay_2".encode(), SECRET)
    assert c.post("/v1/billing/verify", json={"order_id": o2["order_id"], "payment_id": "pay_2", "signature": sig},
                  headers=h).json()["status"] == "pending"
    assert c.get("/v1/me", headers=h).json()["plan"] == "free"


def test_webhook_activates_and_is_signed(env):
    api, c, org, h = env
    o = checkout(c, h, "team")
    event = {"event": "order.paid", "payload": {"payment": {"entity": {
        "id": "pay_9", "order_id": o["order_id"], "status": "captured", "amount": o["amount"], "currency": "INR"}}}}
    body = json.dumps(event).encode()
    assert c.post("/v1/billing/webhook", content=body, headers={"X-Razorpay-Signature": "nope"}).status_code == 400
    assert c.post("/v1/billing/webhook", content=body,
                  headers={"X-Razorpay-Signature": sign(body, HOOK)}).status_code == 200
    assert c.get("/v1/me", headers=h).json()["plan"] == "team"
    # a signed event for an unknown order is acknowledged but changes nothing
    stray = json.dumps({"event": "order.paid", "payload": {"payment": {"entity": {"order_id": "order_x"}}}}).encode()
    assert c.post("/v1/billing/webhook", content=stray, headers={"X-Razorpay-Signature": sign(stray, HOOK)}).json()


def test_plan_expires_and_billing_can_be_off(env, monkeypatch):
    api, c, org, h = env
    api.store.x("UPDATE tenants SET plan='pro', paid_until=? WHERE id=?", time.time() - 1, org["tenant_id"])
    assert c.get("/v1/me", headers=h).json()["plan"] == "free"  # period over
    assert c.post("/v1/billing/orders", json={"plan": "enterprise"}, headers=h).status_code == 422
    monkeypatch.delenv("RAZORPAY_KEY_SECRET")
    assert c.post("/v1/billing/orders", json={"plan": "pro"}, headers=h).status_code == 503
    assert c.get("/v1/billing", headers=h).json()["enabled"] is False
