# SIA DevTools — Scalability and Roadmap

## Purpose and evidence boundary

This document separates the current architecture from proposed evolution. The current source uses Python, FastAPI, SQLite, two persistence modules, an SDK background sender, in-process rate/concurrency controls, and synchronous managed-agent operations. docs/AUDIT.md explicitly says the current hosted model is SQLite plus one server process and identifies limitations. No traffic measurements were available in this inspection, so scale triggers below are measurable operational criteria rather than guessed user counts.

## Current constraints

| Area | Current design / evidence | Consequence |
|---|---|---|
| Persistence | SQLite in sia/store.py and sia_devtools/store.py | Simple local-first setup; write contention and limited shared coordination |
| Rate limits | Window objects in sia/security.py | In-memory and per process; limits multiply with replicas |
| LLM concurrency | Slots object in sia/security.py; defaults read in sia/api.py | Limits are per process, not global across replicas |
| Long jobs | Agent.study() and Agent.improve() run synchronously per docs/AUDIT.md | Slow provider calls occupy request workers |
| SDK delivery | Hosted traces queued by a background sender in sia_devtools/client.py | Must validate flush/shutdown, retries and permanent failures |
| Large export | Audit notes up to 100,000 rows may be loaded into memory | Memory spikes and slow responses |
| Request size | BodyLimit exists; audit notes reliance on Content-Length | Chunked uploads may bypass early size enforcement |
| Identity | No individual accounts/SSO/roles per audit | Shared keys and self-reported reviewer fields weaken accountability |
| Evaluation trust | SDK computes and uploads evaluation results | Full-scope key holder can submit fabricated scores |
| Observability | JSON access logs, optional Sentry, ops/logcheck; audit says no metrics dashboard | Some diagnostics exist; trend analysis needs more metrics |

## Stage 1 — Reliable local developer tool

**Goal:** make the SDK useful for one developer without requiring hosted infrastructure.

### Retain
- SIAClient local mode and SQLite.
- DevStore migrations, datasets, deterministic scorers and versioned prompt records.
- CLI and offline scripted demo.
- Managed-agent modules for users who explicitly want SIA-managed agents.

### Improve
- Test local database migrations from clean and previous versions.
- Make SDK flush/close behavior explicit and well-tested.
- Document redaction and local data location.
- Ensure the local dashboard binds only to loopback and remains safe against DNS rebinding.
- Provide clear errors for unsupported Python and missing optional dependencies.
- Add a small reproducible example that records one trace and runs one deterministic evaluation.

### Trigger to leave this stage
Move forward when independent developers can install, instrument, evaluate and inspect results without manual database repair, and CI passes on supported Python versions.

### Risk of changing too early
Adding cloud queues, multiple databases and deployment orchestration before the local API stabilizes increases maintenance cost without improving the core workflow.

### Tests
Fresh install, offline demo, dataset idempotency, schema migration, local-mode network restrictions, redaction, trace shutdown and deterministic scorer tests.

## Stage 2 — Single-user hosted service

**Goal:** a dependable hosted service for one developer or one small team sharing one organization.

### Retain
- FastAPI route structure.
- Organization API keys and scopes.
- Project-level data model.
- SQLite only if measured write volume remains low and deployment is one process.
- Existing billing boundary, after real provider validation.

### Change / add
- Deploy only behind HTTPS and a reviewed proxy configuration.
- Enforce streaming request-body limits, not just Content-Length.
- Define backup frequency, retention, RPO/RTO and run a restore drill.
- Add metrics for request latency, status codes, database lock time, LLM duration/cost, trace queue depth and failed deliveries.
- Add secret storage and rotation procedures.
- Confirm Razorpay order verification/webhook behavior in provider sandbox and then a controlled live test.
- Publish clear retention and deletion behavior.

### Trigger
Proceed when actual write-lock errors, request latency, backup/restore requirements, or multiple-worker deployment make single-process SQLite inadequate.

### Risk of changing too early
A move to PostgreSQL or a queue without load evidence adds operational work and new failure modes.

### Tests
Production-like restore drill, body-limit tests, load test with realistic batch sizes, payment replay/idempotency, secret-redaction checks, and failure-injection for model provider outages.

## Stage 3 — Multi-tenant developer SaaS

**Goal:** support multiple customer organizations with meaningful isolation and auditable review.

### Retain
- Project and organization model.
- API key scopes and rotation/revocation.
- Pydantic validation, scoring policy and dataset versioning.
- Current public SDK interface where possible.

### Required changes
- Add authenticated human identities, organization membership and reviewer roles before shared review workflows are marketed as accountable.
- Make organization/project ownership checks consistent for every endpoint.
- Add per-organization retention, deletion and export workflows.
- Decide how customer-supplied evaluation scores are trusted. For stronger evidence, execute target functions in a controlled customer-side runner or use a clearly defined attestation model; do not imply an uploaded score is independently verified.
- Add security review and penetration testing.
- Add quotas and billing that are enforced server-side and tested at boundaries.
- Define tenant-specific encryption, backup, deletion and incident response expectations.

### Trigger
Before multiple independent customers store production traces, especially if traces contain personal or confidential data.

### Risk of changing too early
Complex organization roles and enterprise controls can distract from validating the core failure-to-regression-test workflow. But identity and isolation are not optional once customers share data or reviewers need accountability.

### Migration approach
Introduce user/organization tables with migrations, keep API-key compatibility during a transition, add explicit membership checks, then deprecate shared-key-only workflows only after clients have migrated.

### Tests
Cross-tenant access matrix, role matrix, revoked-key behavior, deletion/export, per-tenant quotas, audit actor provenance, and migration compatibility.

