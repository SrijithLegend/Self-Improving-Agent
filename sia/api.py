"""HTTP API: SIA DevTools (projects, runs, feedback, datasets, evaluations, versions) and the managed agents API.

Auth:  Authorization: Bearer <sia api key>. "full" keys can do everything in their organization; "ingest" keys
       (for production apps) can only send runs and feedback and read the active prompt.
LLM:   only the managed agents routes call a model, with the caller's own key: X-LLM-Provider, X-LLM-Key,
       optional X-LLM-Model, X-LLM-Base-URL. Keys sent in X-LLM-Key are used for the request only and never stored.
       DevTools routes never send your data to a model provider.
Local: SIA_LOCAL_MODE=1 (used by `sia dashboard`) skips keys and serves the local org, to loopback clients only.
"""
import csv
import io
import json
import logging
import os
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from sia_devtools.errors import NotFound, QuotaExceeded, SIAError, ValidationError
from sia_devtools.store import LOCAL_ORG, DevStore

from sia_loop import jobs

from . import billing
from . import security as sec
from .agent import Agent
from .llm import LLM, LLMError
from .store import PAID_PERIOD_DAYS, PLANS, Store

app = FastAPI(title="SIA DevTools API", version="0.1.0",
              description="A release gate for prompt changes: capture runs, verify feedback into "
                          "datasets, evaluate prompt versions and release them through an explicit approval step.")
DB = os.getenv("SIA_DB", "sia.db")
LOCAL_MODE = os.getenv("SIA_LOCAL_MODE") == "1"
store, dev = Store(DB), DevStore(DB, custom_regex=LOCAL_MODE)  # hosted: named redaction rules only
MAX_BODY = sec.env_int("SIA_MAX_BODY_BYTES", 4 * 1024 * 1024)
# Throttles (in-memory, per process: see sia/security.py). 0 turns one off.
RATE_LIMIT = sec.env_int("SIA_RATE_LIMIT", 120)  # requests per organization per minute
org_hits = sec.Window(RATE_LIMIT, 60)
ip_hits = sec.Window(sec.env_int("SIA_IP_RATE_LIMIT", 600), 60)  # requests per client IP per minute
auth_fails = sec.Window(sec.env_int("SIA_AUTH_FAIL_LIMIT", 20), 600)  # bad keys per IP per 10 min, then 429
access_hits = sec.Window(5, 3600)  # access requests per client IP per hour
llm_slots = sec.Slots(sec.env_int("SIA_LLM_CONCURRENCY_PER_ORG", 2), sec.env_int("SIA_LLM_CONCURRENCY", 16))
log, seclog = logging.getLogger("sia.access"), logging.getLogger("sia.security")
if not logging.getLogger("sia").handlers:  # without a handler, Python drops INFO: the access lines never appeared
    _out = logging.StreamHandler(sys.stdout)
    _out.setFormatter(logging.Formatter("%(message)s"))  # the lines are JSON already
    logging.getLogger("sia").addHandler(_out)
    logging.getLogger("sia").setLevel(logging.INFO)
    logging.getLogger("sia").propagate = False
DASHBOARD = Path(__file__).with_name("dashboard.html")
DASHBOARD_HTML = DASHBOARD.read_text(encoding="utf-8")
DASHBOARD_CSP = sec.dashboard_csp(DASHBOARD_HTML)
# landing page, mounted at / after every route (the Docker image sets SIA_WEB_DIR)
WEB = Path(os.getenv("SIA_WEB_DIR") or Path(__file__).resolve().parents[1] / "web")
LANDING_CSP = sec.landing_csp((WEB / "index.html").read_bytes().decode("utf-8")) if (WEB / "index.html").is_file() \
    else sec.API_CSP
NO_CSP = ("/docs", "/redoc")  # FastAPI's API docs load their own scripts from a CDN

# outermost first: Host allowlist, then the body limit, then guard_and_log below
if os.getenv("SIA_ALLOWED_HOSTS"):
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[h.strip() for h in os.environ["SIA_ALLOWED_HOSTS"]
                       .split(",") if h.strip()] + ["127.0.0.1", "localhost"])  # localhost: the health check
app.add_middleware(sec.BodyLimit, max_bytes=MAX_BODY)


def sentry_scrub(event, hint=None):
    """Keep only the method and path of a request: headers carry API, admin and model keys, bodies carry customer
    data, query strings can carry either."""
    req = event.get("request")
    if isinstance(req, dict):
        event["request"] = {k: req[k] for k in ("method", "url") if k in req}
        if "url" in event["request"]:
            event["request"]["url"] = str(event["request"]["url"]).split("?", 1)[0]
    event.pop("user", None)
    for exc in (event.get("exception") or {}).get("values") or []:  # frame locals hold keys and run inputs
        for frame in (exc.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
    return event


sentry = None
if os.getenv("SENTRY_DSN"):  # optional error reporting (pip install "sia-devtools[sentry]")
    try:
        import sentry_sdk as sentry
        sentry.init(dsn=os.environ["SENTRY_DSN"], send_default_pii=False, traces_sample_rate=0,
                    default_integrations=False, before_send=sentry_scrub,
                    include_local_variables=False, max_request_body_size="never",
                    environment=os.getenv("SENTRY_ENVIRONMENT", "production"))
    except ImportError:
        sentry = None
        logging.getLogger("sia.security").warning("SENTRY_DSN is set but sentry-sdk isn't installed; "
                                                  "pip install 'sia-devtools[sentry]'")
if not LOCAL_MODE and sec.admin_key_problem(os.getenv("SIA_ADMIN_KEY", "")):
    seclog.warning("admin API disabled: %s", sec.admin_key_problem(os.getenv("SIA_ADMIN_KEY", "")))


def client_ip(request: Request):
    # behind a reverse proxy this is the real client only if uvicorn trusts the proxy (SIA_TRUSTED_PROXIES)
    return request.client.host if request.client else "unknown"


def security_event(request: Request, event, **extra):
    seclog.warning(json.dumps({"ts": round(time.time(), 3), "event": event, "ip": client_ip(request),
                               "path": request.url.path, "request_id": getattr(request.state, "request_id", None),
                               **extra}))


@app.exception_handler(SIAError)
def sia_error(request: Request, e: SIAError):
    return JSONResponse({"detail": str(e)}, e.status)


@app.middleware("http")
async def guard_and_log(request: Request, call_next):
    """Throttle per client IP, add security headers and a request id, hide internal errors, and log one JSON line
    per request (no headers, no query strings, no bodies: those can hold keys or personal data)."""
    t0 = time.perf_counter()
    rid = request.headers.get("x-request-id", "")
    request.state.request_id = rid if sec.SAFE_REQUEST_ID.match(rid) else uuid.uuid4().hex[:16]
    path = request.url.path
    retry = 0 if LOCAL_MODE or path == "/health" else ip_hits.retry_after(client_ip(request))
    if retry:
        response = JSONResponse({"detail": "Too many requests from this address."}, 429,
                                headers={"Retry-After": str(retry)})
    else:
        try:
            response = await call_next(request)
        except Exception:  # never send a stack trace to the client; the request id finds it in the log
            log.exception("unhandled error, request_id=%s", request.state.request_id)
            if sentry:
                with sentry.new_scope() as scope:
                    scope.set_tag("request_id", request.state.request_id)
                    scope.set_context("request", {"method": request.method, "path": path})
                    sentry.capture_exception()
            response = JSONResponse({"detail": "Internal server error.", "request_id": request.state.request_id}, 500)
    h = response.headers
    h.update(sec.COMMON_HEADERS)
    h["X-Request-ID"] = request.state.request_id
    if request.url.scheme == "https":
        h["Strict-Transport-Security"] = sec.HSTS
    # A 304 has no content type, and browsers copy its headers onto the cached page: giving it the API policy
    # would block the page's own scripts and styles on every revisit. It keeps the policy it was served with.
    if "content-security-policy" not in h and not path.startswith(NO_CSP) and response.status_code != 304:
        h["Content-Security-Policy"] = LANDING_CSP if h.get("content-type", "").startswith("text/html") \
            else sec.API_CSP
    if path.startswith("/v1/"):
        h.setdefault("Cache-Control", "no-store")  # API data may be private: never keep it in shared caches
    log.info(json.dumps({"ts": round(time.time(), 3), "method": request.method, "path": path,
                         "status": response.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1),
                         "org": getattr(request.state, "org", None), "request_id": request.state.request_id}))
    return response


