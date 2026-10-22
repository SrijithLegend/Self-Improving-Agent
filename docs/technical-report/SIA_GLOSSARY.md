# SIA Glossary

This glossary explains terms used by the current repository. When a capability is not implemented, that is stated.

## AI and LLM concepts

| Term | Simple definition | SIA example / why it matters |
|---|---|---|
| LLM | A large language model that generates text from input | The provider client in sia/llm.py calls model APIs for managed-agent answers and critiques |
| Inference | Using a trained model to produce an output | Drafting or critiquing an answer is inference; SIA does not train weights |
| Model weights | Numeric parameters learned during model training | SIA does not modify them |
| Fine-tuning | Training a model further on a dataset | Not implemented in SIA; SIA changes prompts and evaluation data instead |
| Prompt | Instructions and input sent to a model | build_system() composes domain instructions, playbook, lessons and knowledge |
| System prompt | High-priority instruction context supplied to the model | Built by sia/agent.py for managed agents |
| Context window | The amount of input/output a model can process in one call | Large documents, lessons and trace payloads can increase token use or exceed provider limits |
| Token | A unit of text processed by a model | Provider usage can report prompt and completion tokens |
| Temperature | A model setting affecting output variability | Critique uses temperature 0; generation may use a higher temperature |
| Embedding | A numeric representation of text often used for semantic similarity | No embedding pipeline is visible in the current retrieval path |
| Retrieval | Finding stored material relevant to the current task | Store.search_chunks() and relevant_lessons() retrieve knowledge and lessons |
| RAG | Retrieval-augmented generation: retrieve content and insert it into a prompt | The managed-agent context path is a lexical form of retrieval augmentation |
| Vector search | Searching for nearby embedding vectors | Not implemented by the inspected FTS5 retrieval path |
| Full-text search | Searching text using tokenization and text ranking | SQLite FTS5 indexes chunks and lessons in sia/store.py |
| Prompt injection | Untrusted content attempts to override instructions | User corrections are held pending until reviewed; uploaded documents still need cautious handling |

## Agent architecture

| Term | Simple definition | SIA example / why it matters |
|---|---|---|
| Agent | Orchestration that uses an LLM and context to complete a task | Agent in sia/agent.py manages draft, critique, revision and learning |
| Orchestration | Code coordinating multiple steps | Agent.run() sequences retrieval, generation, critique and persistence |
| Critique | A review of a generated answer against criteria | Agent.critique() asks an LLM for score, issues and suggestion |
| Revision | Another attempt based on critique | Agent.run() revises while score is below PASS_SCORE and budget remains |
| Episode | A stored managed-agent task, answer and outcome | Stored in episodes by Store.add_episode() |
| Lesson | Reusable guidance extracted from experience | Stored in lessons and retrieved for future tasks |
| Playbook | A versioned set of strategy instructions | Stored in playbooks and inserted into the system prompt |
| Knowledge chunk | A segment of an uploaded document | Stored in the chunks FTS5 index |
| Reflection | Summarizing what should be learned from an attempt | Agent.reflect() and _save_lessons() |
| Self-study | Creating practice questions from supplied documents | Agent.study() generates synthetic examples; these may need human review |

## Evaluation and improvement

| Term | Simple definition | SIA example / why it matters |
|---|---|---|
| Evaluation example | Input plus expected answer or grading reference | Managed-agent examples and DevTools dataset examples |
| Dataset | A collection of evaluation examples | DevTools datasets are versioned so results can be reproduced |
| Scoring rubric | Criteria used to judge an output | Agent.critique() uses the agent rubric or DEFAULT_RUBRIC |
| Scorer | A function that assigns a score to an output | sia_devtools/scoring.py includes exact_match, contains, json_match and regex support |
| LLM judge | A model asked to grade another model's output | Optional llm_judge scorer; can be inconsistent |
| Baseline | The current version used for comparison | Existing prompt version or playbook |
| Candidate | A proposed change not yet released | Generated candidate prompt/playbook |
| Paired comparison | Evaluate baseline and candidate on the same examples | compare_scores() computes per-example gains |
| Regression | A test case becomes worse after a change | Critical regressions can block release |
| Critical example | A case that must not get worse | Dataset example can be marked critical |
| Standard error | Estimate of uncertainty in an average | Used by comparison policy to avoid treating noise as reliable gain |
| Statistical significance | Evidence that a result is unlikely to be noise under a specified test | SIA uses a practical noise threshold; this does not prove universal quality |
| Overfitting | Optimizing for known tests but failing on new cases | Use held-out examples and realistic production cases |
| Playbook promotion | Making a candidate the active instruction version | Approval and activation are separate operations |
| CI gate | A command returns a failing exit code when a release policy fails | sia eval gate can be used in automation |
| Audit log | Record of important changes and decisions | DevTools audit table records project-level actions |

## Python and software architecture

