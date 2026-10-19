# Build Spec: Self-Improving Agent API (SIA)

> Hand this file to Claude Code. Its job is to build the whole system in this file without stopping to ask
> questions. Make reasonable default choices and write them down in the README.

## 1. Goal

Build a **multi-purpose, self-improving AI agent** that customers use through an **API**. A customer:

1. brings **their own LLM API key** (a free model by default),
2. creates an agent for **any field** (tax, law, medicine notes, support, sales, coding, …),
3. gives it **context**: instructions, documents, examples and a definition of a good answer,
4. uses it. The agent then **gets better on its own** the way a person does: it practises, gets critiqued,
   writes down what it learned, takes corrections, studies its material and keeps rethinking its approach.

It must be **commercially viable**: multi-tenant, secure, metered, deployable, and the improvement must be
**measurable**, not just claimed.

## 2. Hard constraints

- **Free model by default.** Use Google Gemini's free tier through raw REST (`generativelanguage.googleapis.com/v1beta`).
  Do not hardcode a model that may be retired. Use the alias `gemini-flash-latest` with `gemini-flash-lite-latest`
  as a fallback, and check the list with `GET /v1beta/models` before relying on any name.
- **Bring your own key (BYO-key).** Every request that uses the LLM carries the customer's provider key in a header.
  **Never store or log provider keys.**
- **Any provider.** Support Gemini, Anthropic, and any OpenAI-compatible endpoint (OpenAI, Groq, OpenRouter, Together,
  Mistral, DeepSeek, Ollama). Use raw HTTP via `httpx` instead of per-provider SDKs.
