"""Razorpay payments. A plan changes only after Razorpay confirms the order is paid: either the checkout callback
(signature checked, then the order re-fetched from Razorpay) or a signed webhook. Card details go straight from
the customer's browser to Razorpay's checkout and never reach this server.

Env: RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET, optional SIA_PRICE_PRO_PAISE,
SIA_PRICE_TEAM_PAISE, SIA_CURRENCY. Use Razorpay test-mode keys (rzp_test_...) until you go live.
"""
import hashlib
import hmac
import os

import httpx

from sia_devtools.errors import ServerError

API = "https://api.razorpay.com/v1"


def settings():
    return {"key_id": os.getenv("RAZORPAY_KEY_ID", ""), "key_secret": os.getenv("RAZORPAY_KEY_SECRET", ""),
            "webhook_secret": os.getenv("RAZORPAY_WEBHOOK_SECRET", ""), "currency": os.getenv("SIA_CURRENCY", "INR"),
            # placeholder prices in the smallest currency unit (paise): hypotheses, not validated pricing
            "prices": {"pro": int(os.getenv("SIA_PRICE_PRO_PAISE", "199900")),
                       "team": int(os.getenv("SIA_PRICE_TEAM_PAISE", "799900"))}}


class Razorpay:
    def __init__(self, key_id, key_secret, http=None):
        self.auth = (key_id, key_secret)
        self.http = http or httpx.Client(timeout=httpx.Timeout(15, connect=5))

    def _call(self, method, path, **kw):
        try:
            r = self.http.request(method, API + path, auth=self.auth, **kw)
        except httpx.HTTPError as e:
            raise ServerError(f"Could not reach Razorpay ({type(e).__name__}).")
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["description"]
            except (ValueError, KeyError, TypeError):
                msg = r.text[:200]
            raise ServerError(f"Razorpay error {r.status_code}: {msg}")
        return r.json()

    def create_order(self, amount, currency, receipt, notes):
        return self._call("POST", "/orders", json={"amount": amount, "currency": currency, "receipt": receipt,
                                                   "notes": notes})

    def order(self, order_id):
        return self._call("GET", f"/orders/{order_id}")


def _sig(secret, message):
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def valid_payment_signature(order_id, payment_id, signature, key_secret):
    """Checkout returns HMAC-SHA256(order_id|payment_id, key_secret)."""
    return bool(signature) and hmac.compare_digest(_sig(key_secret, f"{order_id}|{payment_id}".encode()), signature)


def valid_webhook_signature(body, signature, webhook_secret):
    """Webhooks are signed with HMAC-SHA256 of the raw request body and the webhook secret."""
    return bool(signature and webhook_secret) and hmac.compare_digest(_sig(webhook_secret, body), signature)
