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
| `phase1/` | An earlier prototype (needs `google-genai`, not in requirements) | Not part of the product. Left untouched. |

The engine itself (critique loop, lessons, paired comparison) was sound and is kept. `significant_gain` was valid
and is now implemented on top of the shared `compare_scores`.

## 2. What was built

| Phase | Result |
|---|---|
| 2. Stabilise engine | Corrections become **pending** examples; `verify_example` admits them and extracts lessons. `improve` returns a verdict and reason, needs 3+ examples, and activates only with `activate=True` and an "improved" verdict. Numbered migrations (`schema_migrations`). |
| 3. SDK + runs | `sia_devtools.SIAClient`: runs, nested runs, `@trace`, errors, latency, tokens/cost, tags, metadata, scores, prompt versions. Hosted mode (background batching, bounded retries, transient failures kept, permanent ones dropped with `last_error`) and local SQLite mode. Client-side redaction and size limits. |
| 4. Datasets, scoring, feedback | Immutable dataset versions (`added_in`/`removed_in`), history, edits as replacements. Deterministic scorers (`exact_match`, `contains`, `json_match`, `regex`), custom `@scorer`, optional `llm_judge`; deterministic scores outrank model scores. Feedback is pending until verified or rejected; provenance kept. |
| 5. Improvement lab + CI | `propose_improvement` (verified evidence only, locked text enforced), `compare` (paired, min examples, z·SE noise threshold, regression and critical-regression rules), `approve` (needs an "improved" comparison against the active version under the project policy, or an audited override), `reject`, `activate`, rollback. `sia eval run / compare / gate` with exit codes and JSON. |
| 6. Hosted API | Organizations, projects, multiple keys with `full`/`ingest` scopes, rotation and revocation, plan limits, 413 body limit, JSON access logs, health check, pagination, admin plan endpoint as the payment boundary. |
| 7. Dashboard | Single static page served by the API: overview, runs and traces, datasets and review queue, evaluations and comparisons, improvement lab with diff/approve/reject/rollback, project settings and keys, usage. Real data only. |
| 8. Docs, deploy, demo | README, `.env.example`, Dockerfile (non-root, healthcheck), `pyproject.toml` (`sia` command), GitHub Actions gate example, `examples/devtools_demo.py`. |
| 9. Tests | 46 offline tests (see `tests/`), also run on a clean Python 3.11 install. |

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
- No backups, metrics, alerting or on-call runbook. Logs go to stdout as JSON lines.
- No retention job: retention is applied when runs are ingested, so an idle project keeps old runs until the next write.
- `sia/agent.py` managed-agent `study` and `improve` still run synchronously inside the request.

**Product**
- Prices and plan limits are hypotheses. No billing provider is integrated; `POST /v1/admin/orgs/{id}/plan` is
  the boundary a payment webhook would call.
- Only a synchronous Python SDK. No async support, no framework adapters (LangChain, LlamaIndex, ...).
- The gate can only protect behaviour that the developer's dataset and target function exercise.
