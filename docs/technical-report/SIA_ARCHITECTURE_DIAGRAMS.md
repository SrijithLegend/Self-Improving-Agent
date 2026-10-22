# SIA DevTools — Architecture Diagrams

> Source snapshot: main tree 72be749749ebf228786b522ca904d81bd1954b9c. Diagrams describe current code unless explicitly marked PROPOSED. ER relationships are conceptual unless enforced by SQL.

## 1. Complete system architecture — current

~~~mermaid
flowchart TD
  User[Developer or browser] --> SDK[sia_devtools client]
  User --> API[FastAPI: sia/api.py]
  SDK -->|hosted mode| API
  SDK -->|local mode| DStore[DevStore]
  API --> DStore
  API --> MStore[Store]
  DStore --> DB[(SQLite)]
  MStore --> DB
  API --> Agent[Managed Agent]
  Agent --> LLM[LLM HTTP abstraction]
  LLM --> Model[External model provider]
  API --> Billing[Razorpay integration]
  Billing --> RZ[Razorpay API]
  API --> Dash[sia/dashboard.html]
  API --> Web[web static site]
~~~

**What it shows:** SDK users can write directly to local SQLite or send data to the hosted API. Managed-agent routes additionally call the LLM abstraction. DevTools trace ingestion does not inherently need a model call.

**Dependencies and failures:** SQLite failure affects persistence; provider failure affects managed-agent or model-based evaluation operations; Razorpay failure affects payment flows. The diagram does not imply separate services or separate database servers.

## 2. Component dependencies — current

~~~mermaid
flowchart LR
  CLI[sia_devtools/cli.py] --> Client[sia_devtools/client.py]
  CLI --> Ops[sia/ops.py]
  Client --> Scoring[sia_devtools/scoring.py]
  Client --> DStore[sia_devtools/store.py]
  Client --> Errors[sia_devtools/errors.py]
  API[sia/api.py] --> DStore
  API --> MStore[sia/store.py]
  API --> Agent[sia/agent.py]
  API --> Security[sia/security.py]
  API --> Billing[sia/billing.py]
  Agent --> MStore
  Agent --> LLM[sia/llm.py]
  Agent --> Scoring
  MStore --> SQLite[(SQLite)]
  DStore --> SQLite
~~~

**What it shows:** arrows mean “uses/calls.” The two store classes serve distinct data models, although the hosted process can point them at the same database file.

**Key lesson:** SDK changes often affect local and hosted modes. Schema changes belong in the correct store migration, not in route code alone.

## 3. Managed-agent request sequence — current

~~~mermaid
sequenceDiagram
  participant C as Caller
  participant F as FastAPI
  participant A as Agent
  participant S as Store
  participant L as LLM
  C->>F: POST /v1/agents/{agent_id}/run
  F->>F: Validate RunIn and authenticate
  F->>S: Resolve agent within tenant
  F->>L: Construct provider client
  F->>A: metered(call Agent.run)
  A->>S: Retrieve chunks, lessons, active playbook
  A->>L: Generate draft
  alt learn=true
    A->>L: Critique draft (JSON)
    loop While below PASS_SCORE and budget remains
      A->>L: Generate revision
      A->>L: Critique revision
    end
    A->>L: Reflect and extract lessons (when needed)
    A->>S: Credit lessons and save episode
  else learn=false
    A->>S: Save unscored episode
  end
  A-->>F: Answer and episode metadata
  F->>S: Record usage in finally path
  F-->>C: JSON response
~~~

**What it shows:** Agent.run is the orchestrator; Store provides retrieval and persistence; LLM performs external calls. learn=false skips critique, revision and lesson extraction, and stores an unscored episode.

**Failure points:** initial generation errors can propagate; critique/revision errors are best-effort in the inspected implementation; persistence failures can prevent a complete response. The API meters usage around LLM-using operations.

## 4. DevTools trace ingestion sequence — current

