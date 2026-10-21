# Launch Spec: get SIA DevTools ready for an official launch

> Hand this file to Claude Code from the repo root. Work through the tasks **in order**, without stopping to ask
> questions. Where the spec leaves room, make a reasonable choice and write it down in `docs/LAUNCH_NOTES.md`.
> Section 3 lists work only the human owner can do. **Do not attempt it**; just make the code ready for it.

## 0. Rules for every task

- **Branch:** work on `launch-prep`, never directly on `main`. Open one PR at the end.
- **Commit after every task** with a descriptive message. Never commit `.env`, `.venv`, `*.db`, `__pycache__` or real keys.
- **Tests:** `python -m pytest -q` must pass after every task. Every behaviour change gets a test. Never weaken or
  delete an existing test to make something pass.
- **Security CI must stay green:** `pip-audit --skip-editable` and `bandit -q -r sia sia_devtools -ll --skip B608`.
- **UI work follows `DESIGN_RULES.md`.** If it conflicts with `FRONTEND_SPEC.md`, `DESIGN_RULES.md` wins.
- **Honesty:** no invented numbers, users, logos or testimonials anywhere. Only real results from real runs.
- **No new runtime dependencies** unless a task says so. Python stdlib + the existing FastAPI stack.
- **Keep CSP strict:** any new inline script gets a hash through the existing `sec.landing_csp` / `dashboard_csp`
  path. Don't add `'unsafe-inline'` to `script-src`.
- Update `README.md` and `docs/AUDIT.md` (section 3, "Production readiness") whenever a task closes a listed gap.

---

## 1. Tasks (in order)

### Task 1: One product: align the landing page with SIA DevTools