# --- dependencies ------------------------------------------------------------
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def key_auth(request: Request, authorization: str = Header(default="")):
    """Any valid key (full or ingest scope)."""
    if LOCAL_MODE:
        # no keys here, so refuse anything not from and addressed to this machine (the Host check stops DNS
        # rebinding: a web page on evil.example pointing its DNS at 127.0.0.1 still sends Host: evil.example;
        # forwarding headers mean a reverse proxy put this machine on the network)
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if (request.client.host if request.client else "") not in LOOPBACK or host not in LOOPBACK | {"testserver"} \
                or sec.forwarded(request.headers):
            raise HTTPException(403, "Local mode only serves this machine.")
        t = {"id": LOCAL_ORG, "name": "local", "plan": "local", "key_id": "local", "scope": "full"}
    else:
        ip = client_ip(request)
        if retry := auth_fails.blocked(ip):
            raise sec.too_many(retry, "Too many invalid API keys from this address; try again later.")
        key = authorization.removeprefix("Bearer ").strip()
        t = store.tenant_by_key(key) if key and len(key) <= 200 else None
        if not t:
            auth_fails.add(ip)
            security_event(request, "invalid_api_key")
            raise HTTPException(401, "Invalid, revoked or missing API key (Authorization: Bearer <key>).")
    request.state.org = t["id"]
    if not LOCAL_MODE and (retry := org_hits.retry_after(t["id"])):
        raise sec.too_many(retry, "Rate limit exceeded.")
    return t


def tenant(t=Depends(key_auth)):
    """A full-scope key: required for everything except ingesting runs/feedback and reading the active prompt."""
    if t["scope"] != "full":
        raise HTTPException(403, "This key has 'ingest' scope; this action needs a full-scope key.")
    return t


def actor(t, name=None):
    return name or f"key:{t['key_id']}"


def limits(t):
    return PLANS.get(t["plan"])  # None in local mode: no limits


def month_start():
    return datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def next_month_start():
    d = datetime.fromtimestamp(month_start(), timezone.utc)
    return d.replace(year=d.year + d.month // 12, month=d.month % 12 + 1)


def run_cap(lim):
    """Runs a month before ingest is refused: the included runs, or up to hard_cap times that on paid plans."""
    return int(lim["runs_per_month"] * (lim.get("hard_cap", 1) if lim["overage"] else 1))


def require_admin(request: Request, x_admin_key: str):
    admin = os.getenv("SIA_ADMIN_KEY", "")
    if problem := sec.admin_key_problem(admin):
        raise HTTPException(503, f"The admin API is disabled: {problem}.")
    ip = client_ip(request)
    if retry := auth_fails.blocked(ip):
        raise sec.too_many(retry, "Too many invalid keys from this address; try again later.")
    if not secrets.compare_digest(x_admin_key.encode(), admin.encode()):
        auth_fails.add(ip)
        security_event(request, "invalid_admin_key")
        raise HTTPException(403, "Admin key required.")


def llm(x_llm_provider: str = Header(default="gemini"), x_llm_key: str = Header(default=""),
        x_llm_model: str = Header(default=""), x_llm_base_url: str = Header(default="")):
    # Self-hosters may set a server-side default key (e.g. their own free Gemini key).
    key = x_llm_key or (os.getenv("SIA_DEFAULT_LLM_KEY", "") if x_llm_provider == "gemini" else "")
    try:
        return LLM(x_llm_provider, key, x_llm_model or None, x_llm_base_url or None)
    except LLMError as e:
        raise HTTPException(400, str(e))


def owned_agent(agent_id: str, t=Depends(tenant)):
    a = store.agent(agent_id, t["id"])
    if not a:
        raise HTTPException(404, "Agent not found.")
    return a


def llm_quota(org_id):
    """Refuse (402) once the organization has used its plan's LLM calls for this month. Checked before an action
    starts, so one action can run past the cap by its own calls."""
    org = store.tenant(org_id)
    lim = PLANS.get(org["plan"]) if org else None  # local mode has no tenant row: no limit
    if lim and store.llm_calls_since(org_id, month_start()) >= lim["llm_calls_per_month"]:
        raise QuotaExceeded(f"The {org['plan']} plan includes {lim['llm_calls_per_month']:,} LLM calls a month; "
                            f"they reset on {next_month_start():%d %B %Y} (UTC).")


def metered(request: Request, a, model, fn, *args):
    """Run an LLM-using action, record its LLM calls for billing (even if it fails), add llm_calls. Capped per
    organization and in total, since these hold a worker thread for as long as the model takes."""
    llm_quota(a["tenant_id"])
    with llm_slots.take(a["tenant_id"]):
        try:
            return {**call(fn, *args), "llm_calls": model.calls}
        finally:
            store.record_usage(a["tenant_id"], a["id"], request.url.path.rsplit("/", 1)[-1], model.calls)


def call(fn, *args, **kwargs):
    """Map engine errors onto HTTP status codes."""
    try:
        return fn(*args, **kwargs)
    except LLMError as e:
        raise HTTPException(502, f"LLM provider error: {e}")
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'"))
    except ValueError as e:
        raise HTTPException(400, str(e))


# --- request bodies: managed agents -------------------------------------------
class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AccessRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    name: str = Field(max_length=200, pattern=r"\S")
    company: str = Field("", max_length=200)
    use_case: str = Field("", max_length=2000)
    website: str = Field("", max_length=500, description="Leave empty. It's hidden on the form: bots fill it in.")


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=500, description="e.g. 'Indian income-tax advisory'")
    instructions: str = Field("", max_length=20000, description="Role, tone, constraints, output format.")
    rubric: str = Field("", max_length=5000, description="What a great answer looks like; used for self-grading.")


