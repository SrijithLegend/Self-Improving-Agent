# SIA DevTools — Complete Technical Report

> Repository: SrijithLegend/Self-Improving-Agent  
> Inspected branch: main; tree SHA 72be749749ebf228786b522ca904d81bd1954b9c  
> Scope: documentation-only reverse engineering. No application code was changed for this report.  
> Verification note: source was inspected through GitHub. Tests were not run in this task.

## How to use this report

This document explains the current implementation, not an idealized future architecture. “Implemented” means visible in source. “Documented” means stated in existing repository documentation but not independently executed here. “Proposed” means a recommendation, not current functionality.

# Chapter 1 — Executive summary

SIA has evolved from a self-improving-agent experiment into SIA DevTools: a Python developer tool for recording AI application runs, collecting reviewed corrections, building evaluation datasets, comparing prompt versions, and preventing unsafe prompt releases.

A normal chatbot tries to answer a question. SIA DevTools adds an engineering loop around an AI application:

1. Record what the application did.
2. Capture feedback when output is wrong.
3. Review a correction before trusting it.
4. Turn verified corrections into repeatable test cases.
5. Evaluate a candidate prompt against the same examples as the baseline.
6. Keep a candidate inactive when results are weak, uncertain, or regressive.
7. Approve and activate changes through separate operations.

SIA does **not** retrain model weights. It changes application instructions and measures resulting behavior. Its central promise is closer to regression testing and release control for AI behavior than autonomous model training.

## Two current code paths

The repository contains two related paths:

1. **Managed-agent path:** sia/agent.py plus the /v1/agents/... API. A caller creates an SIA-managed agent, supplies knowledge and an LLM key for a request, and SIA drafts, critiques, revises, records episodes, and learns lessons.
2. **Developer-tools path:** sia_devtools/ plus /v1/projects/... API routes. A developer instruments an existing Python function with SIAClient, records traces, manages datasets, uploads evaluation results, compares prompt versions, and gates releases.

README.md positions DevTools as the current product. The managed-agent path remains in the current source and API. docs/AUDIT.md says the earlier phase1 prototype was removed before launch and retained at Git tag archive/phase1. There is no phase1 directory in the current tree.

## Maturity

README.md calls the product public beta. docs/AUDIT.md says it is not production-ready. The audit reports 50 offline tests and a working core workflow, but those tests were not executed during this documentation task. The source includes key hashing, scopes, redaction, request validation, rate limits, release decisions and ownership checks; it also has operational and identity limitations discussed below.

## Example

A support agent incorrectly promises a refund for a final-sale item. SIA records the run. A reviewer submits a correction. The correction remains pending until verified. A verified correction becomes a dataset example. A candidate prompt is evaluated against a fixed dataset. If it fixes this case but breaks a critical exception, the comparison should report a regression and normal approval should not activate it.

This is an illustrative scenario, not a claim that this exact support-policy task was executed.

# Chapter 2 — Evolution and current code paths

The current tree includes sia/, sia_devtools/, tests/, examples/, web/, docs/, Dockerfile, .env.example, pyproject.toml, uv.lock and GitHub Actions workflows. phase1/ is absent.

docs/SPEC.md is explicitly marked historical. It describes the original brief and pre-launch layout, so it is not an authoritative file inventory. Use README.md, docs/AUDIT.md and current source for the present implementation.

| Aspect | Earlier prototype at archive/phase1 | Current implementation |
|---|---|---|
| Purpose | Experiment with a tool-using agent and sandboxed task workspace | Managed-agent loop plus developer tools for traces, datasets, evaluations, prompt versions and release gates |
| Execution | Historical Gemini tool-use/task-debug loop; exact archived source was not inspected in this task | Agent.run() for managed agents; SIAClient and CLI for DevTools |
| Tool usage | Workspace file tools and test execution belonged to the prototype | Current DevTools records developer-supplied runs; it is not a customer-code sandbox |
| Persistence | Earlier work used JSONL traces according to project history | SQLite via sia/store.py and sia_devtools/store.py with migrations |
| Evaluation | Task-level debugging and tests | Deterministic and model-based scorers, versioned datasets, paired comparisons and regression policy |
| Learning | Task-level tool loop | Lessons/playbooks in managed-agent path; reviewed feedback and prompt versioning in DevTools |
| API / CLI | Local prototype | FastAPI, importable Python SDK and sia CLI |
| Testing | Historical task tests | tests/test_*.py plus GitHub Actions |

Do not describe the archived prototype as a current directory or assume all its capabilities carried into the product.

# Chapter 3 — Technology stack

| Technology | Role | Where used | Failure / replaceability |
|---|---|---|---|
| Python 3.11+ | Main language | pyproject.toml and Python modules | Essential to current code; another language requires a rewrite |
| setuptools / project metadata | Build and package | pyproject.toml | Replaceable if entry points and extras are preserved |
| httpx | HTTP for model providers, SDK and payments | sia/llm.py, sia_devtools/client.py, sia/billing.py | Network failures become typed errors; another HTTP client is possible |
| FastAPI | Hosted API and dependency injection | sia/api.py | Central to server; replacing it requires route/middleware rewrite |
| Pydantic | Request validation and field constraints | sia/api.py | Protects route logic from invalid payloads; validation limits must remain |
| SQLite | Persistent database | sia/store.py, sia_devtools/store.py | Simple local deployment; write contention and multi-replica constraints |
| SQLite FTS5 | Lexical full-text search for managed-agent documents and lessons | sia/store.py | Not semantic vector search; weak lexical matches can hurt retrieval |
| pytest | Tests | tests/, pytest.ini, CI | Only validates covered behaviors |
| GitHub Actions | Test/security CI and package release workflow | .github/workflows/ | Replaceable automation |
| Optional Sentry SDK | Error reporting when configured and installed | sia/api.py, pyproject optional extra | Without it, core service can still run |
| Razorpay HTTP API | Paid-plan orders and verification | sia/billing.py, sia/api.py | Requires provider credentials and availability |
| HTML/CSS/JavaScript | Public site and dashboard | web/, sia/dashboard.html | Dashboard is a static page served by FastAPI, not a React app |
| Docker | Container packaging | Dockerfile | Optional for local use; useful for repeatable deployment |
| Litestream example | Optional SQLite replication example | examples/deploy/litestream.yml | Example config does not prove live replication is configured |