~~~mermaid
sequenceDiagram
  participant App as Developer app
  participant SDK as SIAClient
  participant Q as Background sender
  participant API as FastAPI
  participant DB as DevStore/SQLite
  App->>SDK: Start run / @trace
  SDK->>SDK: Capture timing, output, error, metadata
  SDK->>SDK: Redact and serialize payload
  alt Local mode
    SDK->>DB: Persist run locally
  else Hosted mode
    SDK->>Q: Queue completed run
    Q->>API: POST /v1/projects/{id}/runs (batch)
    API->>API: Authenticate and validate
    API->>DB: Check ownership, limits, store run
    API-->>Q: Accepted / error response
  end
~~~

**What it shows:** hosted mode separates app execution from network delivery with a background queue. Local mode writes to SQLite. SDK instrumentation only captures what the developer wraps or explicitly logs.

**Potential failure:** shutdown, permanent server errors, credentials, or rejected payloads can leave traces unsent. Test flush/close behavior before relying on traces for compliance or billing.

## 5. Feedback review sequence — current concept across two APIs

~~~mermaid
sequenceDiagram
  participant App as Agent or customer app
  participant API as API or SDK
  participant DB as Store
  participant Reviewer as Reviewer
  App->>API: Submit rating and correction
  API->>DB: Persist feedback as pending
  Reviewer->>API: Verify or reject
  alt Verified
    API->>DB: Admit example to trusted dataset
    API->>DB: Record review and provenance
    API->>DB: Managed-agent path may extract lesson
  else Rejected
    API->>DB: Mark rejected; do not trust as eval evidence
  end
~~~

**What it shows:** a correction is not automatically trusted. Managed-agent Agent.feedback creates a pending example; verify_example promotes it and may extract lessons. DevTools uses run-linked feedback and a review operation, with dataset admission controlled by the review payload.

**Key risk:** reviewer fields are self-reported in the current audit. Without user accounts, a name in a field is not proof of identity.

## 6. Self-improvement and prompt promotion — current

~~~mermaid
flowchart TD
  A[Recorded failures and verified feedback] --> B[Build evaluation examples]
  B --> C[Select same examples for comparison]
  C --> D[Generate candidate instructions or prompt]
  D --> E[Evaluate baseline]
  D --> F[Evaluate candidate]
  E --> G[Paired compare_scores]
  F --> G
  G --> H{Verdict}
  H -->|improved and policy passes| I[Approve candidate]
  H -->|regressed / no improvement / inconclusive| J[Keep candidate inactive or reject]
  I --> K[Explicit activation]
  K --> L[Active version]
  L --> M[Audit history and rollback path]
~~~

**What it shows:** candidate generation, evaluation, approval and activation are distinct stages. Managed-agent Agent.improve defaults to activate=false. DevTools provides separate version operations and comparison policy.

**What it cannot prove:** a candidate can only be judged on the dataset, target function and metrics exercised. It does not guarantee general correctness or model-weight improvement.

## 7. Database relationship map — conceptual