class AgentPatch(BaseModel):
    name: str | None = Field(None, max_length=200)
    domain: str | None = Field(None, max_length=500)
    instructions: str | None = Field(None, max_length=20000)
    rubric: str | None = Field(None, max_length=5000)


class DocIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=2_000_000)


class ExampleIn(BaseModel):
    input: str = Field(min_length=1, max_length=20000)
    expected: str = Field(min_length=1, max_length=20000, description="Reference answer or grading notes.")


class RunIn(BaseModel):
    task: str = Field(min_length=1, max_length=20000)
    learn: bool = Field(True, description="Self-critique, revise and learn. False = one fast call, no learning.")
    max_revisions: int = Field(2, ge=0, le=5)


class FeedbackIn(BaseModel):
    episode_id: str
    rating: int = Field(ge=1, le=5)
    correction: str | None = Field(None, max_length=20000, description="The answer it should have given. Stored "
                                                                       "as a pending example until verified.")
    comment: str | None = Field(None, max_length=5000)


class StudyIn(BaseModel):
    rounds: int = Field(3, ge=1, le=20)


class EvaluateIn(BaseModel):
    max_examples: int = Field(8, ge=1, le=30)
    trials: int = Field(1, ge=1, le=3)


class ImproveIn(BaseModel):
    max_examples: int = Field(8, ge=1, le=30)
    trials: int = Field(1, ge=1, le=3, description="Answers per example and playbook, averaged; more = less noise.")
    activate: bool = Field(False, description="Activate the candidate if the verdict is 'improved'. Otherwise it is "
                                              "only stored; activate it later with /playbooks/{version}/activate.")


# --- request bodies: DevTools --------------------------------------------------
class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeyIn(Strict):
    name: str = Field("key", min_length=1, max_length=100)
    scope: Literal["full", "ingest"] = "full"


class PlanIn(Strict):
    plan: Literal["free", "pro", "team"]


class ProjectIn(Strict):
    name: str = Field(min_length=1, max_length=64)
    settings: dict | None = None


class ProjectPatch(Strict):
    settings: dict = Field(description="Any of: redact (rule names or regexes), retention_days, policy, locked_text.")


class TraceIn(Strict):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    parent_id: str | None = None
    input: Any = None
    output: Any = None
    status: Literal["running", "success", "error"] = "success"
    error: str | None = None
    started: float | None = None
    ended: float | None = None
    duration_ms: float | None = None
    model: str | None = None
    provider: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost: float | None = None
    tags: list[str] = []
    metadata: dict = {}
    scores: dict[str, float] = {}
    version_name: str | None = None
    version: int | None = None


class RunsIn(Strict):
    runs: list[TraceIn] = Field(min_length=1, max_length=500)


class OutcomeIn(Strict):
    ok: bool
    source: str = Field(min_length=1, max_length=100, description="Which system reported it, e.g. 'refunds-db'.")


class FeedbackIn2(Strict):
    id: str | None = Field(None, max_length=64, description="Client-chosen id; resending it is a no-op.")
    rating: Literal[-1, 1]
    correction: Any = None
    category: str | None = Field(None, max_length=100)
    explanation: str | None = Field(None, max_length=5000)
    reviewer: str | None = Field(None, max_length=200)


class ReviewIn(Strict):
    status: Literal["verified", "rejected"]
    dataset_id: str | None = None
    expected_output: Any = None
    note: str | None = Field(None, max_length=2000)
    reviewer: str | None = Field(None, max_length=200)
    critical: bool = False


class DatasetIn(Strict):
    name: str = Field(min_length=1, max_length=64)


class DatasetExampleIn(Strict):
    input: Any
    expected_output: Any = None
    metadata: dict = {}
    critical: bool = False


class DatasetExamplesIn(Strict):
    examples: list[DatasetExampleIn] = Field(min_length=1, max_length=1000)
    allow_duplicates: bool = Field(False, description="Add examples even if an identical (input, expected_output) "
                                                      "is already in the dataset.")


class ExamplePatch(Strict):
    input: Any = None
    expected_output: Any = None
    metadata: dict | None = None
    critical: bool | None = None


class ResultIn(Strict):
    example_id: int
    output: Any = None
    scores: dict[str, float] = {}
    error: str | None = None
    duration_ms: float | None = None


class EvaluationIn(Strict):
    id: str | None = Field(None, max_length=64)
    name: str | None = Field(None, max_length=200)
    dataset_id: str
    dataset_version: int
    version_name: str | None = None
    version: int | None = None
    prompt_sha: str | None = Field(None, max_length=64)
    config: dict
    results: list[ResultIn] = Field(max_length=50_000)


class CompareIn(Strict):
    baseline_eval_id: str
    candidate_eval_id: str
    policy: dict | None = None


class VersionIn(Strict):
    name: str = Field("system_prompt", min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=50_000)
    parent_version: int | None = None
    lessons: list | None = None
    patterns: list | None = None
    note: str | None = Field(None, max_length=2000)


class ApproveIn(Strict):
    comparison_id: str | None = None
    override_reason: str | None = Field(None, max_length=2000)
    reviewer: str | None = Field(None, max_length=200)


