# Launch notes: default choices made while working through LAUNCH_SPEC.md

Every place where the spec left room, and what was picked. Change any of these freely.

## Task 1: one product

- **H1** is the spec's example, word for word: "Ship prompt changes only when your own test cases prove they help."
- **How it works** shows the five steps with result fields from the README demo run (20 October 2026). They're
  labelled "real result" and are result fields, not raw terminal output, because the demo's full console log
  wasn't kept.
- **Hero diagram** is now the five-step loop (capture, feedback, dataset, eval, release), with the same single
  animation: an amber square moving along it, off under reduced motion.
- **Results** keeps five rows of the README table and the full caveat paragraph.
- **Pricing** at this point has two cards (self-host, hosted beta); Task 7 replaces it with the four plans.
- **"Works with"** provider marks stay: the SDK traces any model, and `sia improve` drafts candidates with these
  providers.
- **OG image** regenerated with the new headline and the five steps.

## Task 2: request access

- **Email check** is a basic `x@y.z` pattern (no DNS lookup), with surrounding whitespace stripped and the address
  lower-cased before the duplicate check.
- **Duplicates**: only a *pending* request with the same email is de-duplicated. Once a request is approved or
  rejected, the same address can ask again.
- **Rate limit** is 5 requests per IP per hour, counted in memory per process like the other limiters. Requests
  that fail validation (422) don't count, because they never reach the handler.
- **Honeypot**: a field named `website`, positioned off-screen (not `display: none`, which some bots skip) and
  hidden from screen readers. A filled-in honeypot logs a `access_request_honeypot` security event.
- **Approve** names the organization after the company, or the person's name when no company was given, and
  returns the first full-scope key once. An already-decided request returns 404, so a double approval can't
  create two organizations.
- **Admin listing** is also logged as a security event (`access_requests_listed`), since it reveals personal data.
- **Form copy** asks people not to include their users' personal data in the use-case field.
- **Notify me** (the old `mailto:` waitlist) is gone: the Request access form replaces it.

## Task 3: contact address

- **`data-contact` attributes** on `<body>` are gone: the address now comes only from `SIA_CONTACT_EMAIL` via
  `/site-config.json`, so there's one place to set it.
- **Fallback**: when the address is unset or the fetch fails, links keep the GitHub-issues `href` and their written
  text (for example "us"), instead of the old replacement text.
- **Grievance contact** is a paragraph under "Your rights" in the privacy policy, with the same 30-day answer time.

## Task 4: usage cap

- **A batch that would cross the cap is refused whole** (402), like the free plan's limit already worked, so a
  client never gets a half-accepted batch. Runs exactly up to the cap are accepted.
- **`/v1/usage` also returns `resets`** (the next first-of-month, UTC), which the dashboard banner shows.
- **Dashboard banner**: one line, the most severe that applies: refused at the cap (red), past 100% of included
  runs (amber), or at 80% or more (amber).
- **Terms §7** names the included runs for each plan and says overage isn't billed during the beta.

## Task 5: HEAD

- `/privacy` and `/terms` answer `HEAD` as well as `/`, `/health` and `/dashboard`, so a monitor can watch any page.

## Task 6: backups

- **Code lives in `sia/ops.py`** (server operator commands), wired into the `sia` CLI, so the SDK module stays
  free of server code.
- **Timestamps are UTC** in backup file names, so they sort correctly across time-zone changes.
- **Retention only touches files named exactly `sia-YYYYMMDD-HHMMSS.db`**, so nothing else in the directory is
  ever deleted, and it runs only after the new copy passed its integrity check.
- **A copy that fails the check is deleted** and the command exits 4, so a bad copy never counts as the newest.
- **Restore checks the backup first**, refuses if anything answers `GET /health` at `SIA_BASE_URL` (default
  `http://127.0.0.1:8000`), saves the current file as `<name>-before-restore-<UTC time>.db`, then copies with
  SQLite's backup API (not a file copy) so the target's WAL files stay consistent.
- **Two backups in the same second**: the second one fails rather than overwrite the first.
- **Leaked-key revocation** for an unreachable customer is a Python one-liner in the runbook rather than a new CLI
  command; the customer can already rotate their own key.

## Task 7: pricing

- **Four cards in the existing two-column grid** (self-host, Free, Pro, Team), no badge, no highlighted card.
- **Static fallback** is the full set of limits written into the HTML (a test checks it against `PLANS`), with
  "Price at checkout" in place of the paid prices, since those come from env vars that differ per deployment.
- **Numbers use `en-US` grouping** (250,000) to match the terms and README; prices use `en-IN` currency format.
- **"Beta pricing"** appears as the paid cards' label and in the sentence under the heading.
- **`/v1/plans` keeps the API's `no-store` cache header** like every `/v1` route; it's one small request per visit.
