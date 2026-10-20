# Security

## Reporting a vulnerability

Please report security problems privately through GitHub: **Security → Report a vulnerability** on this repository
(the owner must enable private vulnerability reporting under Settings → Code security). Do not open a public issue.
Include what you found, how to reproduce it and the impact. You should get an answer within 7 days.

## What the server protects against

| Threat | Protection | Where |
|---|---|---|
| Reading or changing another customer's data | Every query is scoped to the organization and project of the key; other organizations' ids return `404` | `sia_devtools/store.py`, tests in `test_devtools.py` |
| Stolen or leaked keys | Keys are random 256-bit tokens stored only as SHA-256 hashes, shown once, scoped (`full` / `ingest`), revocable and rotatable; never logged | `sia/store.py` |
| Guessing API or admin keys | 20 invalid keys from one IP in 10 minutes block that IP for the rest of the window (`429`), and each failure is logged as a security event | `key_auth`, `require_admin` in `sia/api.py` |
| A weak admin key | The admin API (create organizations, change plans) refuses to run with a missing key, a known default or one under 32 characters | `sia/security.py: admin_key_problem` |
| Floods from one client | Per-IP limit (600/min) and per-organization limit (120/min), with `Retry-After` | `sia/api.py` |
| Huge or endless request bodies | Bodies over 4 MB get `413`, counted on the bytes actually received, so a chunked upload without `Content-Length` can't get around it | `sia/security.py: BodyLimit` |
| One customer tying up the server with model calls | At most 2 model-calling requests per organization and 16 in total run at once; the rest get `429` | `sia/security.py: Slots` |
| Plan-limit abuse | Plan limits (projects, runs, examples, retention) are enforced server-side (`402`) | `sia/api.py` |
| Fake payments | A plan changes only after the Razorpay signature checks out and Razorpay itself confirms the order is fully paid, or a signed webhook says so; payments are idempotent | `sia/billing.py`, `test_billing.py` |
| Cross-site scripting | Every value the dashboard shows is HTML-escaped; ids are restricted to `[A-Za-z0-9_.:-]`. A strict Content-Security-Policy allows only the page's own inline script (by hash) and no `unsafe-inline` scripts | `sia/dashboard.html`, `sia/security.py` |
| Clickjacking, MIME sniffing, referrer leaks | `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, COOP/CORP | `sia/security.py: COMMON_HEADERS` |
| Downgrade to plain HTTP | `Strict-Transport-Security` on every https response | `guard_and_log` |
| Private data in shared caches | `Cache-Control: no-store` on every `/v1` response | `guard_and_log` |
| Host-header attacks | Optional Host allowlist (`SIA_ALLOWED_HOSTS`) | `sia/api.py` |
| Server-side request forgery | Callers can't choose an LLM base URL unless the server opts in (`SIA_ALLOW_CUSTOM_BASE_URL=1`); model names are validated before they go into URLs; redirects are not followed | `sia/llm.py` |
| Key leakage through errors | Provider error messages are scrubbed of the caller's model key; internal errors return only a request id, and the stack trace stays in the server log | `sia/llm.py`, `guard_and_log` |
| Prompt injection through feedback | Corrections stay pending until a person verifies them; rejected feedback is never used; locked text can't be removed by a candidate | `sia_devtools/store.py`, `sia/agent.py` |
| Personal data in stored runs | Redaction (secrets, cards, email, phone, your own patterns) runs in the SDK before data leaves your app and again on the server | `sia_devtools/store.py` |
| The keyless local mode leaking onto the network | It answers only loopback clients addressed as localhost, refuses requests that came through a proxy, and `sia dashboard` binds to 127.0.0.1 | `key_auth` |
| SQL injection | All values are bound parameters; the few dynamic SQL fragments are fixed strings or whitelisted column names | `bandit` in CI |
| Vulnerable dependencies | `pip-audit` and `bandit` run in CI on every push | `.github/workflows/tests.yml` |

## Production checklist

Do these before you put the server on the internet:

1. **Strong admin key.** `python -c "import secrets; print(secrets.token_urlsafe(32))"` into `SIA_ADMIN_KEY`.
   Keep it out of the repo (`.env` is gitignored) and in your host's secret store.
2. **HTTPS in front.** Terminate TLS at a reverse proxy or load balancer ([`examples/deploy/nginx.conf`](examples/deploy/nginx.conf)
   is a starting point). Never expose port 8000 directly.
3. **Trust only your proxy.** Set `SIA_TRUSTED_PROXIES` to the proxy's address so rate limits see real client IPs and
   HSTS sees https. Leaving it wide open (`*`) lets any client fake its IP with `X-Forwarded-For`.
4. **Pin the hostname.** `SIA_ALLOWED_HOSTS=api.yourdomain.com`.
5. **Never set `SIA_LOCAL_MODE` on a server.** It turns off API keys.
6. **Leave `SIA_ALLOW_CUSTOM_BASE_URL` unset** on a multi-tenant server.
7. **Rate limit at the edge too.** The built-in limits are per process (see "Scaling" below).
8. **Back up the database** (`/data` in Docker) and restrict who can read it: it holds your customers' runs.
9. **Watch the security log.** Lines from the `sia.security` logger (`invalid_api_key`, `invalid_admin_key`,
   `invalid_webhook_signature`) carry the IP, path and request id. Alert on spikes.
10. **Rotate** the admin key, Razorpay secrets and any customer key you suspect leaked (`POST /v1/keys/rotate`).
11. **Keep dependencies current.** Rebuild the image regularly; CI's `pip-audit` fails when a known vulnerability appears.

## Scaling

- **Rate limits are per process.** With N workers or instances, each one counts on its own, so the real limit is N
  times higher. That's fine for abuse protection with a few instances. For exact global limits, add rate limiting
  at the edge (nginx `limit_req`, your load balancer, or Cloudflare) or move the counters in `sia/security.py` to
  Redis.
- **SQLite is a single-machine database.** Run one instance per database file. Moving to Postgres is the step that
  lets you run several instances behind a load balancer.
- **Model calls are synchronous.** `study` and `improve` can take minutes. The concurrency caps protect the server,
  but behind a proxy with a short timeout these calls will fail; raise the proxy's read timeout for `/v1/agents/`
  (the nginx example does), or move them to a job queue.

## Not covered

- **The model's answers.** SIA doesn't check what a model says for correctness, safety or policy. That is what your
  evaluations and rubric are for.
- **Your own app.** The SDK sends whatever you trace. Use redaction rules for anything sensitive.
- **Account management.** There are no user accounts, passwords or SSO. Access is by API key per organization, so
  anyone holding a full-scope key can do everything in that organization. Give production apps `ingest` keys.