class RejectIn(Strict):
    reason: str | None = Field(None, max_length=2000)
    reviewer: str | None = Field(None, max_length=200)


class ActivateIn(Strict):
    reviewer: str | None = Field(None, max_length=200)


class OrderIn(Strict):
    plan: Literal["pro", "team"]


class VerifyIn(Strict):
    order_id: str = Field(max_length=64)
    payment_id: str = Field(max_length=64)
    signature: str = Field(max_length=128)


# --- service routes ------------------------------------------------------------
@app.api_route("/dashboard", methods=["GET", "HEAD"], include_in_schema=False)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML, headers={"Content-Security-Policy": DASHBOARD_CSP, "Cache-Control": "no-store"})


@app.api_route("/health", methods=["GET", "HEAD"])  # HEAD: uptime monitors
def health():
    try:
        store.q("SELECT 1")
    except Exception:
        return JSONResponse({"ok": False, "db": False}, 503)
    return {"ok": True, "db": True, "local_mode": LOCAL_MODE}


@app.get("/site-config.json", include_in_schema=False)
def site_config():
    """Public settings the static pages read once (web/site.js): the contact address, or null."""
    return JSONResponse({"contact_email": os.getenv("SIA_CONTACT_EMAIL", "").strip() or None},
                        headers={"Cache-Control": "public, max-age=300"})


@app.post("/v1/tenants", status_code=201)
def create_tenant(body: TenantIn, request: Request, x_admin_key: str = Header(default="")):
    require_admin(request, x_admin_key)
    return store.create_tenant(body.name)  # the api_key is shown only once


@app.post("/v1/admin/orgs/{org_id}/plan")
def set_plan(org_id: str, body: PlanIn, request: Request, x_admin_key: str = Header(default="")):
    """Payment integration boundary: call this from your billing provider's webhook once a payment is confirmed.
    SIA never handles card data and never assumes a payment succeeded."""
    require_admin(request, x_admin_key)
    if not store.set_plan(org_id, body.plan):
        raise HTTPException(404, "Organization not found.")
    return {"org_id": org_id, "plan": body.plan, "limits": PLANS[body.plan]}


@app.delete("/v1/admin/orgs/{org_id}", status_code=204)
def delete_org(org_id: str, request: Request, x_admin_key: str = Header(default="")):
    """Delete an organization and all its data in one transaction (usage and payments are kept, anonymised)."""
    require_admin(request, x_admin_key)
    if not store.delete_org(org_id):
        raise HTTPException(404, "Organization not found.")
    security_event(request, "org_deleted", org_id=org_id)


# --- access requests: manual onboarding until there is self-serve signup ----------------
@app.post("/v1/access-requests", status_code=202)
def request_access(body: AccessRequestIn, request: Request):
    """Ask for hosted access. The answer is always the same, so it never reveals whether an email is already known."""
    if retry := access_hits.retry_after(client_ip(request)):
        raise sec.too_many(retry, "Too many access requests from this address; try again later.")
    if body.website:  # the hidden honeypot field: a bot filled it in, so store nothing
        security_event(request, "access_request_honeypot")
    else:
        store.add_access_request(body.email, body.name, body.company, body.use_case)
    return {"status": "received"}


@app.get("/v1/admin/access-requests")
def list_access_requests(request: Request, status: Literal["pending", "approved", "rejected"] | None = None,
                         x_admin_key: str = Header(default="")):
    require_admin(request, x_admin_key)
    security_event(request, "access_requests_listed", status=status)
    return store.access_requests(status)


@app.post("/v1/admin/access-requests/{request_id}/{decision}")
def decide_access_request(request_id: str, decision: Literal["approve", "reject"], request: Request,
                          x_admin_key: str = Header(default="")):
    """approve creates the organization and a full-scope key, returned once; reject only marks the request."""
    require_admin(request, x_admin_key)
    r = store.decide_access_request(request_id, decision == "approve")
    if not r:
        raise HTTPException(404, "No pending access request with that id.")
    security_event(request, f"access_request_{r['status']}", access_request=request_id, org_id=r.get("org_id"))
    return r


# --- billing (Razorpay) ------------------------------------------------------------
def razorpay():
    cfg = billing.settings()
    if LOCAL_MODE or not (cfg["key_id"] and cfg["key_secret"]):
        raise HTTPException(503, "Payments are not configured on this server (set RAZORPAY_KEY_ID and "
                                 "RAZORPAY_KEY_SECRET).")
    return billing.Razorpay(cfg["key_id"], cfg["key_secret"])


@app.get("/v1/billing")
def billing_status(t=Depends(tenant)):
    cfg = billing.settings()
    org = store.tenant(t["id"]) or {}
    return {"enabled": bool(cfg["key_id"] and cfg["key_secret"]) and not LOCAL_MODE, "key_id": cfg["key_id"],
            "currency": cfg["currency"], "prices": cfg["prices"], "plans": PLANS, "plan": t["plan"],
            "paid_until": org.get("paid_until"), "period_days": 30, "payments": store.payments(t["id"])}


@app.post("/v1/billing/orders", status_code=201)
def create_order(body: OrderIn, t=Depends(tenant)):
    """Create a Razorpay order for one 30-day period of a plan. The dashboard opens Razorpay Checkout with it."""
    rzp, cfg = razorpay(), billing.settings()
    amount = cfg["prices"][body.plan]
    order = rzp.create_order(amount, cfg["currency"], f"sia-{t['id'][:12]}-{int(time.time())}",
                             {"org_id": t["id"], "plan": body.plan})
    store.add_payment(order["id"], t["id"], body.plan, amount, cfg["currency"])
    return {"order_id": order["id"], "amount": amount, "currency": cfg["currency"], "key_id": cfg["key_id"],
            "plan": body.plan, "org_name": t["name"]}


def _confirm(order_id, payment_id, rzp_order):
    """Activate the plan only if Razorpay says this order is fully paid in the amount we asked for."""
    pay = store.payment(order_id)
    if rzp_order.get("status") != "paid" or rzp_order.get("amount_paid") != pay["amount"]             or rzp_order.get("currency") != pay["currency"]:
        return False
    store.mark_paid(order_id, payment_id)
    return True


