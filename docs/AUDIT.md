# Audit and readiness assessment

## 1. Audit of the starting repository (2026-10-20)

Baseline: `python -m pytest` gave **15 passed**, all offline with a scripted fake LLM.

| Area | What existed | Gaps found |
|---|---|---|
| `sia/agent.py` | `run` (draft, critique, revise), `reflect`/`_save_lessons`, `feedback`, `study`, `scores`/`evaluate`, `improve`, `significant_gain` (paired gain must beat +0.02 and one standard error) | User corrections went **straight into the eval set and into lessons** with no review, a prompt-poisoning path. `improve` **auto-activated** candidates, so generation, evaluation, approval and activation were one step. One example could "prove" an improvement (no minimum sample). Unverified user text was pasted into the playbook-generation prompt. |
| `sia/store.py` | SQLite (WAL), FTS5 search, lessons with utility and retirement, versioned playbooks, usage ledger | Schema created with `CREATE IF NOT EXISTS` only, no migrations. One key per tenant, no scopes, no revocation history. No plans or quotas. |
| `sia/api.py` | FastAPI, hashed tenant keys, tenant isolation on agents, BYO LLM key, rate limit, key rotation | Built for one product shape: a hosted agent that answers. No way to instrument an **existing** app, no traces, no datasets, no release process. No payload size limit, no structured logs. |
| `sia/llm.py` | Raw-HTTP client for 9 providers, retries, model fallback chains, SSRF guard | No token usage captured. |
| SDK | None | Everything in sections 4-11 of the spec. |
| Evaluation | LLM-graded only, eval set chosen with `ORDER BY random()` each time | No deterministic scorers, no pinned dataset versions, no stored per-example results, no record of scorer or config. |
| Dashboard | None | |
| `phase1/` | An earlier prototype (needs `google-genai`, not in requirements) | Not part of the product. Removed before launch; kept at the git tag `archive/phase1`. |

The engine itself (critique loop, lessons, paired comparison) was sound and is kept. `significant_gain` was valid
and is now implemented on top of the shared `compare_scores`.

## 2. What was built

| Phase | Result |
|---|---|
| 2. Stabilise engine | Corrections become **pending** examples; `verify_example` admits them and extracts lessons. `improve` returns a verdict and reason, needs 3+ examples, and activates only with `activate=True` and an "improved" verdict. Numbered migrations (`schema_migrations`). |
| 3. SDK + runs | `sia_devtools.SIAClient`: runs, nested runs, `@trace`, errors, latency, tokens/cost, tags, metadata, scores, prompt versions. Hosted mode (background batching, bounded retries, transient failures kept, permanent ones dropped with `last_error`) and local SQLite mode. Client-side redaction and size limits. |
| 4. Datasets, scoring, feedback | Immutable dataset versions (`added_in`/`removed_in`), history, edits as replacements. Deterministic scorers (`exact_match`, `contains`, `json_match`, `regex`), custom `@scorer`, optional `llm_judge`; deterministic scores outrank model scores. Feedback is pending until verified or rejected; provenance kept. |
| 5. Improvement lab + CI | `propose_improvement` (verified evidence only, locked text enforced), `compare` (paired, min examples, z·SE noise threshold, regression and critical-regression rules), `approve` (needs an "improved" comparison against the active version under the project policy, or an audited override), `reject`, `activate`, rollback. `sia eval run / compare / gate` with exit codes and JSON. |
| 6. Hosted API | Organizations, projects, multiple keys with `full`/`ingest` scopes, rotation and revocation, plan limits, 413 body limit, JSON access logs, health check, pagination, admin plan endpoint for raising limits by hand. |
| 7. Dashboard | Single static page served by the API: overview, runs and traces, datasets and review queue, evaluations and comparisons, improvement lab with diff/approve/reject/rollback, project settings and keys, usage. Real data only. |
| 8. Docs, deploy, demo | README, `.env.example`, Dockerfile (non-root, healthcheck), `pyproject.toml` (`sia` command), GitHub Actions gate example, `examples/devtools_demo.py`. |
| 9. Tests | 50 offline tests (see `tests/`), also run on a clean Python 3.11 install. |

## 3. Production readiness: not yet

Assessed explicitly, as the spec asks. The workflow works end to end and the isolation, authorization and release
rules are tested, but these must be addressed before hosting paying customers:

**Security**
- No user accounts, SSO or per-person roles. People share organization API keys; the dashboard signs in with one.
  The `reviewer` and `actor` fields are self-reported, not authenticated identities.