- **No fine-tuning.** The improvement has to work with any model behind an API, so it lives in retrieved knowledge,
  learned lessons and a versioned, gated "playbook" (the agent's system prompt). Every change must be inspectable
  and reversible.
- **Python 3.12, minimal dependencies:** `fastapi`, `uvicorn`, `httpx`, `pydantic`, `python-dotenv`, `pytest`.
  Use SQLite with **FTS5** for storage and retrieval (no vector DB, no embeddings service).
- **Work in the existing repo** (`phase1/` holds earlier experiments; leave it alone apart from bug fixes).
  Put the product in a `sia/` package.
- **Commit after every step** with a descriptive message. Never commit `.env`, `.venv`, `*.db` or `__pycache__`.
- **Don't stop until it's finished.** Don't wait for user input.

## 3. How the agent improves (the core idea)

The agent learns on three timescales, plus two outside signals:

| Timescale / signal | Mechanism | Where the signal comes from |
|---|---|---|
| **Within a task** | Write a draft, critique it against the agent's rubric (a score from 0 to 10 with specific issues), revise, and repeat up to N times. Keep the best-scoring attempt. | LLM reviewer |
| **Across tasks** | After each task, **reflect**: boil the episode down to ≤2 short, general, imperative **lessons**. Store them, and skip near-duplicates (token Jaccard > 0.6). Before each task, retrieve the relevant lessons plus the most proven ones. | Critique |
| **Lesson utility** | Each lesson tracks `uses` and `total` outcome score. Utility = `(total + 0.7·2) / (uses + 2)`, a Bayesian prior that is mildly optimistic for new lessons. Lessons with ≥5 uses and utility < 0.45 are **retired automatically**. Lessons are ranked by utility. | Episode scores and user ratings |
| **Human feedback** | A rating from 1 to 5 re-credits the lessons used in that episode with `(rating-1)/4`. A correction turns into **new lessons** and also a new **eval example**. A low rating or a comment triggers lesson extraction. | Users |
| **Self-study** | Pick random chunks of the agent's own documents, have the LLM write a realistic question and the ideal answer from the source, answer it with learning on, and grade against the source. Each question is saved as a *synthetic* eval example. | Customer documents |
| **Consolidation** | `improve()` asks the LLM to write a new **playbook** (≤350 words of principles, procedures and checks) from the current playbook, the top lessons and recent failures. Score **both** the current and the candidate playbook on the eval set (same examples, temperature 0.3, graded against the expected answers). **Adopt the candidate only if it beats the current one by more than 0.02.** Keep every version; rollback must be possible. | Eval set |

Eval set priority: examples the user provided, then feedback corrections, then synthetic study questions.

The system prompt is built in this order: domain role → customer instructions → active playbook →
relevant lessons → retrieved knowledge chunks (FTS5 bm25, top 5) → "say so when information is missing,
don't invent it".

Learning is **best-effort**. A failed reflection must never fail the customer's task.

## 4. Components

```
sia/llm.py      LLM client: providers, retries, fallback chain, JSON parsing
sia/store.py    SQLite + FTS5 persistence
sia/agent.py    Agent: run, critique, reflect, feedback, study, evaluate, improve
sia/api.py      FastAPI service
sia/__main__.py CLI: `python -m sia serve [--port N]`, `python -m sia create-tenant <name>`
tests/          offline tests with a scripted fake LLM
examples/demo.py  live demo on the free Gemini tier showing the score rise
README.md, Dockerfile, requirements.txt, pytest.ini (testpaths = tests)
```

### 4.1 `llm.py`
- `LLM(provider, api_key, model=None, base_url=None)` with methods `.text(system, prompt, temperature, as_json)`
  and `.json(system, prompt)`. Also keep a `.calls` counter, which is used for metering.
- `model` may be a **comma-separated fallback chain**. On 429/5xx/timeouts, retry with exponential backoff
  (respect `Retry-After`, cap 60s). Once retries run out, move to the next model. Use more retries with a single model and
  fewer with a chain. Auth/4xx errors fail immediately with no fallback.
- Gemini: `systemInstruction`, `generationConfig.responseMimeType=application/json` for JSON, key in the
  `x-goog-api-key` **header** (never in the URL), and drop `thought` parts.
- `parse_json` must tolerate code fences and chatter around the JSON object.
- **SSRF guard:** a custom `base_url` and Ollama are allowed only when `SIA_ALLOW_CUSTOM_BASE_URL=1`.
- Providers with no safe default model must have the model set explicitly.

### 4.2 `store.py` (SQLite, WAL, one connection per call)
Tables:
- `tenants(id, name, key_hash, created)`: API keys are `sia_<random>` and only the **sha256 is stored**. Show the key once.
- `agents(id, tenant_id, name, domain, instructions, rubric, created)`
- `docs(id, agent_id, title, chunks, created)` + FTS5 `chunks(text, title, agent_id, doc_id)`. Chunk on paragraphs
  into pieces of about 1000 characters, and hard-split oversized paragraphs.
- `lessons(id, agent_id, text, source, uses, total, active, created)` + FTS5 `lessons_fts`
- `playbooks(id, agent_id, version, content, score, active, note, created)`, with exactly one active version per agent
  (version 0 means no playbook)
- `episodes(id, agent_id, task, answer, score, critique JSON, attempts, lesson_ids JSON, rating, correction, created)`
- `examples(id, agent_id, input, expected, source[user|feedback|synthetic], created)`

Build FTS queries from quoted tokens joined with OR, so user text can't inject FTS syntax.
Every query is **scoped by agent_id**, and every API lookup checks tenant ownership.
`stats(agent_id)` returns: the episode count, the average score of the first 20 vs the last 20 episodes (the learning curve), active lessons,
docs, eval examples, playbook version and score, episodes since the last improve, and `improve_recommended` (≥10).

### 4.3 `api.py`
Auth: `Authorization: Bearer <sia key>`. LLM headers: `X-LLM-Provider` (default `gemini`), `X-LLM-Key`,
`X-LLM-Model`, `X-LLM-Base-URL`. An optional `SIA_DEFAULT_LLM_KEY` env var is a server-side default for self-hosters.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness |
| POST | `/v1/tenants` | create a customer (requires `X-Admin-Key` == `SIA_ADMIN_KEY`, constant-time compare) |
| POST/GET | `/v1/agents` | create / list agents |
| GET/PATCH/DELETE | `/v1/agents/{id}` | details + stats + active playbook / update / delete everything |
| POST/GET/DELETE | `/v1/agents/{id}/knowledge[/{doc_id}]` | documents |
| POST/GET/DELETE | `/v1/agents/{id}/examples[/{ex_id}]` | eval set |
| POST | `/v1/agents/{id}/run` | `{task, learn=true, max_revisions=2}` → answer, score, episode_id, attempts, critique, lessons_used, new_lessons, llm_calls |
| POST | `/v1/agents/{id}/feedback` | `{episode_id, rating 1-5, correction?, comment?}` |
| POST | `/v1/agents/{id}/study` | `{rounds 1-20}` |
| POST | `/v1/agents/{id}/improve` | `{max_examples 1-30}` → adopted, old_score, new_score, active_version, playbook |
| GET | `/v1/agents/{id}/lessons` | ranked by utility; `POST …/lessons/{lid}/enable|disable` lets a human veto |
| GET | `/v1/agents/{id}/episodes` | history |
| GET | `/v1/agents/{id}/playbooks` | versions; `POST …/playbooks/{v}/activate` rolls back (0 = none); an unknown version → 404 and the current version stays active |

Error mapping: provider errors → 502, bad input/config → 400, missing → 404, auth → 401/403.
Every response from an LLM-using endpoint includes `llm_calls` (this is the metering hook).
Validate input sizes with pydantic (task ≤20k chars, document ≤2M chars).

## 5. Tests (offline, deterministic)

Write a `FakeLLM` that answers "GOOD" only when coached (it sees reviewer feedback, or a playbook that says
"Always be GOOD"). It grades GOOD as 9/10 and anything else as 4/10, and returns fixed lessons and quiz questions. Cover:
- chunking, FTS sanitising, `parse_json`
- knowledge search is isolated per agent
- lesson dedupe, crediting and automatic retirement
- `run` revises (draft scores 0.4 → revised scores 0.9), saves a lesson, and the next task retrieves and credits that lesson
- `learn=false` makes exactly one LLM call
- a feedback correction becomes an eval example; an unknown episode raises an error
- study creates examples; `improve` adopts a better playbook, **refuses an equal one**, and rollback works
- LLM guards: missing key, the SSRF base URL check, a missing model, an unknown provider
- model fallback on 503, tested with `httpx.MockTransport`
- API end to end with `TestClient`: admin gate, 401, **tenant isolation (404 for another tenant's agent)**,
  run → feedback → improve → stats → delete

## 6. Live verification (required before calling it done)

1. Run `python -m pytest`: everything must pass.
2. Start `python -m sia serve` and test it over real HTTP: health, create a tenant, create an agent, `/docs`, and a 404 for an unknown agent.
3. Run `examples/demo.py` on the free Gemini key from `.env` (`GENAI_API_KEY`). **Pick a task set hard enough that
   the baseline eval score is clearly below 1.0** (for example a refund policy with multi-step arithmetic, exceptions
   and a required escalation email). The demo should show the baseline score → per-task scores and new lessons →
   study → improve (old → new, adopted?) → final lessons and stats. If the free model returns 503, rely on the
   fallback chain instead of failing.
4. Report the real numbers. Do not claim an improvement that the run didn't show.

## 7. Security checklist
- Provider keys are never persisted or logged; tenant keys are hashed; the admin key uses a constant-time compare.
- Every route checks that the agent belongs to the tenant.
- Custom base URLs and Ollama are off by default (SSRF).
- Provider keys go in headers, not URLs.

## 8. Deliverables
- Working code as described above, tests passing, the demo run, a README (concept, quickstart, API, curl examples
  with a `sia()` shell helper, cost per call, deployment, security, known limits) and a Dockerfile
  (`/data` volume, `HOST=0.0.0.0`).
- Known limits to write down rather than build yet: billing and rate limits per tenant, background jobs for
  study/improve (they currently run synchronously), Postgres for running several instances, and self-grading bias
  (which user examples and corrections counteract).