@app.post("/v1/billing/verify")
def verify_payment(body: VerifyIn, t=Depends(tenant)):
    """Checkout callback. The signature proves the ids came from Razorpay; the order is then re-fetched so a
    payment that is authorized but not yet captured never activates a plan."""
    pay = store.payment(body.order_id)
    if not pay or pay["org_id"] != t["id"]:
        raise NotFound("Order not found.")
    if not billing.valid_payment_signature(body.order_id, body.payment_id, body.signature,
                                           billing.settings()["key_secret"]):
        raise ValidationError("Payment signature does not match.")
    if pay["status"] != "paid" and not _confirm(body.order_id, body.payment_id, razorpay().order(body.order_id)):
        return {"status": "pending", "detail": "Razorpay has not confirmed the payment yet; the plan switches as "
                                                "soon as it does (via webhook)."}
    org = store.tenant(t["id"])
    return {"status": "paid", "plan": org["plan"], "paid_until": org["paid_until"]}


@app.post("/v1/billing/webhook", include_in_schema=False)
async def razorpay_webhook(request: Request):
    """Point a Razorpay webhook (events: order.paid, payment.captured) at this URL."""
    secret = billing.settings()["webhook_secret"]
    if not secret:
        raise HTTPException(503, "RAZORPAY_WEBHOOK_SECRET is not set.")
    body = await request.body()
    if not billing.valid_webhook_signature(body, request.headers.get("x-razorpay-signature", ""), secret):
        security_event(request, "invalid_webhook_signature")
        raise HTTPException(400, "Invalid webhook signature.")
    try:
        event = json.loads(body)
    except ValueError:
        event = None
    if not isinstance(event, dict):
        raise HTTPException(400, "Webhook body is not a JSON object.")
    entity = (event.get("payload", {}).get("payment") or {}).get("entity") or {}
    order_id = entity.get("order_id")
    pay = store.payment(order_id) if order_id else None
    if event.get("event") in ("order.paid", "payment.captured") and pay and entity.get("status") == "captured"             and entity.get("amount") == pay["amount"] and entity.get("currency") == pay["currency"]:
        store.mark_paid(order_id, entity.get("id"))
    return {"ok": True}  # 2xx for events we ignore too, so Razorpay doesn't retry them


@app.get("/v1/plans")
def plans():
    """Public: plan limits and prices (smallest currency unit, per 30-day period), for the landing page."""
    cfg = billing.settings()
    return {"beta": True, "currency": cfg["currency"], "period_days": PAID_PERIOD_DAYS,
            "plans": {name: {**lim, "price": cfg["prices"].get(name, 0)} for name, lim in PLANS.items()}}


@app.get("/v1/me")
def me(t=Depends(key_auth)):
    return {"org_id": t["id"], "name": t["name"], "plan": t["plan"], "limits": limits(t), "scope": t["scope"],
            "key_id": t["key_id"], "local_mode": LOCAL_MODE}


@app.get("/v1/keys")
def list_keys(t=Depends(tenant)):
    return [] if LOCAL_MODE else store.keys(t["id"])


@app.post("/v1/keys", status_code=201)
def create_key(body: KeyIn, t=Depends(tenant)):
    if LOCAL_MODE:
        raise HTTPException(400, "Local mode has no API keys.")
    return store.create_key(t["id"], body.name, body.scope)  # the api_key is shown only once


@app.post("/v1/keys/rotate")
def rotate_key(t=Depends(tenant)):
    if LOCAL_MODE:
        raise HTTPException(400, "Local mode has no API keys.")
    return store.rotate_key(t["id"], t["key_id"])  # the new key is shown once; the old one stops working now


@app.post("/v1/keys/{key_id}/revoke")
def revoke_key(key_id: str, t=Depends(tenant)):
    if not store.revoke_key(t["id"], key_id):
        raise HTTPException(404, "Key not found or already revoked.")
    return {"id": key_id, "revoked": True}


@app.get("/v1/usage")
def usage(days: int = 30, t=Depends(tenant)):
    days = min(max(days, 1), 366)
    lim = limits(t)
    used = store.units_since(t["id"], "runs", month_start())
    included = lim["runs_per_month"] if lim else None
    return {"days": days, "plan": t["plan"], "limits": lim, **store.usage(t["id"], time.time() - days * 86400),
            "runs_this_month": {"used": used, "included": included,
                                "overage": max(0, used - included) if included is not None else 0,
                                "hard_cap_runs": run_cap(lim) if lim else None,
                                "percent_used": round(100 * used / included, 1) if included else None,
                                "resets": next_month_start().timestamp()}}


# --- DevTools: projects ----------------------------------------------------------
@app.post("/v1/projects")
def create_project(body: ProjectIn, t=Depends(key_auth)):
    """Get or create a project by name. Ingest keys can only look up existing projects."""
    full = t["scope"] == "full"
    lim = limits(t)
    if full and lim and not any(p["name"] == body.name for p in dev.projects(t["id"])) \
            and dev.count_projects(t["id"]) >= lim["projects"]:
        raise QuotaExceeded(f"The {t['plan']} plan includes {lim['projects']} project(s).")
    return dev.create_project(t["id"], body.name, body.settings, create=full, actor=actor(t))


@app.get("/v1/projects")
def list_projects(t=Depends(tenant)):
    return dev.projects(t["id"])


@app.get("/v1/projects/{project_id}")
def get_project(project_id: str, t=Depends(tenant)):
    return dev.project(t["id"], project_id)


@app.patch("/v1/projects/{project_id}")
def update_project(project_id: str, body: ProjectPatch, t=Depends(tenant)):
    return dev.update_project(t["id"], project_id, body.settings, actor=actor(t))


@app.delete("/v1/projects/{project_id}", status_code=204)
def delete_project(project_id: str, t=Depends(tenant)):
    """Delete the project and every run, feedback item, dataset, evaluation, version and audit row in it."""
    dev.delete_project(t["id"], project_id)
    store.x("UPDATE usage SET project_id=NULL WHERE project_id=?", project_id)  # usage stays, anonymised


@app.get("/v1/projects/{project_id}/overview")
def overview(project_id: str, t=Depends(tenant)):
    return dev.overview(t["id"], project_id)


@app.get("/v1/projects/{project_id}/audit")
def audit(project_id: str, limit: int = 100, t=Depends(tenant)):
    return dev.audit(t["id"], project_id, limit)