**Problem:** `README.md` sells **SIA DevTools** (capture runs → verified feedback → datasets → paired evals →
gated release). `web/index.html` still sells the old hosted agent (*"Agents that learn your domain from every
answer, correction and document."*) and shows the old `examples/demo.py` results.

**Do:**
- Rewrite `web/index.html` copy around DevTools. Keep the existing layout, tokens, fonts and components.
  - H1 (≤ 12 words), for example: *"Ship prompt changes only when your own test cases prove they help."*
  - Lead: what it does, for whom, local-first, BYO model key.
  - Sections: Mission → How it works (the 5-step loop as a **sequence** with real output at each step, not 3 cards)
    → Quickstart (real code from README, copy button) → CI gate (the real `sia eval gate` command + exit codes)
    → Results (the README "Demo results" table **including its "What this does and does not show" caveat**)
    → Pricing (Task 7) → Request access (Task 2).
- Update the "On this page" scroll-spy entries, `web/agent.md`, `web/llms.txt`, page `<title>`, meta description
  and OG tags to match.
- Delete `SIA_explained.pptx` from the repo root. It describes the old product.
- Grep afterwards: no remaining "Agents that learn your domain", `examples/demo.py` results, or old `/v1/agents/run`
  marketing on the landing page. API docs for still-existing endpoints can stay in the README.

**Done when:** a test asserts `GET /` contains the new H1 and not the old one; the DESIGN_RULES §8 grep checks pass
on `web/`.

### Task 2: "Request access" flow (manual onboarding, no self-serve signup yet)

**Problem:** organizations can only be created with the admin key, so a visitor has no way in.

**Do:**
- New table via a numbered migration: `access_requests(id, email, name, company, use_case, status
  [pending|approved|rejected], org_id, created, decided)`.
- `POST /v1/access-requests` (public, no auth):
  - pydantic validation: email (≤ 254 chars, basic format), name ≤ 200, company ≤ 200, use_case ≤ 2,000
  - a hidden honeypot field (`website`): if it's filled, return 202 and store nothing
  - its own per-IP limit: 5 per hour, using the existing `sec.Window`
  - de-duplicate a pending request for the same email (return 202, don't insert again)
  - always return `202 {"status": "received"}`, so the endpoint never reveals whether an email exists
- Admin endpoints (use `require_admin`):
  - `GET /v1/admin/access-requests?status=pending`
  - `POST /v1/admin/access-requests/{id}/approve` creates the org (name = company or name) plus a full-scope key, and
    returns the key **once**
  - `POST /v1/admin/access-requests/{id}/reject`
  All three log a `security_event`.
- CLI: `sia admin requests [--status pending]`, `sia admin approve ID`, `sia admin reject ID` (read
  `SIA_BASE_URL` and `SIA_ADMIN_KEY` from env).
- Landing page form (section `#access`): labelled fields, the honeypot visually hidden, inline success/error states,
  and a disabled submit while sending. No third-party script.
- `web/privacy.html`: add what this form collects, why, how long it's kept (delete rejected requests after 90 days:
  add this to Task 9's retention job), and how to ask for deletion.

**Done when:** tests cover honeypot, rate limit, dedupe, validation errors, admin approve → key works on `/v1/me`,
reject, and non-admin → 403.

### Task 3: Real contact address, configured once

**Problem:** every page has `data-contact=""` and "Contact" links to GitHub issues.

**Do:**
- Env var `SIA_CONTACT_EMAIL` (add to `.env.example`).
- Public `GET /site-config.json` → `{"contact_email": "<value or null>"}`, with `Cache-Control: public, max-age=300`.
- `web/site.js` fetches it once and turns every `[data-contact-link]` into a `mailto:` link (and fills
  `[data-contact-show]` with the visible address). If it's null, keep the current GitHub-issues fallback.
- Privacy page: a "Grievance contact" line using the same mechanism.

**Done when:** tests cover the endpoint with and without the env var; a manual browser check (note it in the PR) shows
mailto links.

### Task 4: Cap paid-plan usage until overage billing exists

**Problem:** `PLANS["pro"|"team"]["overage"] = True` is reported in `/v1/usage` (`api.py` ~582) but never billed, so
paid plans are effectively unlimited.

**Do:**
- In `sia/store.py` `PLANS`, add `"hard_cap": 2.0` (a multiple of the included runs) for pro and team; free keeps
  `overage: False`.
- In `ingest_runs`: when monthly runs ≥ `runs_per_month × hard_cap`, reject with the existing `QuotaExceeded` and a
  message naming the cap and the reset date (first of next month, UTC).
- `/v1/usage` returns `included`, `used`, `overage`, `hard_cap_runs` and `percent_used`.
- Dashboard usage screen: a plain-text warning at ≥ 80% and ≥ 100% of the included runs, and a block notice at the
  cap. No toast spam: one banner.
- `web/terms.html` §7: say paid plans include N runs with a soft limit up to 2×, and that **overage is not billed
  during the beta**. Update "Last updated".

**Done when:** tests cover under the quota, between 1× and 2× (accepted, overage reported), at the 2× cap (rejected),
and a new month resetting it.

### Task 5: HEAD support for health and pages

**Problem:** `HEAD /dashboard` returns 404 and `HEAD /health` isn't routed, so uptime monitors that send HEAD
report the site as down.

**Do:** use `@app.api_route(..., methods=["GET", "HEAD"])` for `/health` and `/dashboard`. Confirm the static mount
answers `HEAD /` with 200.

**Done when:** tests assert `HEAD` returns 200 on `/`, `/health` and `/dashboard`, with the same CSP as GET.

### Task 6: Backups and restore

**Problem:** no backups, and SQLite is the only copy of customer data.

**Do:**
- CLI `sia backup [--dir backups/] [--keep 14]`: uses `sqlite3.Connection.backup()` (safe while the server is
  running in WAL mode), writes `sia-YYYYMMDD-HHMMSS.db`, runs `PRAGMA integrity_check` on the copy, deletes all but
  the newest `--keep`, and exits non-zero on any failure.
- CLI `sia restore FILE`: refuses to run if the server is reachable at `SIA_BASE_URL`, makes a safety copy of the
  current DB first, then replaces it.
- `examples/deploy/litestream.yml`: an example config for continuous replication to S3-compatible storage
  (placeholders only), plus a cron example for `sia backup` in `docs/RUNBOOK.md`.
- `docs/RUNBOOK.md` (new): how to back up, restore, rotate the admin key, revoke a leaked customer key, and what to
  do if the server is down. Short and step by step.

**Done when:** tests cover a backup of a populated DB that restores to identical row counts, retention keeping
exactly N files, and integrity-check failure → non-zero exit.

### Task 7: Visible pricing from one source of truth

**Problem:** the landing page shows only "Free" self-host. Hosted prices live in env vars (`SIA_PRICE_*_PAISE`).

**Do:**
- Public `GET /v1/plans`: plan limits from `PLANS` + prices and currency from `billing.settings()`, plus
  `"beta": true`. No auth.
- The landing pricing section fetches it and renders **Self-host (free, FSL)**, **Free hosted**, **Pro**, **Team**
  with real limits, "per 30 days", and a "Beta pricing" label. Static fallback text if the fetch fails. No "Most
  popular" badge (DESIGN_RULES #17).

**Done when:** a test asserts that `/v1/plans` matches `PLANS` and env prices; the page shows the right numbers
in a browser.

### Task 8: Refund and cancellation page

**Do:** `web/refund.html` in the same style as `terms.html`:
- 30-day passes, with no auto-renewal (so there's nothing to cancel)
- a full refund within 7 days if the paid limits are unused
- a pro-rata refund for more than 24h of downtime caused by us
- refunds go to the original payment method through Razorpay, typically within 5–7 working days
- how to request one (the contact address from Task 3)

It must match `terms.html` §7 exactly; if they differ, change both to the same wording. Link it in every page
footer and next to the dashboard's pay button.

**Done when:** a link-check test confirms every footer link in `web/*.html` resolves to 200.

### Task 9: Data-retention job

**Problem:** retention is only applied when new runs arrive, so idle projects keep old data forever.

**Do:** CLI `sia maintenance` deletes:
- runs older than the plan's `retention_days`
- rejected access requests older than 90 days
- `created`-status payment orders older than 30 days that were never paid

It's idempotent and logs counts as one JSON line. Document a daily cron in `docs/RUNBOOK.md`.

**Done when:** tests with backdated rows prove exactly the expired rows are deleted, per plan.

### Task 10: Launch readiness check

**Do:** CLI `sia doctor` prints PASS / WARN / FAIL lines and exits non-zero on any FAIL:

| Check | FAIL when |
|---|---|
| `SIA_ADMIN_KEY` | missing or weak (reuse `sec.admin_key_problem`) |
| `SIA_ALLOWED_HOSTS` | unset (WARN in local mode) |
| `SIA_TRUSTED_PROXIES` | unset while behind a proxy (WARN) |
| `SIA_CONTACT_EMAIL` | unset |
| Razorpay keys | missing; WARN if `rzp_test_` (fine for beta, not for live) |
| `RAZORPAY_WEBHOOK_SECRET` | missing |
| DB | not writable, or `integrity_check` fails |
| Backups | no file in the backup dir newer than 26 hours |
| Prices | still the placeholder defaults (WARN) |

**Done when:** tests cover a fully configured env (all PASS) and each FAIL individually.

### Task 11: Razorpay test-mode plan (document only)

**Do:** `docs/RAZORPAY_TEST_PLAN.md`, a checklist the owner runs on the deployed HTTPS URL in **test mode**. Each row
has the steps, the expected API response, the expected DB state and the expected dashboard state:
1. successful card payment → verify → plan active, `paid_until` = now + 30d
2. successful UPI payment, where the webhook arrives before the checkout callback
3. failed payment → plan unchanged
4. abandoned checkout → order stays `created`, is cleaned up by Task 9 after 30 days
5. duplicate webhook (re-send from the Razorpay dashboard) → `paid_until` unchanged
6. a webhook with a bad signature → 400 + security log line
7. a second payment for the same plan while active → period extends from the old `paid_until`
8. paying for Team while on Pro → Team starts now (document that Pro's remainder is lost; the owner decides later
   whether to prorate)
9. switching to live keys → `sia doctor` no longer warns

Include the Razorpay test card/UPI details **by linking to Razorpay's official test-data docs**, not by copying values.

### Task 12: Repo cleanup

**Do:**
- `git tag archive/phase1` on the current commit, then `git rm -r phase1/` (it's an old prototype with deliberately
  buggy files and an undeclared `google-genai` dependency).
- Replace the static "tests 50 passing" README badge with the GitHub Actions status badge for `tests.yml`.
- `README.md`: a "Status: public beta" line near the top, linking `docs/AUDIT.md`.
- `docs/AUDIT.md` §3: mark every gap closed by Tasks 2–10 as done (with the commit), and leave the rest.
- `.gitignore`: add `backups/`.

### Task 13 (optional, needs `GENAI_API_KEY` in `.env`): a harder, honest demo

**Problem:** the README demo was an easy formatting win (0.000 → 1.000).

**Do:**
- Add `examples/devtools_demo.py --hard`: a 20-example dataset where correct answers need **content** reasoning
  (rule interactions, exceptions, arithmetic), scored with deterministic scorers.
- Run it 3 times from a clean DB.
- Add the real results to the README as a new table, with mean and spread across the 3 runs, **whatever they show**,
  including "inconclusive" or "regressed". Don't tune the dataset after seeing results to make SIA look better; if
  you change it, say so and why.

---

## 2. Final deliverable

- One PR `launch-prep → main` with a summary table: task, files changed, tests added, and anything skipped and why.
- `docs/LAUNCH_NOTES.md` listing every default choice you made.
- `python -m pytest -q`, `pip-audit`, `bandit` and `sia doctor` (with a sample `.env`) outputs pasted into the PR.

## 3. Human-only checklist (Claude Code: don't do these, just make the code ready)

- [ ] Buy a domain; set up a real mailbox (e.g. `hello@domain`) → `SIA_CONTACT_EMAIL`
- [ ] Deploy behind HTTPS (Docker image + reverse proxy, see `examples/deploy/nginx.conf`); set `SIA_ALLOWED_HOSTS`,
      `SIA_TRUSTED_PROXIES`, `SIA_ADMIN_KEY`
- [ ] Schedule `sia backup` (or Litestream) and `sia maintenance`; do one real restore drill
- [ ] Uptime monitor on `GET /health`; error alerting
- [ ] Run `docs/RAZORPAY_TEST_PLAN.md` end to end in test mode
- [ ] Talk to a CA: GST registration, invoices, and whether a sole proprietorship is needed to accept live payments
- [ ] Complete Razorpay live KYC; switch to live keys; `sia doctor` all PASS
- [ ] Have a lawyer read `terms.html`, `privacy.html` and `refund.html` once
- [ ] Onboard 3–5 beta users by hand through Request access; fix what they hit
- [ ] Only then: public announcement
