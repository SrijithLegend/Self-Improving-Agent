# SIA — Self-Improving Agent API

Create an agent for **any domain** (tax advice, legal triage, support, sales, medicine notes, code review…),
give it documents, examples and feedback, and it gets better over time — the way a person does:
practice, get critiqued, write down what it learned, and periodically rethink how it works.

Customers bring their **own LLM key**. Defaults to Google Gemini's free tier (`gemini-flash-latest`);
also works with Groq, OpenRouter free models, Together, Mistral, DeepSeek, OpenAI, Anthropic and local Ollama.

## How it improves

| Timescale | Mechanism | Signal |
|---|---|---|
| Within a task | Draft → self-critique against a rubric → revise (keeps the best attempt) | LLM reviewer |
| Across tasks | Each episode is distilled into short **lessons**. Lessons used in good outcomes gain utility; ones that keep coinciding with bad outcomes are retired automatically. | Critique scores, user ratings |
| From people | Ratings re-credit the lessons used; corrections become new lessons **and** eval examples. | Human feedback |
| Self-study | The agent quizzes itself on its documents, grades against the source, learns from its mistakes. | Your documents |
| Consolidation | `improve` rewrites the agent's **playbook** (strategy prompt) from lessons + failures, and adopts it **only if it scores higher on the eval set**. Every version is kept; roll back any time. | Eval set |

Nothing here fine-tunes model weights — improvement lives in retrieved knowledge, lessons and a gated, versioned
playbook. That makes it work with any provider, any model, and any free tier, and every change is inspectable.

## Quickstart (self-hosted)

```bash
pip install -r requirements.txt
```

```bash
python -m pytest
```

Create a `.env`:

```
SIA_ADMIN_KEY=change-me            # needed to create customers over HTTP
SIA_DEFAULT_LLM_KEY=<gemini key>   # optional: server-side default for gemini requests without X-LLM-Key
```

```bash
python -m sia create-tenant my-company
```

```bash
python -m sia serve
```

Interactive docs: http://127.0.0.1:8000/docs

See it learn on a real free model (uses `GENAI_API_KEY` from `.env`):

```bash
python examples/demo.py
```

### Live demo results (2026-10-20, free `gemini-flash-latest`)

Two real runs of `examples/demo.py`, each starting from an empty database: a deliberately fiddly refund policy
(proration, fee floor/cap, EU override, finance approval, escalation contacts, fixed reply format) and 6
hand-written graded examples. Each run made 67 LLM calls. Both ran **before** the eval changes described below.

| | Run 1 | Run 2 |
|---|---|---|
| Baseline on the 6 examples (no lessons, no playbook) | 0.60 | 0.95 |
| 6 practice tasks | 0.8, 1.0, 1.0, 1.0, 1.0, 0.8 | 0.8, 1.0, 0.9, 1.0, 1.0, 0.8 |
| Self-study, 3 generated questions | 1.0, 0.8, 1.0 | 1.0, 1.0, 0.8 |
| Same 6 examples, with the learned lessons | 0.73 | 0.53 |
| Candidate playbook on the same 6 examples | 0.65, not adopted | 0.67, adopted |