# --- DevTools: runs and feedback --------------------------------------------------
@app.post("/v1/projects/{project_id}/runs", status_code=202)
def ingest_runs(project_id: str, body: RunsIn, t=Depends(key_auth)):
    """Insert or update runs by id; resending a batch is safe."""
    lim = limits(t)
    if lim and store.units_since(t["id"], "runs", month_start()) + len(body.runs) > run_cap(lim):
        cap = f"{lim['runs_per_month']:,} runs" + (f", up to {run_cap(lim):,} with overage" if lim["overage"] else "")
        raise QuotaExceeded(f"Monthly run limit of the {t['plan']} plan reached ({cap}). It resets on "
                            f"{next_month_start():%d %B %Y} (UTC).")
    n = dev.log_runs(t["id"], project_id, [r.model_dump() for r in body.runs],
                     retention_days=lim["retention_days"] if lim else None)
    store.record_usage(t["id"], None, "runs", 0, units=n, project_id=project_id)
    return {"accepted": n}


def _run_filters(status, tag, name, q, since, until, root_only):
    return dict(status=status, tag=tag, name=name, q=q, since=since, until=until, root_only=root_only)


@app.get("/v1/projects/{project_id}/runs")
def list_runs(project_id: str, status: str | None = None, tag: str | None = None, name: str | None = None,
              q: str | None = None, since: float | None = None, until: float | None = None, root_only: bool = False,
              limit: int = 50, offset: int = 0, t=Depends(tenant)):
    return dev.runs(t["id"], project_id, limit, offset, **_run_filters(status, tag, name, q, since, until, root_only))


@app.get("/v1/projects/{project_id}/runs/export")
def export_runs(project_id: str, status: str | None = None, tag: str | None = None, name: str | None = None,
                q: str | None = None, since: float | None = None, until: float | None = None,
                root_only: bool = False, t=Depends(tenant)):
    rows = dev.export_runs(t["id"], project_id, **_run_filters(status, tag, name, q, since, until, root_only))
    return StreamingResponse((json.dumps(r) + "\n" for r in rows), media_type="application/x-ndjson",
                             headers={"Content-Disposition": "attachment; filename=runs.jsonl"})


@app.get("/v1/projects/{project_id}/runs/{run_id}")
def get_run(project_id: str, run_id: str, t=Depends(tenant)):
    return dev.run(t["id"], project_id, run_id)


@app.post("/v1/projects/{project_id}/runs/{run_id}/feedback", status_code=201)
def add_feedback(project_id: str, run_id: str, body: FeedbackIn2, t=Depends(key_auth)):
    """Stored as pending. It never enters a dataset or an improvement until a full-scope key verifies it."""
    return dev.add_feedback(t["id"], project_id, run_id, **body.model_dump())


@app.post("/v1/projects/{project_id}/runs/{run_id}/outcome")
def record_outcome(project_id: str, run_id: str, body: OutcomeIn, t=Depends(key_auth)):
    """An outcome signal from your own system (not a person): did what the run decided actually work out?"""
    return dev.record_outcome(t["id"], project_id, run_id, body.ok, body.source)


@app.get("/v1/projects/{project_id}/feedback")
def list_feedback(project_id: str, status: str | None = None, run_id: str | None = None, limit: int = 50,
                  offset: int = 0, t=Depends(tenant)):
    return dev.feedback_list(t["id"], project_id, status, run_id, limit, offset)


@app.post("/v1/projects/{project_id}/feedback/{feedback_id}/review")
def review_feedback(project_id: str, feedback_id: str, body: ReviewIn, t=Depends(tenant)):
    b = body.model_dump()
    b["reviewer"] = actor(t, b["reviewer"])
    if b["dataset_id"] and limits(t) and dev.count_examples(t["id"], project_id, b["dataset_id"]) >= \
            limits(t)["examples_per_dataset"]:
        raise QuotaExceeded(f"The {t['plan']} plan allows {limits(t)['examples_per_dataset']} examples per dataset.")
    return dev.review_feedback(t["id"], project_id, feedback_id, **b)


# --- DevTools: datasets -------------------------------------------------------------
@app.post("/v1/projects/{project_id}/datasets")
def create_dataset(project_id: str, body: DatasetIn, t=Depends(tenant)):
    """Get or create a dataset by name."""
    return dev.create_dataset(t["id"], project_id, body.name, actor=actor(t))


@app.get("/v1/projects/{project_id}/datasets")
def list_datasets(project_id: str, name: str | None = None, t=Depends(tenant)):
    return dev.datasets(t["id"], project_id, name)


@app.get("/v1/projects/{project_id}/datasets/{dataset_id}")
def get_dataset(project_id: str, dataset_id: str, version: int | None = None,
                split: Literal["dev", "holdout"] | None = None, include_pending: bool = False, t=Depends(tenant)):
    return dev.dataset(t["id"], project_id, dataset_id, version, split, include_pending)


@app.post("/v1/projects/{project_id}/datasets/{dataset_id}/examples/{example_id}/{decision}")
def review_dataset_example(project_id: str, dataset_id: str, example_id: int, decision: Literal["verify", "reject"],
                           t=Depends(tenant)):
    """Verify or reject a pending example (for example one written by study). Only verified examples are tested."""
    new_id = dev.review_example(t["id"], project_id, dataset_id, example_id,
                                "verified" if decision == "verify" else "rejected", actor(t))
    return {"id": example_id, "status": "verified" if decision == "verify" else "rejected", "new_id": new_id}


@app.get("/v1/projects/{project_id}/datasets/{dataset_id}/history")
def dataset_history(project_id: str, dataset_id: str, t=Depends(tenant)):
    return dev.dataset_history(t["id"], project_id, dataset_id)


@app.post("/v1/projects/{project_id}/datasets/{dataset_id}/examples", status_code=201)
def add_dataset_examples(project_id: str, dataset_id: str, body: DatasetExamplesIn, t=Depends(tenant)):
    lim = limits(t)
    return dev.add_examples(t["id"], project_id, dataset_id, [e.model_dump() for e in body.examples],
                            body.allow_duplicates, lim and lim["examples_per_dataset"])  # {"ids": [...], "skipped": n}


@app.put("/v1/projects/{project_id}/datasets/{dataset_id}/examples/{example_id}")
def update_dataset_example(project_id: str, dataset_id: str, example_id: int, body: ExamplePatch, t=Depends(tenant)):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    return {"id": dev.update_example(t["id"], project_id, dataset_id, example_id, fields)}


@app.delete("/v1/projects/{project_id}/datasets/{dataset_id}/examples/{example_id}", status_code=204)
def delete_dataset_example(project_id: str, dataset_id: str, example_id: int, t=Depends(tenant)):
    dev.delete_example(t["id"], project_id, dataset_id, example_id)