### LLM layer

sia/llm.py implements a raw-HTTP client. It has branches for Gemini and Anthropic, and OpenAI-compatible chat-completions endpoints for providers in OPENAI_COMPATIBLE, including OpenAI, Groq, OpenRouter, Together, Mistral, DeepSeek and Ollama. It validates provider/model settings, uses timeouts, retries selected transient errors and supports comma-separated fallback models. This avoids a provider SDK dependency per service, but SIA owns request and response parsing, so provider API changes can break a branch.

### Dependency map

~~~mermaid
flowchart TD
  Caller[Python caller or browser] --> SDK[sia_devtools client]
  Caller --> API[FastAPI: sia/api.py]
  SDK -->|hosted HTTPS| API
  SDK -->|local mode| DevStore[sia_devtools/store.py]
  API --> DevStore
  API --> Store[sia/store.py]
  Store --> DB[(SQLite)]
  DevStore --> DB
  API --> Agent[sia/agent.py]
  Agent --> LLM[sia/llm.py]
  LLM --> Providers[External LLM APIs]
  API --> Billing[sia/billing.py]
  Billing --> Razorpay[Razorpay API]
  API --> Dashboard[sia/dashboard.html]
  API --> Site[web static files]
~~~

Not every request uses every component. DevTools trace ingestion does not inherently call an LLM; managed-agent run/study/improve routes do.

# Chapter 4 — Repository structure

| Path | Responsibility | Main symbols or contents | Dependencies |
|---|---|---|---|
| sia/agent.py | Managed-agent reasoning and learning | Agent, build_system, run, critique, reflect, feedback, verify_example, study, scores, evaluate, improve | Store, LLM, compare_scores |
| sia/store.py | Managed-agent persistence | Store, SCHEMA, MIGRATIONS, search_chunks, relevant_lessons, add_episode, add_example, add_playbook | SQLite, shared migration helper |
| sia/llm.py | Provider HTTP abstraction | LLM, LLMError, Unavailable, parse_json | httpx |
| sia/api.py | FastAPI, schemas, auth, limits and routes | app, request models, key_auth, tenant, owned_agent, metered | Both stores, Agent, LLM, billing, security |
| sia/security.py | Request/security helpers | Window, Slots, BodyLimit, CSP, headers, admin-key checks | Used by API |
| sia/billing.py | Razorpay client and signature validation | Razorpay and signature helpers | httpx and provider |
| sia/ops.py | Operations commands | backup, restore, maintenance, logcheck, doctor | SQLite, filesystem, health endpoint |
| sia/__main__.py | Module entry point | delegates to sia_devtools.cli.main | CLI |
| sia/dashboard.html | Hosted single-page dashboard | HTML/CSS/JS | Served by API |
| sia_devtools/client.py | Public SDK | SIAClient, Run, Dataset, Prompt, HTTP queue and local/hosted modes | DevStore, scoring, errors, httpx |
| sia_devtools/store.py | DevTools persistence, migrations, validation, redaction | DevStore, migrate, redactor, dataset/version/run schema | SQLite |
| sia_devtools/scoring.py | Scorers and release policy | Scorer, built-ins, compare_scores, gate | Used by SDK, CLI and managed-agent |
| sia_devtools/errors.py | Typed client/server exceptions | Error classes and status mapping | Client/API |
| sia_devtools/cli.py | sia command-line interface | parser and cmd_* handlers | SDK, LLM and ops |
| sia_devtools/__init__.py | Public import surface | SIAClient, Run, Dataset, Prompt, scorers, errors | SDK consumers |
| tests/test_sia.py | Managed-agent tests | fake LLM, run, feedback, lessons, study/improve, FTS | pytest |
| tests/test_devtools.py | SDK and DevTools tests | runs, datasets, evaluations and client behavior | pytest |
| tests/test_improvements.py | Improvement/release tests | proposals, comparisons and gate behavior | pytest |
| tests/test_security.py | Security and isolation tests | auth, ownership and security checks | pytest |
| tests/test_billing.py | Payment tests | mocked provider and signatures | pytest |
| tests/test_launch.py | Launch/operations tests | launch-facing behavior | pytest |
| examples/devtools_demo.py | End-to-end demo | offline scripted model and real-provider mode | SDK and optional LLM |
| examples/ci/sia-gate.yml | GitHub Actions gate example | CI commands | sia CLI |
| examples/deploy/ | Deployment examples | Litestream, Nginx | Optional ops |
| web/ | Static public site and legal pages | index, app.js, site.js, CSS, privacy/terms/refund | API serves files |
| .env.example | Safe configuration template | placeholders | Operators |
| pyproject.toml | Packaging, extras and CLI entry | dependencies, package data and entry point | pip/build/CI |
| requirements.txt, uv.lock, pytest.ini | Dependencies and test settings | lock/config files | local development/CI |
| Dockerfile | Container | Python 3.12 slim, non-root user, /data volume, health check | deployment |
| .github/workflows/tests.yml | CI | Python 3.11/3.12 test matrix, pip-audit and Bandit | GitHub Actions |
| docs/AUDIT.md | Readiness assessment | starting gaps, built work, limitations | maintainers |
| docs/SPEC.md | Historical build brief | pre-launch plan | historical context |
| docs/RUNBOOK.md | Operational instructions | backups, restore, maintenance and alerting | operators |
| docs/RELEASING.md | Release steps | package publishing | maintainers |
| SECURITY.md, LICENSE.md | Security policy and license | disclosure and license terms | users/maintainers |