| Term | Simple definition | SIA example / why it matters |
|---|---|---|
| Module | A Python file grouping related code | sia/agent.py, sia/store.py |
| Package | Modules distributed/imported together | sia and sia_devtools |
| Dependency injection | Pass a dependency into a component instead of constructing it internally | Agent receives Store and an LLM-like object, enabling fake-LLM tests |
| SDK | Software development kit used by another application | SIAClient is the public Python SDK |
| CLI | Command-line interface | sia command is registered in pyproject.toml |
| Decorator | A wrapper that changes or records function behavior | SIAClient.trace instruments a function |
| Context manager | A with-block that runs setup/cleanup around work | Run objects can be used to record a bounded execution |
| Exception | A Python object representing an error | LLMError and typed SIA errors |
| Retry | Repeating an operation after a transient failure | LLM._post() and SDK HTTP client retry selected errors |
| Timeout | Maximum time allowed for an operation | httpx clients have configured timeouts |
| Idempotency | Repeating an operation does not duplicate effects | Client-chosen IDs and deduplication protect selected ingestion operations |
| Mock | A fake dependency used to test code without the real service | FakeLLM drives offline managed-agent tests |
| Unit test | Tests a small function/component | Scorer and helper tests |
| Integration test | Tests components working together | API/store/auth tests exercise more of the application |
| CI/CD | Automated checks and delivery workflow | GitHub Actions runs tests and security scans |
| Migration | Repeatable schema change applied to a database | Store migration lists and schema_migrations tracking |

## APIs and integrations

| Term | Simple definition | SIA example / why it matters |
|---|---|---|
| API | A defined interface between programs | FastAPI exposes SIA operations over HTTP |
| Endpoint | One HTTP method and path | POST /v1/projects/{project_id}/runs |
| Request schema | Rules for acceptable input | Pydantic models in sia/api.py |
| HTTP status | Number describing response result | 401 means authentication failed; 429 means throttled |
| Authentication | Establishing which key/caller is presenting a request | key_auth resolves an organization |
| Authorization | Checking what that caller is allowed to do | tenant requires full scope; project/agent ownership must be checked |
| API key | Secret used to authenticate an integration | Organization keys are hashed in storage |
| Rate limit | Maximum requests over a period | Window in sia/security.py stores per-process counters |
| SDK instrumentation | Explicitly wrapping an existing function to record it | @sia.trace records the decorated function; it cannot see unwrapped calls |
| Provider fallback | Try another configured model after transient unavailability | LLM supports a comma-separated fallback chain |

## Databases and persistence

| Term | Simple definition | SIA example / why it matters |
|---|---|---|
| SQLite | File-based relational database | Local mode uses a SQLite database without a hosted account |
| WAL | Write-ahead logging mode that can improve reader/writer coexistence | Does not remove all write contention |
| Transaction | A group of database operations treated as one unit | Important when updating related records or approving access |
| Foreign key | A constraint tying a child row to a parent row | Inspect migrations before assuming the relationship is enforced |
| Index | Extra data structure that speeds up queries | Indexes exist for tenant, agent, usage and run queries |
| FTS5 | SQLite full-text search extension | Indexes document chunks and lessons |
| Data retention | Policy for how long records remain stored | DevTools project settings and maintenance operations support retention |
| Backup / restore | Copy stored data and recover it later | sia/ops.py and docs/RUNBOOK.md provide operational commands |

## Security

| Term | Simple definition | SIA example / why it matters |
|---|---|---|
| Tenant | Organization ownership boundary | Organization keys scope agents and projects |
| Tenant isolation | One organization cannot read another's data | Must be checked at every API resource lookup |
| Least privilege | Give a key only the access it needs | full and ingest key scopes |
| Secret management | Keeping credentials out of source, logs and user-visible output | Admin, LLM and payment secrets must remain server-side |
| Redaction | Removing or masking sensitive fields | DevTools payload redactor is pattern-based and not perfect |
| SSRF | Server-side request forgery, where an attacker causes a server to request an unintended URL | Custom LLM base URLs are guarded; network egress controls are still valuable |
| XSS | Cross-site scripting in a browser | Dashboard escaping and CSP reduce risk |
| TLS | Encryption for network connections | Use HTTPS for hosted SDK and dashboard traffic |
| Penetration test | Deliberate security testing by an independent reviewer | Not evidenced as completed in the audit |
| Reviewer identity | Verified identity of the person approving feedback | Current audit says user accounts/SSO/roles are absent; reviewer fields are self-reported |

## Scaling and operations

| Term | Simple definition | SIA example / why it matters |
|---|---|---|
| Concurrency | Multiple operations active at once | LLM slots cap concurrent managed-agent calls per process |
| Horizontal scaling | Adding more server instances | In-memory limits are not shared between replicas |
| Background job | Work processed outside the request-response path | Proposed for long evaluations if worker saturation is measured |
| Observability | Logs, metrics and traces that reveal system health | SIA has JSON access logs and optional Sentry; a metrics dashboard is still a gap in the audit |
| Queue | A buffer of work waiting to be processed | SDK has a background sender; durable server-side evaluation queue is proposed |
| Backpressure | Slowing/rejecting new work when capacity is reached | Rate limits, body limits and concurrency slots provide partial controls |
| Recovery point objective (RPO) | How much recent data can be lost after a failure | Must be defined and tested for deployment backups |
| Recovery time objective (RTO) | How quickly service should be restored | Validate through a real restore drill |
| PostgreSQL | Client-server relational database | Proposed if SQLite write contention or multi-replica requirements justify migration |
| Durable queue | Queue whose jobs survive process restart | Proposed for long-running evaluation/improvement operations |