# --- knowledge and memory (hosted agents) ----------------------------------------------
@app.post("/v1/projects/{project_id}/knowledge", status_code=201)
def add_project_knowledge(project_id: str, body: DocIn, t=Depends(tenant)):
    """A document the agent's search_knowledge tool can search."""
    return dev.add_knowledge(t["id"], project_id, body.title, body.content, actor(t))


@app.get("/v1/projects/{project_id}/knowledge")
def list_project_knowledge(project_id: str, t=Depends(tenant)):
    return dev.knowledge_docs(t["id"], project_id)


@app.delete("/v1/projects/{project_id}/knowledge/{doc_id}", status_code=204)
def delete_project_knowledge(project_id: str, doc_id: str, t=Depends(tenant)):
    dev.delete_knowledge(t["id"], project_id, doc_id, actor(t))


@app.get("/v1/projects/{project_id}/lessons")
def list_project_lessons(project_id: str, state: Literal["probation", "pinned", "retired"] | None = None,
                         t=Depends(tenant)):
    return dev.lessons(t["id"], project_id, state)


@app.post("/v1/projects/{project_id}/lessons/{lesson_id}/{action}")
def veto_lesson(project_id: str, lesson_id: int, action: Literal["retire", "probation"], t=Depends(tenant)):
    """Retire a lesson (a human veto: the loop can't pin it again) or put it back in probation."""
    return dev.set_lesson_state(t["id"], project_id, lesson_id, "retired" if action == "retire" else "probation",
                                actor(t))


# --- jobs ---------------------------------------------------------------------------
@app.get("/v1/projects/{project_id}/jobs")
def list_jobs(project_id: str, kind: str | None = None, limit: int = 50, t=Depends(tenant)):
    return jobs.jobs(dev, t["id"], project_id, limit, kind)


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, t=Depends(tenant)):
    return jobs.get(dev, t["id"], job_id)


@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, t=Depends(tenant)):
    return jobs.cancel(dev, t["id"], job_id)


# --- DevTools: evaluations and comparisons -----------------------------------------
@app.post("/v1/projects/{project_id}/evaluations", status_code=201)
def record_evaluation(project_id: str, body: EvaluationIn, t=Depends(tenant)):
    """Store a finished evaluation (the SDK runs your app and scorers; the server checks completeness and computes
    the summary)."""
    return dev.record_evaluation(t["id"], project_id, body.model_dump(), actor=actor(t))


@app.get("/v1/projects/{project_id}/evaluations")
def list_evaluations(project_id: str, dataset_id: str | None = None, limit: int = 50, offset: int = 0,
                     t=Depends(tenant)):
    return dev.evaluations(t["id"], project_id, dataset_id, limit, offset)


@app.get("/v1/projects/{project_id}/evaluations/{evaluation_id}")
def get_evaluation(project_id: str, evaluation_id: str, format: Literal["json", "csv"] = "json", t=Depends(tenant)):
    ev = dev.evaluation(t["id"], project_id, evaluation_id)
    if format == "json":
        return ev
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["example_id", "critical", "score", "passed", "error", "scores", "input", "expected_output", "output"])
    for r in ev["results"]:
        w.writerow([r["example_id"], r["critical"], r["score"], r["passed"], r["error"] or "", json.dumps(r["scores"]),
                    json.dumps(r["input"]), json.dumps(r["expected_output"]), json.dumps(r["output"])])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=evaluation-{evaluation_id}.csv"})


@app.post("/v1/projects/{project_id}/comparisons", status_code=201)
def compare(project_id: str, body: CompareIn, t=Depends(tenant)):
    return dev.compare(t["id"], project_id, body.baseline_eval_id, body.candidate_eval_id, body.policy,
                       actor=actor(t))


@app.get("/v1/projects/{project_id}/comparisons")
def list_comparisons(project_id: str, limit: int = 50, t=Depends(tenant)):
    return dev.comparisons(t["id"], project_id, limit)


@app.get("/v1/projects/{project_id}/comparisons/{comparison_id}")
def get_comparison(project_id: str, comparison_id: str, t=Depends(tenant)):
    return dev.comparison(t["id"], project_id, comparison_id)


# --- DevTools: versions -----------------------------------------------------------
@app.post("/v1/projects/{project_id}/versions", status_code=201)
def create_version(project_id: str, body: VersionIn, t=Depends(tenant)):
    """New versions are always candidates. Approve and activate them explicitly."""
    return dev.create_version(t["id"], project_id, actor=actor(t), **body.model_dump())


@app.get("/v1/projects/{project_id}/versions")
def list_versions(project_id: str, name: str | None = None, t=Depends(tenant)):
    return dev.versions(t["id"], project_id, name)


@app.get("/v1/projects/{project_id}/versions/active")
def active_version(project_id: str, name: str = "system_prompt", t=Depends(key_auth)):
    """The version production should use (null if none). Readable with ingest keys."""
    return dev.active_version(t["id"], project_id, name)


@app.get("/v1/projects/{project_id}/versions/{name}/{version}")
def get_version(project_id: str, name: str, version: int, t=Depends(tenant)):
    return dev.version(t["id"], project_id, name, version)


@app.post("/v1/projects/{project_id}/versions/{name}/{version}/approve")
def approve_version(project_id: str, name: str, version: int, body: ApproveIn, t=Depends(tenant)):
    return dev.approve(t["id"], project_id, name, version, body.comparison_id, body.override_reason,
                       actor(t, body.reviewer))


@app.post("/v1/projects/{project_id}/versions/{name}/{version}/reject")
def reject_version(project_id: str, name: str, version: int, body: RejectIn, t=Depends(tenant)):
    return dev.reject(t["id"], project_id, name, version, body.reason, actor(t, body.reviewer))


@app.post("/v1/projects/{project_id}/versions/{name}/{version}/activate")
def activate_version(project_id: str, name: str, version: int, body: ActivateIn, t=Depends(tenant)):
    return dev.activate(t["id"], project_id, name, version, actor(t, body.reviewer))


# --- managed agents -----------------------------------------------------------------
@app.post("/v1/agents", status_code=201)
def create_agent(body: AgentIn, t=Depends(tenant)):
    return store.create_agent(t["id"], body.name, body.domain, body.instructions, body.rubric)


@app.get("/v1/agents")
def list_agents(t=Depends(tenant)):
    return store.agents(t["id"])