Changing client.py can affect local and hosted SDK users. Changing either store can affect existing databases, so use migrations. Changing scoring.py can alter release decisions. Changing api.py can alter authorization for both API families. Changing agent.py changes model calls, quality, learning and cost.

# Chapter 5 — Architecture overview

SIA is a modular Python application with SQLite and a FastAPI server. The SDK can use local SQLite or the hosted API. The same API process serves DevTools routes and the older managed-agent routes.

~~~mermaid
flowchart TD
  A[Developer app or browser] --> B{Workflow}
  B -->|Instrument existing app| C[SIAClient]
  C --> D{Mode}
  D -->|Local| E[DevStore + local SQLite]
  D -->|Hosted| F[FastAPI /v1/projects]
  F --> G[DevStore]
  B -->|Managed agent| H[FastAPI /v1/agents]
  H --> I[Agent orchestration]
  I --> J[Managed Store]
  I --> K[LLM client]
  K --> L[Provider API]
  G --> M[(SQLite file)]
  J --> M
  F --> N[Dashboard]
~~~

- **SDK:** public integration surface; users should not need table names or SQL.
- **Agent orchestration:** draft/critique/revise and across-task learning for managed agents.
- **LLM layer:** provider selection, HTTP requests, retries and JSON parsing.
- **Scoring:** managed-agent critique is LLM-graded; DevTools supports deterministic scorers and optional LLM judge.
- **Feedback:** corrections are pending until reviewed.
- **Evaluation/release:** stores evaluations and comparisons; normal activation requires acceptable evidence.
- **Persistence:** Store holds managed-agent knowledge/lessons/episodes/playbooks; DevStore holds projects/runs/feedback/datasets/evaluations/versions/audit.
- **Security/operations:** middleware, auth, rate limits, billing and backup commands.

A component diagram shows dependencies. A sequence diagram shows the order of one operation. An ER diagram shows data relationships. A deployment diagram shows processes, machines and external services. Do not mistake a source dependency diagram for a production deployment guarantee.

# Chapter 6 — Request lifecycle

## Managed-agent route

POST /v1/agents/{agent_id}/run in sia/api.py is the entry point. FastAPI validates RunIn, authenticates the organization, resolves the agent through owned_agent, constructs the LLM client, and calls the operation through metered/call helpers.

Call chain:
1. FastAPI receives request.
2. Pydantic validates task, learn and max_revisions.
3. key_auth resolves organization; owned_agent scopes the lookup to that organization.
4. llm() constructs LLM from provider/key/model/base URL headers and optional server default.
5. metered() acquires an LLM concurrency slot and records usage even on failure.
6. Agent.run() calls _context() to retrieve knowledge and lessons and build the system prompt.
7. LLM drafts an answer.
8. If learning is enabled, critique() grades against rubric and optional expected answer.
9. If score is below PASS_SCORE=0.75, revisions can run up to the requested budget, preserving the highest-scoring answer.
10. reflect() may distil lessons.
11. Used lessons receive credit; episode is saved.
12. The API returns answer, score, attempts, critique, lessons and episode ID.
13. call() maps LLM errors to 502, missing records to 404 and invalid values to 400. Learning failures can be returned as learning_error while preserving an answer.

Representative excerpt from sia/agent.py:

~~~python
PASS_SCORE = 0.75
verdict = self.critique(task, answer, knowledge, expected)
best = (verdict["score"], answer, verdict)
while best[0] < PASS_SCORE and attempts <= max_revisions:
    # revise using previous answer and critique
    ...
    verdict = self.critique(task, answer, knowledge, expected)
    if verdict["score"] > best[0]:
        best = (verdict["score"], answer, verdict)
~~~

PASS_SCORE is a threshold, not a guarantee. critique returns a normalized 0–1 score. best stores the strongest attempt. The loop revises only below threshold and while budget remains. A new attempt replaces best only when its score is strictly higher. The grader is an LLM, so the score is not proof of correctness.

## DevTools trace

The SDK starts from SIAClient.trace, SIAClient.start_run or Run.end in sia_devtools/client.py. It tracks metadata/timing, redacts and serializes values, then persists locally or queues hosted batches.

- Local mode writes through DevStore to SQLite, default sia_local.db.
- Hosted mode sends batches to POST /v1/projects/{project_id}/runs. The API authenticates, validates, checks ownership/limits, then stores the traces.
- SDK instrumentation only sees calls the developer wraps or explicitly logs. It does not magically capture every model/tool call.

## Teaching example: ₹730 annual subscription cancelled after 200 days

This is not a built-in refund calculator. Without policy content or explicit instructions, the managed agent should not invent a refund promise. Any refund formula must be defined and independently verified.

# Chapter 7 — Agent engine

### build_system(agent, playbook, lessons, knowledge)

Composes the system prompt from domain, instructions, playbook, lessons and retrieved knowledge. It appends a rule to admit missing information. It does not call the model or write to storage. Prompt order is tested in tests/test_sia.py; add tests when changing order.

### Agent._context(task, playbook=None)

Retrieves document chunks and relevant lessons scoped by the agent ID. If no playbook is supplied, it loads the active playbook. Returns system prompt, selected lessons and knowledge.

### Agent.critique(task, answer, knowledge=(), expected=None)

