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

## Task 8: refunds

- **Pro rata** means the unavailable time as a share of the 30-day pass (3 days down refunds 3/30), only once the
  total downtime in the period passes 24 hours. The old terms said "in proportion" without saying of what.
- **One wording, two pages**: the five policy bullets carry `class="policy"` in both `refund.html` and terms §7, and
  a test fails if they differ. "Business days" became "working days" in both, as the spec words it.
- **Refund requests** go to the contact address with the order or payment reference; we promise a reply within
  7 days. Refunds themselves are issued by hand in the Razorpay dashboard (there's no refund API call in SIA).
- **The footer link check** fetches every same-site footer link; external links (GitHub, license) aren't fetched in
  tests, which have no network.
- **Dashboard**: the refund link sits in the note under the plan cards, next to the pay buttons, and opens in a
  new tab so an open checkout isn't lost.

## Task 9: retention job

- **Run retention** is the shorter of the plan's `retention_days` and the project's own `retention_days` setting,
  the same rule ingest uses. An expired paid pass counts as Free, so its runs drop to 14 days.
- **Projects of organizations not in the `tenants` table** (none on a hosted server) only get their own setting.
- **Rejected access requests** are aged from the decision time, not the request time, as the privacy policy says.
- **Unpaid orders** are `created`-status payments older than 30 days from creation.
- **One transaction** for the whole job; the JSON line has `runs_deleted`, `rejected_access_requests_deleted` and
  `unpaid_orders_deleted`.

## Task 10: sia doctor

- **Exit code 1** on any FAIL (the CLI's "check failed" code, like a failed gate); WARNs alone exit 0.
- **`SIA_TRUSTED_PROXIES` unset is always a WARN**: the server can't tell from config alone whether it's behind a
  proxy, and the default trusts only a proxy on 127.0.0.1.
- **"Local mode"** for the `SIA_ALLOWED_HOSTS` check means `SIA_LOCAL_MODE=1`.
- **Razorpay keys** FAIL unless both the id and the secret are set; live keys are anything not starting `rzp_test_`.
- **DB writable** is checked by taking SQLite's write lock (`BEGIN IMMEDIATE`) and rolling back, so it changes
  nothing. A missing file is a FAIL rather than creating an empty database.
- **Backup age** uses file modification time in `--backup-dir` (default `backups`), counting only `sia backup`'s
  file names.
- **Placeholder prices** WARN when either paid price equals the built-in default (199900 / 799900 paise).

## Task 11: Razorpay test plan

- **Links, not values**: test cards and UPI ids are linked from Razorpay's docs, as the spec asks.
- **Database checks** are Python one-liners (the Docker image has Python but no `sqlite3` binary).
- **Abandoned-order cleanup** can be tested at once by back-dating the order and running `sia maintenance`.
- **A refund round-trip** in test mode is added after the checklist, to compare against `web/refund.html`.

## Task 12: cleanup

- **`archive/phase1`** is pushed to GitHub, so the prototype stays recoverable after `git rm -r phase1/`.
- **AUDIT §3** marks only the gaps it listed that these tasks closed (backups and runbook, retention job, refund
  policy), each with its commit. The access-request flow, usage cap, HEAD routes and `sia doctor` weren't listed
  there, so they're described in the README instead.
- **`.gitignore`** got `backups/` in Task 6, when `sia backup` started writing there by default.
- **`SPEC.md`** still names `phase1/` in its original file list; it's the historical spec and was left as written.

## Found along the way

- **`sia` now reads the `.env` in the directory it runs from.** It used to search from the installed package's
  folder, so `cd /srv/sia && sia backup` from cron would have ignored the server's `.env`. Found while running
  `sia doctor` against a sample `.env`; covered by `test_cli_reads_the_env_file_where_it_runs`.

## Task 13: the harder demo

- **Dev / held-out split.** `propose_improvement` shows the model each failed case with its expected answer, so a
  candidate scored on those same cases is graded on answers it was given. The 20 cases are split into 10 dev
  (evidence for the candidate) and 10 held-out test (the decision). Both are reported; the held-out one counts.
- **Pre-registered.** The dataset was committed (647f70e) before the first run and not changed afterwards. One case
  was reworded before any run because its phrasing was ambiguous ("a gift 10 days after delivery").
- **Gate fix.** With one improved example out of n, the mean gain equals the paired standard error, so with `z = 1`
  the verdict was decided by float rounding. `compare_scores` now treats a tie with the threshold as not an
  improvement (tolerance 1e-9). This makes the gate slightly stricter for everyone, `sia eval gate` included. The
  demo's verdicts were recomputed from the stored scores; the run-time output said `improved` in 5 of 6
  comparisons, and the README says so.
- **Refund scorer** takes the first `REFUND:` line with digits. It was not changed between runs so all three are
  scored the same way; the README names its one effect.