@app.get("/v1/agents/{agent_id}")
def get_agent(a=Depends(owned_agent)):
    return {**a, "stats": store.stats(a["id"]), "playbook": store.active_playbook(a["id"])["content"]}


@app.patch("/v1/agents/{agent_id}")
def update_agent(body: AgentPatch, a=Depends(owned_agent)):
    store.update_agent(a["id"], **body.model_dump())
    return store.agent(a["id"])


@app.delete("/v1/agents/{agent_id}", status_code=204)
def delete_agent(a=Depends(owned_agent)):
    store.delete_agent(a["id"])


@app.post("/v1/agents/{agent_id}/knowledge", status_code=201)
def add_knowledge(body: DocIn, a=Depends(owned_agent)):
    return store.add_doc(a["id"], body.title, body.content)


@app.get("/v1/agents/{agent_id}/knowledge")
def list_knowledge(a=Depends(owned_agent)):
    return store.docs(a["id"])


@app.delete("/v1/agents/{agent_id}/knowledge/{doc_id}", status_code=204)
def delete_knowledge(doc_id: str, a=Depends(owned_agent)):
    if not store.delete_doc(a["id"], doc_id):
        raise HTTPException(404, "Document not found.")


@app.post("/v1/agents/{agent_id}/examples", status_code=201)
def add_example(body: ExampleIn, a=Depends(owned_agent)):
    return {"id": store.add_example(a["id"], body.input, body.expected, "user")}


@app.get("/v1/agents/{agent_id}/examples")
def list_examples(include_unverified: bool = False, a=Depends(owned_agent)):
    return store.examples(a["id"], include_unverified=include_unverified)


@app.post("/v1/agents/{agent_id}/examples/{example_id}/verify")
def verify_example(example_id: int, request: Request, a=Depends(owned_agent), model=Depends(llm)):
    """Accept a pending correction: it joins the eval set and the agent extracts lessons from it."""
    return metered(request, a, model, Agent(store, model, a).verify_example, example_id)


@app.post("/v1/agents/{agent_id}/examples/{example_id}/reject")
def reject_example(example_id: int, a=Depends(owned_agent)):
    if not store.review_example(a["id"], example_id, "rejected"):
        raise HTTPException(404, "No pending example with that id.")
    return {"id": example_id, "status": "rejected"}


@app.delete("/v1/agents/{agent_id}/examples/{example_id}", status_code=204)
def delete_example(example_id: int, a=Depends(owned_agent)):
    if not store.delete_example(a["id"], example_id):
        raise HTTPException(404, "Example not found.")


@app.post("/v1/agents/{agent_id}/run")
def run(body: RunIn, request: Request, a=Depends(owned_agent), model=Depends(llm)):
    return metered(request, a, model, Agent(store, model, a).run, body.task, body.learn, body.max_revisions)


@app.post("/v1/agents/{agent_id}/feedback")
def feedback(body: FeedbackIn, request: Request, a=Depends(owned_agent), model=Depends(llm)):
    return metered(request, a, model, Agent(store, model, a).feedback,
                   body.episode_id, body.rating, body.correction, body.comment)


# ponytail: study/improve run synchronously (can take minutes on rate-limited free tiers);
# move to a job queue + polling endpoint if requests start hitting proxy timeouts.
@app.post("/v1/agents/{agent_id}/study")
def study(body: StudyIn, request: Request, a=Depends(owned_agent), model=Depends(llm)):
    return metered(request, a, model, Agent(store, model, a).study, body.rounds)


@app.post("/v1/agents/{agent_id}/evaluate")
def evaluate(body: EvaluateIn, request: Request, a=Depends(owned_agent), model=Depends(llm)):
    """Score the agent as it is now on its eval set, without changing anything."""
    def score():
        examples = store.examples(a["id"], body.max_examples)
        if not examples:
            raise ValueError("No eval examples. Add examples, verify feedback corrections, or run study first.")
        pb = store.active_playbook(a["id"])
        scores = Agent(store, model, a).scores(pb["content"], examples, body.trials)
        return {"score": round(sum(scores) / len(scores), 3), "playbook_version": pb["version"],
                "trials": body.trials, "examples": [{"id": e["id"], "source": e["source"], "score": round(x, 3)}
                                                    for e, x in zip(examples, scores)]}
    return metered(request, a, model, score)


@app.post("/v1/agents/{agent_id}/improve")
def improve(body: ImproveIn, request: Request, a=Depends(owned_agent), model=Depends(llm)):
    return metered(request, a, model, Agent(store, model, a).improve, body.max_examples, body.trials, body.activate)


@app.get("/v1/agents/{agent_id}/lessons")
def list_lessons(include_inactive: bool = False, a=Depends(owned_agent)):
    return store.lessons(a["id"], include_inactive)


@app.post("/v1/agents/{agent_id}/lessons/{lesson_id}/{action}")
def toggle_lesson(lesson_id: int, action: str, a=Depends(owned_agent)):
    if action not in ("enable", "disable"):
        raise HTTPException(404, "Use /enable or /disable.")
    if not store.set_lesson_active(a["id"], lesson_id, action == "enable"):
        raise HTTPException(404, "Lesson not found.")
    return {"id": lesson_id, "active": action == "enable"}


@app.get("/v1/agents/{agent_id}/episodes")
def list_episodes(limit: int = 50, a=Depends(owned_agent)):
    return store.episodes(a["id"], min(max(limit, 1), 500))


@app.get("/v1/agents/{agent_id}/playbooks")
def list_playbooks(a=Depends(owned_agent)):
    return store.playbooks(a["id"])


@app.post("/v1/agents/{agent_id}/playbooks/{version}/activate")
def activate_playbook(version: int, a=Depends(owned_agent)):
    if not store.activate_playbook(a["id"], version):
        raise HTTPException(404, "Playbook version not found.")
    return {"active_version": version}


# The landing page. Mounted last: routes registered above (/docs, /health, /v1/*, /dashboard) take priority.
if WEB.is_dir():  # a pip install ships the API without the landing page
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    @app.api_route("/privacy", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/terms", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/refund", methods=["GET", "HEAD"], include_in_schema=False)
    def legal_page(request: Request):
        return FileResponse(WEB / f"{request.url.path.strip('/')}.html")

    # without this, unknown API paths would fall through to the mount and get the HTML 404 page
    @app.api_route("/v1/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def unknown_api_route(rest: str):
        raise HTTPException(404, "Not Found")

    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")  # also serves 404.html for unknown paths