## Stage 4 — Sustained concurrent use

**Goal:** handle concurrent API requests and evaluation jobs without relying on a single process's memory.

### Change when measured triggers occur
- **Database:** migrate SQLite to PostgreSQL when write-lock waits/errors or query latency exceed the agreed service objective, or when more than one API replica is required.
- **Rate limiting:** move Window state to a shared store or database-backed limiter before deploying multiple replicas.
- **Concurrency:** use a shared limiter if the model provider's account-wide limit must be respected across replicas.
- **Background jobs:** move long evaluations, model-based judging, study and improvement cycles to a durable queue when request latency or worker saturation breaches the service objective.
- **Exports:** stream or paginate large exports rather than loading all rows into memory.
- **SDK sender:** add explicit flush/shutdown semantics, queue metrics, and a documented policy for retry exhaustion.
- **Idempotency:** enforce stable idempotency keys for retryable ingestion and payment operations.

### Proposed topology

~~~mermaid
flowchart TD
  Client[SDK / browser] --> Edge[TLS ingress]
  Edge --> API1[API replica A]
  Edge --> API2[API replica B]
  API1 --> PG[(PostgreSQL)]
  API2 --> PG
  API1 --> SharedLimit[(Shared limiter)]
  API2 --> SharedLimit
  API1 --> Queue[Durable job queue]
  API2 --> Queue
  Queue --> Worker[Evaluation workers]
  Worker --> PG
  Worker --> LLM[LLM providers]
  API1 --> Obs[Metrics and logs]
  API2 --> Obs
  Worker --> Obs
  PG --> Backups[Automated backup and restore checks]
~~~

**Proposed only:** this topology is not in the current deployment. Add it when metrics show the single-instance design is the bottleneck or multi-replica availability is required.

### Migration sequence
1. Benchmark current SQLite query/write patterns.
2. Add database abstraction boundaries where current code assumes SQLite-specific SQL.
3. Create and test schema migrations for PostgreSQL.
4. Run dual-read or offline data migration only if needed; validate row counts and hashes.
5. Switch one staging instance and run integration/concurrency tests.
6. Introduce shared rate limiting and durable jobs.
7. Add replicas only after shared-state behavior is correct.

### Risk of changing too early
A distributed system creates queue delivery, retries, consistency, deployment and incident-response problems that do not exist in a single process. Do not split into microservices merely to appear scalable.

## Stage 5 — Larger organizations and enterprise requirements

**Goal:** satisfy customers that need security, auditability, availability and governance guarantees.

### Likely requirements
- SSO/OIDC, role-based access, organization provisioning and audited reviewer identities.
- Tenant-aware data retention, deletion, exports and backup policy.
- Centralized secrets, rotation, access logs, incident response and vulnerability management.
- Measured availability objectives and restore tests.
- Metrics, alerting, traces and dashboards for API, database, queue and provider dependencies.
- Network egress controls and strict policy for custom model endpoints.
- Durable background jobs with retry/dead-letter behavior.
- PostgreSQL backups and point-in-time recovery where justified.
- Release approval policies, critical tests and audit history.
- Documented data-processing and provider-retention behavior.
- Optional deployment isolation for customers with stricter data requirements.

### Retain
Keep the public SDK abstraction, scorer contract, dataset model and compare/approve/activate separation where they remain useful. These are product concepts, not tied to SQLite.

### Trigger
Enterprise controls should be driven by a concrete customer's requirements and risk profile, not built speculatively in advance.

### Risk of changing too early
SSO, complex policy engines, per-customer deployments and extensive compliance programs are expensive. Confirm customer demand and contract requirements first, without compromising baseline security.

## Cross-cutting work priorities

- **P0 — correctness/safety:** streamed body limits; TLS/proxy trust; secret hygiene; tested backups/restores; ownership and scope checks; payment signature validation; avoid claiming uploaded scores are independent proof.
- **P1 — dependable beta:** individual reviewer identity if shared review is a promise; isolation/migration tests; SDK queue shutdown semantics; metrics and alerts; real staging restore and payment tests.
- **P2 — measured growth:** PostgreSQL, shared limiter, durable evaluation queue, streaming exports, cost dashboards, stronger score provenance.
- **P3 — longer term:** async SDK, framework adapters, enterprise SSO and deployment isolation when customer demand warrants them.

## Trigger-based operational thresholds

Choose service objectives from the product's real requirements. Suggested triggers are relative to those objectives, not arbitrary user counts:

| Signal | Trigger | Likely action |
|---|---|---|
| SQLite lock errors / write wait | Repeated in load tests or violates write-latency objective | Profile queries; then consider PostgreSQL |
| API p95 latency | Exceeds agreed objective because of evaluation/model work | Move slow work to background jobs |
| Queue age / failed sends | Queue is not draining or retry exhaustion rises | Backpressure, explicit flush, alerting and durable delivery strategy |
| LLM concurrency | Provider throttling persists despite bounded retries | Adjust concurrency/fallback policy and enforce shared limits |
| Export memory | Peak memory approaches container limit | Stream/paginate exports |
| Recovery | Restore drill misses RTO/RPO | Change backup cadence, storage or recovery procedure |
| Tenant security | Multiple people review customer data | Authenticated users, roles and auditable reviewer identities |
| Evaluation trust | Release decisions have high impact | Server-side execution or stronger provenance for scores |

## Final recommendation

Do not start with microservices or Kubernetes. First close the known safety gaps, test backup restoration, make evaluation evidence trustworthy, and measure the current single-process limits. Keep the SDK and release-gate workflow stable while moving persistence, coordination and long-running work only when a measurable trigger justifies it.
