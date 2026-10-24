"""Notifications: the loop's events, POSTed to settings.loop.webhook_url (spec §9.3).

Events: candidate.created, candidate.rejected, version.auto_approved, canary.started, canary.rolled_back,
canary.promoted, loop.paused, budget.exhausted. Each is queued as a "notify" job, so a slow or broken receiver never
holds up a cycle; delivery is retried up to 3 times.

Every request is signed: X-SIA-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<body>" with the project's webhook
secret>, and X-SIA-Timestamp carries the timestamp (reject old ones to stop replays). The URL must be https and resolve
to a public address; redirects are not followed.
"""
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlsplit

import httpx

EVENTS = ("candidate.created", "candidate.rejected", "version.auto_approved", "canary.started", "canary.rolled_back",
          "canary.promoted", "loop.paused", "budget.exhausted")
MAX_ATTEMPTS, TIMEOUT = 3, 5.0
client = None  # tests swap in an httpx.Client with a mock transport


def secret(dev, org, pid, rotate=False):
    """The project's signing secret (created on first use)."""
    with dev._conn(write=True) as c:
        dev._project(c, org, pid)
        row = c.execute("SELECT secret FROM webhooks WHERE project_id=?", (pid,)).fetchone()
        if row and not rotate:
            return row["secret"]
        s = "whsec_" + secrets.token_urlsafe(32)
        c.execute("INSERT INTO webhooks VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET secret=excluded.secret, "
                  "created=excluded.created", (pid, s, time.time()))
        dev._audit(c, pid, None, "webhook.secret", {"rotated": bool(row)})
        return s


def sign(secret_key, timestamp, body):
    return "sha256=" + hmac.new(secret_key.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def verify(secret_key, timestamp, body, signature, max_age=300, now=None):
    """For receivers: check a delivery's signature and age."""
    if abs((now or time.time()) - float(timestamp)) > max_age:
        return False
    return hmac.compare_digest(sign(secret_key, timestamp, body), signature)


def emit(dev, org, pid, event, data):
    """Queue an event for delivery, if the project has a webhook. Never raises: a notification must not break the
    action it reports."""
    from . import jobs
    try:
        if event not in EVENTS or not dev.project(org, pid)["settings"]["loop"]["webhook_url"]:
            return None
        return jobs.enqueue(dev, org, pid, "notify", {"event": event, "data": data, "ts": time.time()})
    except Exception:  # noqa: BLE001
        return None


def deliver(dev, job):
    """The notify job: one POST. A failure queues another attempt, a minute later, up to MAX_ATTEMPTS."""
    from . import jobs
    from .tools.builtin import public_address
    pid, p = job["project_id"], job["payload"]
    org = jobs.org_of(dev, pid)
    url = dev.project(org, pid)["settings"]["loop"]["webhook_url"]
    if not url:
        return {"skipped": "no webhook_url"}
    host = urlsplit(url).hostname or ""
    if client is None and not public_address(host):
        return {"_status": "failed", "error": f"{host} does not resolve to a public address"}
    body = json.dumps({"event": p["event"], "project_id": pid, "data": p["data"], "ts": p["ts"]},
                      default=str, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "User-Agent": "sia-webhooks", "X-SIA-Event": p["event"],
               "X-SIA-Timestamp": ts, "X-SIA-Signature": sign(secret(dev, org, pid), ts, body)}
    try:
        r = (client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)).post(url, content=body, headers=headers)
        ok, detail = 200 <= r.status_code < 300, f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        ok, detail = False, type(e).__name__
    attempt = p.get("attempt", 1)
    if not ok and attempt < MAX_ATTEMPTS:
        jobs.enqueue(dev, org, pid, "notify", {**p, "attempt": attempt + 1}, run_after=time.time() + 60 * attempt)
    return {"delivered": ok, "detail": detail, "attempt": attempt, **({} if ok else {"_status": "failed"})}