Builds a review prompt with task, answer, optional reference answer, reference chunks and rubric. Calls llm.json at temperature 0, normalizes score 0–10 into 0–1, clamps it, and returns score/issues/suggestion. Invalid score values can normalize to zero. Provider or JSON failures raise LLMError.

### Agent.run(task, learn=True, max_revisions=2, expected=None)

Retrieves context, drafts an answer, and returns after one generation call when learn=False (recorded as unscored). Otherwise it critiques and revises, keeping the best score, reflects to extract lessons, credits used lessons and stores an episode. If critique/revision fails with LLMError, it can preserve the best available answer and include learning_error. The initial draft occurs before the try block that catches LLMError in the inspected source, so an initial generation failure can propagate.

### Agent.reflect() and _save_lessons(prompt, source)

reflect skips extraction for a first-attempt score at least 0.95. Otherwise it requests at most two general imperative lessons under 30 words. _save_lessons catches LLMError, validates the returned list and stores valid strings. Reflection is best-effort.

### Agent.feedback(episode_id, rating, correction=None, comment=None)

Checks the episode belongs to this agent, stores rating/correction, credits lessons using (rating - 1) / 4, and creates a pending example when a correction is supplied. API models constrain rating; internal callers should still validate input.

### Agent.verify_example(example_id)

Looks up the example within the agent, marks it verified, then extracts general lessons from task, previous answer and verified correction. Re-review raises ValueError. This reduces prompt-poisoning risk.

### Agent.study(rounds=3)

Selects random document chunks, asks the LLM to create a question and source-grounded answer, stores a synthetic evaluation example, runs the question with the expected answer, and reports scores/lessons. It raises if there are no documents. Synthetic examples can contain generator/source errors and should not be treated as human-reviewed truth.

### Agent.scores() and evaluate()

scores evaluates every example with a supplied playbook; each trial calls the target model and then critique. evaluate returns the mean. More examples/trials mean more latency and cost.

### Agent.improve(max_examples=8, trials=1, activate=False)

Loads evaluation examples, active playbook, top lessons and recent failures; generates a candidate playbook; scores current and candidate on the same examples; calls compare_scores; stores the candidate whether accepted or not; activates only when activate=True and verdict is improved. Otherwise the current active version remains.

### significant_gain(old, new)

Delegates to compare_scores with the managed-agent policy, but this helper uses a one-example minimum override. Normal improve uses MIN_EXAMPLES=3. Inspect the actual policy at the call site before relying on thresholds.

# Chapter 8 — What self-improvement actually means

| Mechanism | Meaning | Implemented? |
|---|---|---|
| Model training / fine-tuning | Updates model weights | No |
| Retrieval-augmented generation | Retrieves material and puts it in the prompt | Yes, lexical FTS5 retrieval for managed-agent docs/lessons |
| Feedback-driven memory | Stores reusable lessons from experiences | Yes, managed-agent path |
| Prompt/playbook optimization | LLM proposes revised instructions | Yes, managed-agent improve and DevTools version workflow |
| Evaluation-driven selection | Compares baseline and candidate on test cases | Yes, shared scoring/comparison and DevTools |
| Runtime source-code rewrite | Agent modifies arbitrary application code | No |

## Lifecycle

1. run drafts and critiques.
2. reflect extracts lessons from imperfect episodes.
3. Store persists lessons and utility/usage signals.
4. Later tasks retrieve relevant lessons.
5. feedback records ratings and may create a pending correction.
6. verify_example promotes reviewed corrections and can extract lessons.
7. study creates synthetic questions from documents.
8. improve drafts a playbook.
9. scores evaluates current and candidate versions on the same examples.
10. compare_scores returns a verdict.
11. Candidate activation requires an explicit activation request and improved verdict.

The DevTools path keeps the target application under developer control. Feedback is reviewed, datasets are versioned, prompt versions can be passed into the target function, and comparison/approval/activation control release. The API stores DevTools artifacts; its module documentation says those routes do not send customer payloads to model providers.

## Illustrative paired-score example

These fictional scores are for teaching only:

| Example | Baseline | Candidate | Gain |
|---|---:|---:|---:|
| Eligibility | 0.70 | 0.90 | +0.20 |
| Cancellation date | 0.90 | 0.90 | 0.00 |
| Final-sale exception | 1.00 | 0.50 | -0.50 |
| Credit calculation | 0.70 | 0.80 | +0.10 |
| Missing policy | 0.80 | 0.90 | +0.10 |
| Mean | 0.82 | 0.80 | -0.02 |

Three examples improved, but a critical case regressed badly. The mean fell, and a critical-regression policy should block release even if the mean had increased. compare_scores in sia_devtools/scoring.py uses paired values, minimum sample size, a standard-error noise threshold and regression/critical-case rules. Verdicts include improved, no_improvement, inconclusive and regressed.

Higher aggregate score does not guarantee better production behavior: evaluation noise, overfitting, biased feedback, contradictory lessons, metric mismatch, dataset drift and synthetic-data errors all matter. Keep critical regression tests and a separate held-out set for high-impact changes.

# Chapter 9 — Database and data model

There are two persistence modules, and both may use the same database file in the hosted server.

## Managed-agent schema in sia/store.py

| Table | Purpose |
|---|---|
| tenants | Organization identity and legacy key hash; migrations add plan/payment fields |
| api_keys | Multiple keys, scope, prefix, creation/last-use/revocation metadata and hash |
| agents | Agent name, domain, instructions, rubric and tenant ID |
| playbooks | Versioned strategy prompts, score, active flag, note and timestamp |
| docs | Uploaded knowledge document metadata |
| chunks (FTS5) | Searchable document chunks and agent/document identifiers |
| lessons | Reusable lessons, source, uses, cumulative utility and active status |
| lessons_fts (FTS5) | Lesson text search index |
| episodes | Task, answer, score, critique, attempts, lesson IDs, feedback and timestamp |
| examples | Input/expected output, source, review status and originating episode |
| usage | Tenant/agent/action, model-call count, units, timestamp and optional project |
| payments | Razorpay order, organization, plan, amount/currency, status and payment metadata |
| access_requests | Requests for access and review status |
| schema_migrations | Applied migration identifiers |

