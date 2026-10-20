"""Server hardening: request size limits, per-IP and per-key throttling, concurrency caps for model calls,
security headers, Host allowlist and configuration checks. Used by sia/api.py.

ponytail: every limiter here is in-memory and per process. With N workers or N instances each one counts on its
own, so the effective limit is N times higher. Put a rate limit at the edge as well (load balancer, Cloudflare or
the nginx example in examples/deploy/), and move these counters to Redis if you need exact global limits.
"""
import base64
import hashlib
import logging
import os
import re
import threading
import time
from collections import deque
from contextlib import contextmanager

from starlette.exceptions import HTTPException

log = logging.getLogger("sia.security")

WEAK_ADMIN_KEYS = {"change-me", "changeme", "admin", "password", "secret", "test", "sia"}
MIN_ADMIN_KEY_LEN = 32


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def admin_key_problem(key):
    """Why an admin key is unusable, or None. The admin key creates organizations and changes plans, so a guessable
    one is a full takeover."""
    if not key:
        return "SIA_ADMIN_KEY is not set"
    if key.lower() in WEAK_ADMIN_KEYS or len(key) < MIN_ADMIN_KEY_LEN:
        return f"SIA_ADMIN_KEY is too weak (use {MIN_ADMIN_KEY_LEN}+ random characters, e.g. " \
               "python -c \"import secrets; print(secrets.token_urlsafe(32))\")"
    return None


# --- sliding-window counters ----------------------------------------------------------------------------------
class Window:
    """At most `limit` events per `seconds` for each key. Memory is bounded: past `max_keys` tracked keys, the
    idle ones are dropped first, then the oldest."""

    def __init__(self, limit, seconds=60, max_keys=100_000):
        self.limit, self.seconds, self.max_keys = limit, seconds, max_keys
        self._q, self._lock = {}, threading.Lock()

    def _trim(self, q, now):
        while q and now - q[0] > self.seconds:
            q.popleft()

    def _gc(self, now):
        if len(self._q) <= self.max_keys:
            return
        for k in [k for k, q in self._q.items() if not q or now - q[-1] > self.seconds]:
            del self._q[k]
        while len(self._q) > self.max_keys:
            del self._q[next(iter(self._q))]

    def retry_after(self, key, record=True):
        """0 if the event is allowed (and recorded when `record`), else seconds until it would be."""
        if self.limit <= 0:
            return 0
        now = time.monotonic()
        with self._lock:
            q = self._q.get(key)
            if q is None:
                self._gc(now)
                q = self._q[key] = deque()
            self._trim(q, now)
            if len(q) >= self.limit:
                return max(1, int(self.seconds - (now - q[0])) + 1)
            if record:
                q.append(now)
            return 0

    def blocked(self, key):
        """Seconds left if `key` is at its limit, without recording anything."""
        return self.retry_after(key, record=False)

    def add(self, key):
        if self.limit > 0:
            self.retry_after(key)


def too_many(retry, detail="Too many requests."):
    return HTTPException(429, detail, headers={"Retry-After": str(retry)})


# --- concurrency caps for long model calls ------------------------------------------------------------------
class Slots:
    """Caps concurrent model-calling requests per organization and in total. Model calls can run for minutes on
    the server's worker threads, so without this one organization could occupy every thread."""

    def __init__(self, per_key, total):
        self.per_key, self.total = per_key, total
        self._used, self._all, self._lock = {}, 0, threading.Lock()

    @contextmanager
    def take(self, key):
        with self._lock:
            if self.total > 0 and self._all >= self.total:
                raise too_many(5, "The server is busy with other model calls; retry shortly.")
            if self.per_key > 0 and self._used.get(key, 0) >= self.per_key:
                raise too_many(5, f"At most {self.per_key} model-calling requests may run at once per organization.")
            self._used[key] = self._used.get(key, 0) + 1
            self._all += 1
        try:
            yield
        finally:
            with self._lock:
                self._all -= 1
                self._used[key] -= 1
                if not self._used[key]:
                    del self._used[key]


# --- request body limit (also for chunked uploads) ------------------------------------------------------------
class BodyLimit:
    """ASGI middleware: refuse bodies over `max_bytes`. The Content-Length header is checked up front, and the bytes
    actually received are counted too, so a chunked upload without a Content-Length cannot get around it."""

    def __init__(self, app, max_bytes):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_bytes):
            return await self._reject(send, 413 if length.isdigit() else 400)
        seen = 0

        async def counted():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    # an HTTPException is passed through by FastAPI's body parsing and turned into a 413 response
                    raise HTTPException(413, f"Request body over {self.max_bytes} bytes.")
            return message

        await self.app(scope, counted, send)

    async def _reject(self, send, status):
        detail = f"Request body over {self.max_bytes} bytes." if status == 413 else "Invalid Content-Length."
        body = ('{"detail": "%s"}' % detail).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})


# --- headers ----------------------------------------------------------------------------------------------------
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


def inline_script_hashes(html):
    """CSP hashes of the inline <script> blocks of a page, so they can run without allowing 'unsafe-inline'."""
    blocks = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    return " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(b.encode()).digest()).decode() for b in blocks)


def landing_csp(index_html):
    return ("default-src 'self'; script-src 'self' " + inline_script_hashes(index_html) + "; "
            "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
            "object-src 'none'")


def dashboard_csp(dashboard_html):
    # Razorpay's checkout script and payment window are the only third-party code the dashboard may load
    return ("default-src 'self'; script-src 'self' " + inline_script_hashes(dashboard_html) +
            " https://checkout.razorpay.com https://*.razorpay.com; frame-src https://*.razorpay.com; "
            "connect-src 'self' https://*.razorpay.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data: https://*.razorpay.com; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self' https://*.razorpay.com; object-src 'none'")


COMMON_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), usb=()",
    # allow-popups: Razorpay opens bank and UPI windows that must report back to the checkout
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
    "Cross-Origin-Resource-Policy": "same-origin",
}
HSTS = "max-age=31536000"


def forwarded(headers):
    """True if the request passed through a proxy (used to keep local mode off the network)."""
    return any(h in headers for h in ("x-forwarded-for", "forwarded", "x-real-ip"))