~~~mermaid
erDiagram
  TENANTS ||--o{ API_KEYS : owns
  TENANTS ||--o{ AGENTS : owns
  TENANTS ||--o{ PROJECTS : owns
  AGENTS ||--o{ EPISODES : records
  AGENTS ||--o{ LESSONS : learns
  AGENTS ||--o{ PLAYBOOKS : versions
  AGENTS ||--o{ DOCS : owns
  DOCS ||--o{ CHUNKS : indexed_as
  AGENTS ||--o{ EXAMPLES : evaluates
  PROJECTS ||--o{ RUNS : records
  RUNS ||--o{ RUNS : parent_child
  RUNS ||--o{ FEEDBACK : receives
  PROJECTS ||--o{ DATASETS : owns
  DATASETS ||--o{ DATASET_EXAMPLES : versions
  PROJECTS ||--o{ EVALUATIONS : owns
  EVALUATIONS ||--o{ EVAL_RESULTS : contains
  PROJECTS ||--o{ COMPARISONS : owns
  PROJECTS ||--o{ VERSIONS : owns
~~~

**What it shows:** conceptual identifiers and ownership, not proof that every edge is enforced with SQL foreign keys. Consult sia/store.py and sia_devtools/store.py migration SQL for exact constraints.

**Key dependency:** deleting or migrating a parent record must account for child rows and retention. Add migration tests before changing schema.

## 8. API request lifecycle — current

~~~mermaid
flowchart TD
  A[HTTP request] --> B[Middleware: request ID, IP limit, headers, JSON log]
  B --> C{Route and validation}
  C -->|DevTools| D[Authenticate key and scope]
  C -->|Managed agent| E[Authenticate and validate agent ownership]
  D --> F[Resolve project within organization]
  E --> G[Build LLM and Agent]
  F --> H[Store / compare / version operation]
  G --> I[Metered model operation]
  I --> J[Store episode and usage]
  H --> K[Response]
  J --> K
  K --> L[Return response with request ID]
~~~

**What it shows:** middleware surrounds route handling. Request models validate data; dependencies establish organization, scope and ownership; route code performs the operation. Unhandled errors become generic 500 responses with request IDs.

**Potential failure:** a route is secure only if its ownership dependency and store lookup are correct. Test wrong-tenant and wrong-scope requests for every resource family.

## 9. Error propagation — current

~~~mermaid
flowchart TD
  A[Input / provider / database operation] --> B{Failure type}
  B -->|Invalid request| C[Pydantic or ValueError]
  B -->|Missing record| D[KeyError / NotFound]
  B -->|Provider transient| E[Retry and fallback]
  E -->|Exhausted| F[Unavailable / LLMError]
  B -->|Unexpected exception| G[Middleware exception handler]
  C --> H[HTTP 400 or validation response]
  D --> I[HTTP 404 or typed NotFound]
  F --> J[HTTP 502 through call helper]
  G --> K[Sanitized HTTP 500 + request ID]
  H --> L[JSON access log]
  I --> L
  J --> L
  K --> L
~~~

**What it shows:** known errors map to stable responses; unexpected errors are logged server-side and hidden from clients. The SDK maps statuses to typed exceptions.

**Caution:** not every database exception is explicitly mapped by call(). Test partial writes and failure paths rather than assuming every error becomes a clean response.

## 10. Existing deployment topology — current/default

~~~mermaid
flowchart TD
  Client[SDK / browser] -->|HTTPS in deployment| Proxy[Optional reverse proxy]
  Proxy --> API[One FastAPI process / container]
  API --> SQLite[(Persistent SQLite file)]
  API --> LLM[External model API when required]
  API --> Pay[Razorpay for billing]
  API --> Sentry[Optional Sentry]
  Disk[Persistent /data volume] --- SQLite
~~~

**What it shows:** Dockerfile configures a non-root user, /data volume, health check and sia serve. SQLite and process-memory controls are local to the instance. HTTPS and proxy configuration depend on deployment; the diagram does not assert that a live deployment exists.

**Failure points:** disk loss, corruption, process failure, provider outage or incorrect proxy trust. Follow docs/RUNBOOK.md and test backup restoration.

## 11. Scaled deployment topology — PROPOSED, not current

~~~mermaid
flowchart TD
  Client[SDK / browser] --> Edge[TLS ingress / reverse proxy]
  Edge --> API1[Stateless API replica A]
  Edge --> API2[Stateless API replica B]
  API1 --> PG[(Managed PostgreSQL)]
  API2 --> PG
  API1 --> Limiter[(Shared rate-limit store)]
  API2 --> Limiter
  API1 --> Queue[Durable job queue]
  API2 --> Queue
  Queue --> Worker[Evaluation / improvement workers]
  Worker --> PG
  Worker --> Models[LLM providers]
  API1 --> Obs[Metrics, logs, alerts]
  API2 --> Obs
  Worker --> Obs
  PG --> Backup[Automated backups and restore tests]
~~~

**This is proposed, not implemented.** It becomes justified when measured SQLite write contention, API worker saturation or multi-replica needs exceed the single-instance design.

**Migration logic:** measure DB locks, latency, job duration and queueing; then migrate shared state to PostgreSQL and shared rate limits; move slow work to durable jobs; add replicas only after shared-state correctness tests pass. Avoid adopting a queue, Redis or multiple services before demonstrating the bottleneck.