- No external security review or penetration test. No secret-scanning of stored payloads beyond the redaction rules.
- Evaluation scores are computed by the SDK and uploaded. A full-scope key holder could upload fabricated scores.
  That is the customer's own data, but it means scores are evidence, not proof.
- Redaction is pattern-based. It will miss secrets and personal data that don't match a rule.

**Reliability and operations**
- SQLite, one server process. The rate limiter and the run quota check are per process. Horizontal scaling needs
  Postgres and a shared limiter.
- Request-size limiting relies on `Content-Length`; chunked uploads without it are not capped before parsing.
- Run export loads up to 100,000 rows into memory.
- ~~No backups or on-call runbook.~~ Done (Task 6, 4c596ad): `sia backup` / `sia restore`, a Litestream example and
  `docs/RUNBOOK.md`. ~~No alerting.~~ Done (improvement 6): `sia logcheck` alerts on the 5xx rate in the JSON
  logs (which, it turned out, never printed before: fixed), and optional Sentry reports unhandled errors with
  headers and bodies stripped. Still no metrics dashboard. A restore drill on the real server is still to do.
- ~~No retention job.~~ Done (Task 9, 7edabce): `sia maintenance` applies run retention to idle projects too, and deletes
  old rejected access requests and unpaid orders. Schedule it daily (`docs/RUNBOOK.md`).
- `sia/agent.py` managed-agent `study` and `improve` still run synchronously inside the request.

**Product**
- Everything is free: payments (Razorpay) were removed. Plan limits are hypotheses; an admin raises them by hand.
- Only a synchronous Python SDK. No async support, no framework adapters (LangChain, LlamaIndex, ...).
- The gate can only protect behaviour that the developer's dataset and target function exercise.

## 4. The self-improving loop (SELF_IMPROVING_SPEC.md, 2026-10-25)

The managed agents became `kind="agent"` projects that improve themselves through `sia_loop`, behind the same
release gate as everything else. `sia/agent.py`, `/v1/agents/*` and the managed-agent tables are gone (migration
`legacy-005` copies their data, `legacy-006` drops the tables and refuses to while uncopied rows remain).

**New trust boundaries**

| Boundary | Who can cross it | What stops abuse |
|---|---|---|
| Outcome endpoint (`POST /runs/{id}/outcome`) | ingest and full keys: the customer's own system | Only a bool and a 100-character source. An outcome can auto-verify a run into the **dev** split, never the holdout, and only with `auto_verify_outcomes` on, so a forged outcome can teach but can't pass the gate. |
| Webhooks (`loop.webhook_url`) | the server, calling out | https only, public addresses only (resolved before the request), no redirects, 5 s timeout; every delivery is HMAC-SHA256 signed with a per-project secret plus a timestamp, so receivers can reject forgeries and replays. |
| Agent-made tools (macros) | the optimizer (a model) | Data, not code: prompt templates filled by a regex (never `str.format`), and pipelines of up to 5 built-in tools with no loops. Locked text is required in every prompt template. Validated on every config write. |
| `http_get` tool | the actor (a model) | Off by default; `allowed_tools` and a host allowlist must both opt in; https, public addresses, no redirects, 100 KB. The address is checked, not pinned, before the request (DNS rebinding between the two is possible: see the `ponytail:` note). |
| Canary traffic | the loop (autonomy 2-3) or a person | Only approved versions; at most 50%; any metric worse at z > 2 rolls it back; a 48-hour watch after promotion; pause stops every canary at once. |
| Lessons from production text | reflection (a model reading user tasks) | Stored in probation only, never in a prompt until a holdout-tested config pins them; instruction-override patterns, URLs, emails, code and copied task text are dropped before storage. |
| Self-grading | the actor | Never a promotion signal (G1): deterministic scorers decide, a judge must be a different model, self-critique is a runtime score only. |
| The holdout split | the optimizer | Optimizer evidence comes from the dev split and live signals only, and anything about a holdout input is filtered out even when it arrives through production runs or feedback (G2, tested by capturing every prompt). |

**Still client-trusted**

- `kind="app"` evaluations are computed by the SDK where your code runs (`sia loop run --target`) and uploaded, as
  before: a full-scope key holder can upload fabricated scores for their own project. The server re-checks
  completeness, policy and locked text, not the scores themselves.
- `reviewer` and `actor` names are self-reported; the loop's own actions are audited as `sia-loop`.
- Background jobs use the server's model keys (`SIA_LLM_KEY_<PROVIDER>`); their calls count against the
  organization's monthly `llm_calls` quota, enforced before every job and capped inside it.
