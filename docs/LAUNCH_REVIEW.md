Checking the repo against `LAUNCH_SPEC.md`: what's on `main`, any `launch-prep` branch, and whether each task is really done.

All 13 tasks are committed on `launch-prep`, which isn't merged yet. Now verifying it rather than trusting the commit messages: tests, security scans, then each diff.

94 tests pass (up from 65), and bandit and pip-audit are clean. The spec banned weakening existing tests, and two existing test files were edited, so I'm checking those first:

Existing tests were tightened, not weakened. The tie fix is a genuine bug the hard demo uncovered. Now the risky code: access requests, the usage cap, backup/restore and data deletion.

The deletion logic matches the existing ingest-time retention. Now a live run of the whole branch: server, access-request flow, pricing, HEAD, link check, and the operator CLI.

The whole live flow works: dedupe, honeypot, validation, a 5-per-hour limit, admin gate, a working key after approval, a 404 on approving twice, and no broken links. Now the operator commands:

One problem is mine: the emoji check in my own `DESIGN_RULES.md` fails without a UTF-8 locale ("code point too large"). Running that check another way, then reading the Task 13 results:

Fixing the broken emoji check in my `DESIGN_RULES.md` on your computer, since that error was mine:

**Verdict: Claude Code did the spec properly. All 13 tasks are done and verified, not just committed. Merge `launch-prep` after fixing 2 things.** It's not merged yet: `main` is still at the pre-launch commit.

## What I verified myself
| Check | Result |
|---|---|
| Tests | **94 pass** (65 → 94). Two existing tests were edited, and both got **stricter**, not weaker |
| Security CI | bandit clean, pip-audit clean |
| Task 1 (one product) | New DevTools H1; old agent copy is gone; no emojis, hype words, em dashes or banned fonts. Gradients are only used for the corner-bracket strokes, which is allowed |
| Task 2 (request access) | Ran live: dedupe (case-insensitive), honeypot stores nothing, bad email → 422, 6th request in an hour → 429, no admin key → 403, approve → key works on `/v1/me`, approving twice → 404 |
| Task 3 (contact) | `/site-config.json` returns the email; privacy page has a grievance contact |
| Task 4 (cap) | 2× cap enforced; the dashboard shows one banner at 80%, 100% and the cap; terms say overage isn't billed in the beta |
| Task 5 (HEAD) | 200 on `/`, `/health`, `/dashboard`, `/privacy`, `/terms`, `/refund` |
| Tasks 6, 9, 10 | `backup` with retention, `maintenance`, `restore` (it correctly refused a bad file) and `doctor` (exit 1 on FAIL) all run |
| Tasks 7, 8 | `/v1/plans` matches `PLANS`; refund page matches terms §7 word for word; no broken internal links |
| Task 12 | `phase1/` removed and tagged `archive/phase1`; live CI badge; "Status: public beta" line |
| Task 13 | Dataset committed **before** the runs, 3 runs, reported as **inconclusive**. It also uncovered a real gate bug (a tie with the standard error counted as "improved"), now fixed and tested |

## Fix before merging

1. **The landing page is stale on results.** It still says *"A harder dataset… is needed to show SIA finding content fixes."* That harder test has now been run, and it **didn't show a gain** (held-out 0.917 → 0.967, inconclusive). Showing only the easy +1.000 win, while hinting the hard test is still to come, is selective reporting. Add the hard-demo row and its honest verdict to `web/index.html` and `web/agent.md`.
2. **`sia backup` leaves orphaned files.** Each backup copies the WAL journal mode, so opening it for the integrity check creates `-wal`/`-shm` side files (~32 KB each). Retention deletes the old `.db` but not its side files, so they pile up forever. **No data is at risk:** I checked, and the `.db` files are complete on their own. But it clutters the folder and makes restore confusing. Fix it in `sia/ops.py`: set `PRAGMA journal_mode=DELETE` on the copy after backup, and delete side files during retention, with a test.

## Small notes (fine for beta, fix later)
- **Approval isn't atomic.** `decide_access_request` marks the request approved *before* creating the org. If org creation ever fails, the request is stuck "approved" with no org. Do both in one transaction.
- **`sia restore` only checks for a running server at `SIA_BASE_URL`** (default `127.0.0.1:8000`). If your server runs on another port, set that variable before restoring.
- **The product still has no proof that it improves content.** 18 of 20 cases were already right at baseline. Market it as "a release gate that stops bad prompt changes," which the demo *does* show (it blocked the injected feedback and the regressions). Don't market it as "improves your agent" yet.

## My mistake, fixed
The emoji check in my `DESIGN_RULES.md` (`grep -P "\x{1F300}…"`) crashes without a UTF-8 locale, so it could never have caught anything. I replaced it with a Python one-liner, tested it on the repo (clean), and updated `DESIGN_RULES.md` in your folder. **Commit that change**; it's uncommitted locally and the copy on GitHub still has the broken line.

**Next:** fix items 1 and 2 → merge `launch-prep` → start the human checklist in `LAUNCH_SPEC.md` §3 (domain, deploy, backups, Razorpay test mode, CA).

If you want, I can write a short follow-up spec covering the two fixes and the atomic-approval change so Claude Code can do them in one pass.