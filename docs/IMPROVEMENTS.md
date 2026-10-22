Improvements, in priority order

1. The SDK silently duplicates dataset examples (fix now). Running the quickstart script twice took the dataset from n=1 to n=2. create_dataset returns the existing dataset (good), but add_example adds the same example again. Since people re-run scripts constantly, datasets fill with duplicates and those examples count twice in every evaluation. Fix: skip identical (input, expected_output) pairs by default (hash the pair), add add_example(..., allow_duplicate=True) for the rare real need, and print “skipped N duplicates”. Add a test that re-runs and expects n to stay the same.

2. Redaction is weak for your own market. DEFAULT_REDACT only covers secrets and card numbers. Add opt-in redactors for Aadhaar (12 digits, with a checksum check), PAN (ABCDE1234F), and more secret formats: ghp_ (GitHub), xox[bp]- (Slack), AKIA… (AWS), rzp_live_ (Razorpay). Indian support-bot logs will contain Aadhaar and PAN numbers, and storing them unredacted is a DPDP Act problem.

3. Clean up the repo root. SPEC.md describes the old hosted-agent product and will confuse anyone reading the repo. Move it plus FRONTEND_SPEC.md, LAUNCH_SPEC.md and DESIGN_RULES.md into docs/, and mark SPEC.md as historical.

4. You still have no proof that it improves anything. The hard demo was inconclusive because the baseline was already right on 18 of 20 cases. That’s fine for “release gate” positioning, but the first thing a beta user will ask is “show me it catching something.” What would help:

a demo where a prompt change breaks a case and the gate blocks it (the gate’s real job), not just one where it fixes something
a harder dataset where baseline accuracy is around 60–70%

5. Distribution: publish to PyPI. pip install "...@ git+https://..." is friction and looks unfinished. Check that the sia-devtools name is free, then publish 0.1.0. Also add an async client, or at least document that the SDK’s work runs in a background thread so it won’t block async apps.

6. Operations (from your own AUDIT.md, still open):

metrics and alerting: at minimum, error-rate alerts from the JSON logs, and Sentry’s free tier for exceptions
one restore drill on the real server
Postgres: only when you need more than one server instance. Not before.

7. Wait for beta users before building: user accounts/SSO (shared org keys are fine for 5–10 design partners), LangChain/LlamaIndex adapters (build whichever framework your first users actually use), recurring subscriptions.

What actually blocks launch now

The code is ready. These human steps from LAUNCH_SPEC.md §3 aren’t done yet: domain → deploy → scheduled backups → Razorpay test-mode run → CA → beta users. Each week spent polishing code instead of getting the first 3 users is a week lost.

---

## Status (2026-10-22)

| # | Item | Status | Commit |
|---|---|---|---|
| 1 | Duplicate dataset examples | **Done.** Identical `(input, expected_output)` pairs, after redaction, are skipped (existing id returned, version unchanged); `allow_duplicate(s)=True` to add anyway; the SDK prints how many were skipped. The hosted plan limit now counts only new examples, so a re-run at the limit isn't refused. | 63f7568 |
| 2 | Redaction for India + more secrets | **Done.** Opt-in `aadhaar` (Verhoeff-checked) and `pan`; the default `secrets` rule now covers GitHub, Slack, AWS key ids and Razorpay live keys. | d0ceb99 |
| 3 | Repo root cleanup | **Done.** `SPEC.md` (marked historical), `FRONTEND_SPEC.md`, `LAUNCH_SPEC.md`, `DESIGN_RULES.md` moved to `docs/`. | 49aaec7 |
| 4 | Proof it catches something | **Done; results in the README.** `--regression` (a "be generous" edit gated with critical cases) and `--harder` (multi-step refund arithmetic), both committed before any run. | 410bf2c, 3290063 |
| 5 | PyPI + async | **Ready; publishing is yours.** Name unregistered on 2026-10-22; metadata, clean build, `twine check`, a trusted-publishing workflow and `docs/RELEASING.md`. `@sia.trace` now works on `async def` (it used to raise `TypeError`). | e1adab8 |
| 6 | Operations | **Done in code; restore drill is yours.** Access logs were never printed (no handler): fixed. `sia logcheck` alerts on the 5xx rate; optional Sentry with headers and bodies stripped. Postgres deferred, as the list says. | d9e1d31 |
| 7 | Wait for beta users | **Not built, on purpose.** Accounts/SSO, framework adapters and subscriptions wait for real users. | — |

Owner-only steps still open: publish to PyPI (one-time trusted-publisher setup in `docs/RELEASING.md`), a restore
drill on the real server, and the launch checklist in `docs/LAUNCH_SPEC.md` §3.