**What this does and does not show.** The loop runs end to end on a free model: it revises weak answers,
writes lessons that match the policy (for example "add three business days when a refund over $1,000 needs
finance approval") and gates playbook changes on an eval. It does **not** show a reliable improvement. The
same 6 examples scored 0.60 in one run and 0.95 in the other with nothing learned, and the learned lessons
helped in run 1 (0.60 → 0.73) but scored below the baseline in run 2 (0.95 → 0.53). Six examples, graded by
the model itself, are too noisy to tell real gains from luck, so run 2 adopted a playbook that still scored
below its own baseline.

Changes made after these runs to make the eval more reliable:
- the grader runs at temperature 0;
- `improve` takes `trials` (1–3): each example is answered that many times per playbook and the scores averaged;
- a candidate is adopted only if its average gain over the same examples beats both +0.02 and one standard
  error of the per-example gains, so a gain that is mostly noise is refused;
- the demo now has 12 hand-checked examples (`DEMO_TRIALS=2` averages two answers per example).

## API

Every call: `Authorization: Bearer <sia key>`. Calls that use a model also take
`X-LLM-Provider` (default `gemini`), `X-LLM-Key`, and optionally `X-LLM-Model` (a comma-separated list is a
fallback chain) and `X-LLM-Base-URL` (self-hosted servers only). Provider keys are used for that request only and
never stored or logged.

Errors: `400` bad input or LLM config, `401` bad SIA key, `403` admin key required, `404` not found (including
another tenant's agent), `502` the LLM provider failed.

```bash
# helper: POSTs when -d is given, GETs otherwise
sia() { curl -s -H "Authorization: Bearer $SIA_KEY" -H "X-LLM-Key: $LLM_KEY" -H "Content-Type: application/json" "localhost:8000$@"; }

# 1. create a domain agent
sia /v1/agents -d '{"name":"tax-bot","domain":"Indian personal income tax",
  "instructions":"Cite the section of the Act. Show calculations.",
  "rubric":"Correct section cited; arithmetic right; flags when a CA is needed."}'

# 2. teach it: documents (chunked + full-text indexed) and graded examples
sia /v1/agents/$ID/knowledge -d '{"title":"Section 80C","content":"..."}'
sia /v1/agents/$ID/examples  -d '{"input":"...","expected":"..."}'

# 3. use it - it critiques, revises and learns as it works
sia /v1/agents/$ID/run -d '{"task":"Can I claim my PPF under 80C?"}'
#   -> {answer, score, episode_id, attempts, critique, lessons_used, new_lessons, llm_calls}
#   "learn": false gives a single fast call with no learning overhead

# 4. correct it
sia /v1/agents/$ID/feedback -d '{"episode_id":"...","rating":2,"correction":"..."}'

# 5. let it study its documents, then consolidate
sia /v1/agents/$ID/study   -d '{"rounds":5}'
sia /v1/agents/$ID/improve -d '{"trials":2}'   # trials: answers per example, averaged (1-3)
#   -> {adopted, old_score, new_score, active_version, playbook}

# 6. inspect / control
GET  /v1/agents/$ID              # stats: learning curve, lessons, playbook version, improve_recommended
GET  /v1/agents/$ID/lessons      # POST /lessons/{id}/disable to veto a bad lesson
GET  /v1/agents/$ID/episodes
GET  /v1/agents/$ID/playbooks    # POST /playbooks/{version}/activate to roll back (0 = none)
```

Create customers (for your signup/billing flow): `POST /v1/tenants` with `X-Admin-Key`; the returned
`api_key` is shown once and stored only as a hash.

## Cost per call

`run` with learning ≈ 3–7 model calls (answer, critique, up to 2 revise+critique, reflection).
`improve` ≈ 1 + 4 × examples × trials. `study` ≈ 5 per round. Every response reports `llm_calls` for metering.
Free tiers are rate-limited; the client backs off and retries on 429.

## Deploy

```bash
docker build -t sia .
```

```bash
docker run -p 8000:8000 -v sia-data:/data -e SIA_ADMIN_KEY=change-me sia
```

Put it behind HTTPS. Security defaults: provider keys never persisted, tenant keys hashed, per-tenant isolation
on every route, custom LLM base URLs and Ollama disabled unless `SIA_ALLOW_CUSTOM_BASE_URL=1` (prevents SSRF).

## Not built yet (add when you have paying users)

- Billing and per-tenant rate limits — `llm_calls` per response is the metering hook.
- Background jobs for `study`/`improve` (they run synchronously; fine until a proxy timeout bites).
- Postgres instead of SQLite for multiple server instances.
- Self-grading is biased toward the model's own taste; user examples and corrections are what anchor it,
  so encourage customers to add a handful.

## Default choices

Choices made where the spec left room, so they are easy to find and change:

| Setting | Value | Where |
|---|---|---|
| Default models | `gemini-flash-latest`, falling back to `gemini-flash-lite-latest` (aliases, so no retired model names) | `sia/llm.py` |
| Retries | 7 tries for a single model, 3 per model in a chain; exponential backoff, honours `Retry-After`, capped at 60s | `sia/llm.py` |
| "Good enough" score | 0.75 (stop revising) | `sia/agent.py` `PASS_SCORE` |
| Revisions per run | 2 by default, 0–5 allowed | API `max_revisions` |
| Lessons per prompt | 6 (relevant ones plus the 3 most proven) | `sia/store.py` |
| Lesson prior / retirement | utility `(total + 0.7·2)/(uses + 2)`; retired at ≥5 uses and utility < 0.45 | `sia/store.py` |
| Lesson dedupe | token Jaccard > 0.6 against the 5 closest lessons | `sia/store.py` |
| Knowledge retrieval | FTS5 bm25, top 5 chunks of ~1000 chars | `sia/store.py` |
| Playbook adoption | mean gain over the same examples must beat +0.02 and one standard error; answers at temperature 0.3, grading at 0, `trials` averaged (default 1) | `sia/agent.py` `significant_gain` |
| Improve recommended | after 10 episodes since the last improve | `stats` |
| Database | `SIA_DB` (default `sia.db`), SQLite in WAL mode | env |

## Repo layout

```
sia/llm.py      provider clients (raw HTTP, retry/backoff)
sia/store.py    SQLite + FTS5: tenants, agents, knowledge, lessons, playbooks, episodes, examples
sia/agent.py    the learning loop: run, reflect, feedback, study, evaluate, improve
sia/api.py      FastAPI service
tests/          offline tests with a scripted fake LLM
examples/demo.py  live demo on the free Gemini tier
phase1/         earlier tool-using coding agent experiments
```