## DevTools schema in sia_devtools/store.py

| Table | Purpose |
|---|---|
| projects | Project identity, organization ownership, settings and creation time |
| runs | Trace/run fields, parent-child relationship, payloads, status, timing, usage, tags, metadata, scores and prompt-version reference |
| feedback | Run-linked rating/correction, category, reviewer and review state |
| datasets | Dataset identity, name and version history |
| dataset_examples | Input/expected output, metadata, critical flag and version membership |
| evaluations | Evaluation metadata and dataset reference |
| eval_results | Per-example output, scores, errors and timing |
| comparisons | Baseline/candidate links, verdict, reason, gain, uncertainty and regression policy |
| versions | Named, numbered prompt versions and status/content |
| active_versions | Active version by project and prompt name |
| audit | Project-level actor/action/detail history |

Use the actual migration SQL for exact columns and constraints. A column named like a foreign key does not by itself prove SQLite enforces a foreign-key constraint.

### Conceptual ER diagram

~~~mermaid
erDiagram
  TENANTS ||--o{ AGENTS : owns
  TENANTS ||--o{ API_KEYS : authenticates
  AGENTS ||--o{ EPISODES : records
  AGENTS ||--o{ LESSONS : learns
  AGENTS ||--o{ PLAYBOOKS : versions
  AGENTS ||--o{ DOCS : owns
  DOCS ||--o{ CHUNKS : split_into
  AGENTS ||--o{ EXAMPLES : evaluates
  TENANTS ||--o{ PROJECTS : owns
  PROJECTS ||--o{ RUNS : records
  RUNS ||--o{ RUNS : parent_child
  RUNS ||--o{ FEEDBACK : receives
  PROJECTS ||--o{ DATASETS : owns
  DATASETS ||--o{ DATASET_EXAMPLES : contains
  PROJECTS ||--o{ EVALUATIONS : runs
  EVALUATIONS ||--o{ EVAL_RESULTS : contains
  PROJECTS ||--o{ COMPARISONS : compares
  PROJECTS ||--o{ VERSIONS : versions
~~~

This is conceptual and does not assert every relationship is enforced by SQL foreign keys.

### FTS5 versus vector search

FTS5 is lexical text search. It matches tokens and ranks text matches; it does not compute semantic embeddings or nearest-neighbor vectors. The managed-agent path calls search_chunks() and relevant_lessons(). If a question uses different wording from a source, retrieval may be weak.

### Transactions and concurrency

SQLite supports multiple readers but serializes writes. WAL can improve reader/writer coexistence but does not make SQLite horizontally scalable. In-memory rate limiters and concurrency slots are per process. The audit explicitly identifies SQLite and a single server process as the current operating model. DevTools dataset membership uses added_in/removed_in version fields so previous snapshots can be reconstructed when reads respect the requested version.

For schema changes: add a numbered migration, test fresh and existing databases, preserve old records, and document backup/rollback requirements.

# Chapter 10 — LLM integration

LLM in sia/llm.py takes provider, API key, optional model/fallback chain and optional base URL. It exposes text and JSON methods, tracks call count and latest successful usage metadata, and delegates HTTP to _post().

- Gemini: generateContent endpoint and generation config.
- Anthropic: Messages API.
- OpenAI-compatible services: chat/completions endpoint.

Network errors and selected transient HTTP statuses are retried. Exhausted transient errors become Unavailable and can trigger the next model in the fallback chain. Other HTTP errors become LLMError. parse_json tolerates fenced JSON and attempts to extract an object from surrounding text.

The hosted managed-agent API reads X-LLM-Provider, X-LLM-Key, X-LLM-Model and X-LLM-Base-URL. SIA_DEFAULT_LLM_KEY can supply a server-side Gemini default. Custom base URLs and Ollama require SIA_ALLOW_CUSTOM_BASE_URL=1 because of SSRF risk. The CLI improvement path reads SIA_LLM_KEY or GENAI_API_KEY; check the command handler for exact provider/model flags.

A single managed-agent request may make multiple calls: draft, critique, revision/critique pairs, and reflection. Evaluation multiplies calls by example count and trials. last_usage reflects the latest successful call, not a complete usage history for every call.

To add a provider, keep provider-specific URL, headers, request body and response parsing inside LLM._text(). Add mocked HTTP tests for success, malformed response, auth failure, transient retry, fallback order and key sanitization.

# Chapter 11 — API and CLI

sia/api.py exposes both /v1/projects/... (DevTools) and /v1/agents/... (managed agents), plus health, dashboard, access/admin and billing routes.

| Route family | Examples | Purpose |
|---|---|---|
| Health/UI | GET /health, GET /dashboard | Health check and dashboard |
| Admin/access | POST /v1/tenants, /v1/admin/orgs/{org_id}/plan, /v1/access-requests... | Provisioning and access review |
| Keys/usage | /v1/keys, /v1/keys/rotate, /v1/keys/{key_id}/revoke, /v1/usage | Key and usage management |
| Projects | /v1/projects, /v1/projects/{project_id}/overview, /audit | Project setup and status |
| Runs/feedback | /runs, /runs/export, /runs/{run_id}, /runs/{run_id}/feedback, /feedback/{feedback_id}/review | Trace capture and review |
| Datasets | /datasets, /datasets/{dataset_id}, /history, /examples | Versioned test cases |
| Evaluation | /evaluations, /evaluations/{evaluation_id}, /comparisons | Store/inspect results and comparisons |
| Prompt versions | /versions, /versions/active, /{name}/{version}/approve, /reject, /activate | Controlled prompt release |
| Managed agents | /v1/agents, /knowledge, /examples, /run, /feedback, /study, /evaluate, /improve, /lessons, /episodes, /playbooks | Managed agent operations |
| Billing | /v1/plans, /v1/billing, /orders, /verify, /webhook | Plans and Razorpay payment |

For exact method/body/status/auth, read the route decorator and matching Pydantic model in sia/api.py. DevTools models often inherit Strict, which forbids unknown fields.

### Authentication

key_auth resolves an organization from the API key. tenant requires full scope for privileged actions. Ingest-scoped keys are restricted to ingestion and active-prompt reads. owned_agent scopes an agent lookup to the organization. Project routes must similarly resolve project ownership. Admin operations use SIA_ADMIN_KEY. SIA_LOCAL_MODE=1 is loopback-only.

The CLI entry point is sia = sia_devtools.cli:main in pyproject.toml. sia/__main__.py delegates to the same main function. CLI families include sia eval, sia versions, sia dashboard, sia serve and operational commands such as backup, restore, maintenance, doctor and logcheck. Use sia --help in a real checkout for the exact live command syntax.

# Chapter 12 — Configuration

Authoritative sources: .env.example, sia/api.py, sia/llm.py, sia/security.py, sia/ops.py and sia_devtools/client.py.

| Variable | Purpose / source behavior |
|---|---|
| SIA_DB | Server SQLite path; API source default is sia.db |
| SIA_LOCAL_MODE | 1 enables loopback-only local mode |
| SIA_DEFAULT_LLM_KEY | Optional server-side Gemini key for managed-agent routes |
| SIA_ALLOW_CUSTOM_BASE_URL | 1 allows custom model endpoints / Ollama; review SSRF implications |
| SIA_ADMIN_KEY | High-entropy admin secret; weak/missing value disables admin API |
| SIA_MAX_BODY_BYTES | Request-body limit; source default 4 MiB |
| SIA_RATE_LIMIT | Per-organization requests/minute; default 120 |
| SIA_IP_RATE_LIMIT | Per-IP requests/minute; default 600 |
| SIA_AUTH_FAIL_LIMIT | Invalid-key attempt control; default 20 in the configured window |
| SIA_LLM_CONCURRENCY_PER_ORG | Concurrent managed-agent actions per organization; default 2 |
| SIA_LLM_CONCURRENCY | Global concurrent LLM action cap per process; default 16 |
| SIA_TRUSTED_PROXIES | Trusted reverse-proxy configuration for deployment |
| SIA_WEB_DIR | Optional public site directory |
| SIA_BASE_URL | Operations health/restore target; documented default localhost:8000 |
| SIA_LLM_KEY | CLI provider key |
| GENAI_API_KEY | CLI fallback key variable |
| SENTRY_DSN / SENTRY_ENVIRONMENT | Optional error reporting configuration |
| Razorpay variables | Inspect sia/billing.py and .env.example for exact names and required combinations |
| Retention/backup variables | Inspect .env.example, sia/ops.py and docs/RUNBOOK.md |

Not every setting is required. A missing value may disable a feature, select a default, or raise an error; inspect its reader before changing configuration. Never copy real secrets into docs or logs.

# Chapter 13 — Errors, logs and debugging

The API registers an exception handler for SIAError. call() maps LLM errors to 502, missing records to 404 and invalid values to 400. Middleware creates a request ID, applies security headers, logs JSON metadata without request headers/query/body, and returns a generic 500 with request ID for unhandled exceptions. Optional Sentry is configured to strip headers/body and local frame variables.

| Symptom | Likely cause | Inspect / next step |
|---|---|---|
| Missing LLM API key | Required key absent | sia/llm.py, llm() and .env.example; verify provider and key source |
| HTTP 400 | Invalid request or provider/model/base URL | Request model and LLM.__init__; read response detail |
| HTTP 401 | Missing, invalid or revoked key | key_auth and Store.tenant_by_key; verify Bearer header |
| HTTP 403 | Wrong scope/owner, local-mode restriction or admin key | tenant, owned_agent, project checks, require_admin |
| HTTP 413 | Body exceeds configured limit | BodyLimit and SIA_MAX_BODY_BYTES |
| HTTP 429 | Rate/auth-failure/concurrency limit | sia/security.py and api.py; honor Retry-After |
| HTTP 502 | Provider failed after retry/fallback | LLM._post and call(); inspect provider status/model/network |
| Score is None | learn=False or critique failed | Agent.run and learning_error |
| Candidate inactive | Verdict not improved or activation not requested | compare_scores, Agent.improve and version routes |
| Search weak | Lexical mismatch or wrong agent scope | search_chunks, relevant_lessons and ingestion |
| Trace missing | Sender not flushed, permanent send error, invalid project/key or rejected payload | SIAClient queue/sender/Run.end and server logs |
| Dashboard empty | Wrong DB path or local-mode config | SIA_DB, dashboard command and server logs |
| Backup/restore fails | Invalid copy, running server or wrong target URL | sia/ops.py and docs/RUNBOOK.md |

These are diagnostic hypotheses, not proof of a particular failure. Reproduce with minimal input and inspect logs without printing secrets or full customer payloads.

# Chapter 14 — Tests and verification

- tests/test_sia.py: fake-LLM tests for managed-agent runs, critiques, lessons, feedback review, study/improve, store search and JSON parsing.
- tests/test_devtools.py: SDK/DevTools functionality; inspect test names for exact coverage.
- tests/test_improvements.py: candidate, comparison and release behavior.
- tests/test_security.py: security and isolation controls.
- tests/test_billing.py: payment/signature logic with fake provider behavior.
- tests/test_launch.py: launch-facing product and operational expectations.

.github/workflows/tests.yml configures Python 3.11 and 3.12 and a security job using pip-audit and Bandit. The workflow skips Bandit B608, with comments that dynamic SQL fragments were reviewed and values are bound parameters.

**Verification performed here:** source/tree inspection only. No local checkout was created and no tests were run in this task. docs/AUDIT.md reports 50 offline tests and a historical baseline of 15 passed at the start of the project; those are project-record statements, not a new test result. Coverage percentage was not measured. docs/AUDIT.md states Razorpay was tested with a fake provider, not a real account, and a real-server restore drill remained outstanding at audit time.

Recommended additional tests: streamed body limits, cross-tenant access for every route, multiple worker behavior, SDK shutdown/flush, all schema migrations, concurrent activation, dataset edits during evaluation, restore drills, webhook replay and threshold-boundary behavior.

# Chapter 15 — Security and privacy

This is a source-informed risk assessment, not a penetration test.

| Area | Status / risk | Evidence and action |
|---|---|---|
| API keys | Hashes, multiple keys, scopes and revocation are implemented | sia/store.py, key_auth; test rotation/revocation |
| Tenant/project isolation | Ownership checks exist; tests are reported | owned_agent, project lookup and tests/test_security.py; external review still needed |
| Validation | Pydantic bounds and strict DevTools models exist | sia/api.py |
| Body size | BodyLimit exists, but docs/AUDIT.md says the limit relies on Content-Length and chunked uploads may not be capped before parsing | sia/security.py; enforce streaming limits |
| Rate limits | In-memory windows and slots | sia/security.py; per-process only |
| Custom LLM URL | Opt-in and URL validation | LLM.__init__; use network egress controls too |
| Logging | Headers/body/query omitted; provider key sanitized | sia/api.py and sia/llm.py |
| Redaction | Pattern-based | May miss unusual secrets or personal data |
| Prompt injection | Pending correction review and verified-only evidence reduce risk | Agent.feedback/verify_example and DevTools review flow; source documents remain untrusted |
| Dashboard XSS | Security docs describe escaped values and restrictive CSP | sia/dashboard.html, sia/security.py; verify all dynamic rendering |
| Payments | Signature checks and provider confirmation exist | sia/billing.py; real account integration not verified here |
| Admin key | Weak/missing key disables admin API | sia/security.py |
| Evaluation trust | SDK uploads scores; full-scope key can submit fabricated scores | docs/AUDIT.md; scores are evidence, not independent proof |
| Reviewer identity | No user accounts/SSO/per-person roles according to audit; reviewer/actor fields self-reported | docs/AUDIT.md; do not treat as strong identity evidence |

Priorities: P0, enforce streamed body limits and secure TLS/proxy/secrets/backup; P1, external security review and authenticated reviewer identities; P1, decide whether release evidence must be server-executed; P2, shared rate limiting and abuse monitoring before horizontal scaling.

# Chapter 16 — Multi-user and concurrency

The current hosted topology is a single FastAPI process with SQLite by default, according to docs/AUDIT.md and Dockerfile.

- SQLite allows concurrent readers but serializes writes; WAL improves some reader/writer coexistence but does not remove all contention.
- Window and Slots state are in process memory. Multiple processes/replicas have separate counters.
- Managed-agent LLM concurrency is capped per organization and globally per process.
- The SDK sends hosted trace batches in a background thread; shutdown and permanent failure behavior deserve explicit tests.
- Agent.study() and Agent.improve() run synchronously in request handling according to the audit, so long model calls can occupy a worker.
- Run export can load up to 100,000 rows into memory according to docs/AUDIT.md.

Local single-user use is a good fit. A small private beta can work with backups, retention and monitoring. Before horizontal scaling, migrate shared state to a client-server database and shared limiter; use a durable job queue for long evaluations if measured latency/worker saturation warrants it. Do not introduce microservices or Kubernetes without a specific bottleneck.

# Chapter 17 — Cost model

Managed-agent request costs can include one draft, one critique, up to R revision/critique pairs, and optional reflection. Study and improve add calls. DevTools trace ingestion and deterministic scorers do not inherently call a model; LLM judging and candidate generation do.

Spreadsheet formulas:

- monthly total = runs × average model cost per run + hosting + storage + logs/monitoring + backups + payment fees
- managed-agent cost ≈ draft + critique + expected revisions × (revision + critique) + reflection
- evaluation cost ≈ examples × trials × (target-function cost + judge cost)

These are formulas, not actual bills. Current model prices and average token usage must be measured from the provider and application. last_usage only tracks the latest successful call; use explicit per-call accounting if accurate cost attribution is required.

Cost controls: deterministic scorers where possible, limit revision/trial counts, bound batch sizes and concurrency, avoid automatic improvement on every request, measure prompt/completion tokens, and cache only with a key that includes exact prompt/model/configuration.

# Chapter 18 — Change guide

| Task | Primary files | Must also update |
|---|---|---|
| Change provider/model defaults | sia/llm.py | .env.example, mocked provider tests |
| Add provider | sia/llm.py | response/error/retry tests and docs |
| Change managed-agent prompt order | sia/agent.py build_system | tests/test_sia.py |
| Change pass score | sia/agent.py PASS_SCORE | run/revision tests |
| Change release threshold | sia/agent.py POLICY and sia_devtools/scoring.py DEFAULT_POLICY | boundary/regression tests |
| Add lesson field | sia/store.py | migration and upgrade tests |
| Add trace field | sia_devtools/client.py, sia/api.py, sia_devtools/store.py | migration, redaction, serialization and tests |
| Add endpoint | sia/api.py | Pydantic model, auth dependency, ownership check and route tests |
| Add scorer | sia_devtools/scoring.py | public export, tests, docs |
| Add CLI command | sia_devtools/cli.py | parser/help, exit-code tests, docs |
| Change retrieval | sia/store.py | retrieval tests and eval set |
| Change dashboard | sia/dashboard.html or web/ | CSP and route checks |
| Change backups | sia/ops.py | tests/test_launch.py and docs/RUNBOOK.md |

Safe workflow: define expected behavior; trace public entry to implementation; write a failing test; make the smallest change; run targeted tests; run the full suite; inspect diff; update docs/config; test migrations against fresh and existing DBs; commit a focused change.

# Chapter 19 — Five practical walkthroughs

## A. Normal managed-agent request

Create an agent, add knowledge, POST to /v1/agents/{agent_id}/run, follow route dependencies into Agent.run, then inspect episode_id through the episodes route or Store.episode(). Debug route → Agent.run → Store/LLM methods.

## B. Human feedback

Managed-agent feedback records rating and optionally creates a pending example. verify_example marks it verified and may extract lessons. DevTools feedback is linked to a run and reviewed before admission to a dataset. Use the exact ReviewIn schema in sia/api.py.

## C. Improvement cycle

Ensure a dataset exists; record baseline; propose candidate from verified evidence; evaluate both versions on the same examples; compare; approve only with a suitable comparison; activate explicitly; inspect audit trail and preserve rollback. In the managed-agent path, improve(activate=False) stores a candidate without activating it.

## D. Debug provider failure

For a 503, capture request ID and sanitized logs. Follow metered → call → Agent.run → LLM.text → _post. The HTTP client retries selected transient errors and may move to a fallback model. Check provider status, model name, quota, network and key configuration. Reproduce using mocked HTTP responses; do not log secrets.

## E. Add a scorer

Inspect Scorer, scorer(), built-in scorers and resolve() in sia_devtools/scoring.py. Implement a pure function with output, expected and example arguments. Add it to the resolver/registry if appropriate, export it from sia_devtools/__init__.py if public, test matching/mismatching/malformed cases, and document whether deterministic or model-based.

# Chapter 20 — Glossary

See SIA_GLOSSARY.md for the categorized glossary. Essential terms: an **episode** is a managed-agent task record; a **lesson** is reusable guidance; a **playbook** is a versioned strategy prompt; a **trace/run** records an existing application execution; a **dataset** is a versioned set of evaluation examples; a **scorer** judges output; a **baseline** is the current version; a **candidate** is a proposed version; a **regression** is a case that got worse; FTS5 is lexical search, not vector search; a **tenant** is an organization boundary; a **migration** is a repeatable schema change; an **idempotent** operation is safe to repeat; a **CI gate** returns a failing exit code when release criteria fail.

# Chapter 21 — Final assessment

## Strengths

- Clear workflow from traces to reviewed feedback to evaluation and release.
- Local-first SDK without a hosted account.
- Centralized scoring and release policy.
- Separation between managed-agent and DevTools SDK concerns.
- Explicit ownership checks, key scopes, request validation, redaction, billing checks and operational commands.
- Fake-LLM tests that can run without a live provider.

## Fragile or incomplete

- SQLite and in-memory coordination limit multi-process scaling.
- Body-size enforcement based on Content-Length does not fully protect chunked uploads.
- No per-person identity/SSO/roles per audit; reviewer identity is self-reported.
- SDK-uploaded scores can be fabricated by a key holder.
- Pattern redaction can miss sensitive content.
- Managed-agent study/improve work is synchronous.
- Large export can consume substantial memory.
- Audit notes real Razorpay account testing and a real-server restore drill were outstanding at audit time.
- The gate only covers behavior represented by the dataset and target function; it cannot guarantee general correctness.

## Prioritized recommendations

| Priority | Work | Effort | Benefit |
|---|---|---|---|
| P0 | Enforce streaming body limits | Medium | Resource-exhaustion protection |
| P0 | Verify TLS, proxy trust, secret storage, backup permissions and restore | Medium | Protect data and availability |
| P0 | Run staging restore drill and record recovery time | Medium | Demonstrate recoverability |
| P1 | Add authenticated users and reviewer roles | High | Trustworthy shared review/audit |
| P1 | Add isolation, migration, sender-shutdown and release-gate edge tests | Medium | Reduce data leakage and release risk |
| P1 | Queue long evaluations if measured latency warrants it | Medium–High | Protect API responsiveness |
| P2 | PostgreSQL and shared rate limiter before replicas | High | Concurrent hosted growth |
| P2 | Metrics for DB locks, queue, LLM latency/cost, 4xx/5xx and evaluation outcomes | Medium | Measurable operations |
| P2 | Decide whether evaluation should run server-side for stronger trust | High | More credible gate |
| P3 | Async SDK and framework adapters after demand validation | Medium–High | Expand reach without diluting focus |

## 30-day learning plan

- Week 1: trace SIAClient.trace through queue/storage; follow one /v1/projects route; read DevStore migration code.
- Week 2: understand compare_scores and dataset versioning; add a deterministic scorer and threshold boundary tests.
- Week 3: test auth, project ownership, key scopes, body limits and redaction; reproduce a denied cross-tenant request.
- Week 4: follow docs/RUNBOOK.md in a safe local environment; inspect CI results; write a design note for the next readiness improvement.

**Bottom line:** SIA's strongest current claim is not that it makes every AI agent smarter. It gives developers a repeatable way to record AI behavior, turn reviewed failures into tests, and control prompt releases with measured evidence. Preserve that focused value while strengthening operations and identity controls for a trustworthy hosted service.
