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

Three real runs of `examples/demo.py`, each starting from an empty database: a deliberately fiddly refund policy
(proration, fee floor/cap, EU override, finance approval, escalation contacts, fixed reply format) and
hand-written graded examples. Runs 1 and 2 used the original eval (6 examples). Run 3 used the more reliable
eval described below (12 examples, grading at temperature 0, adoption only for gains beyond noise, 1 trial).

| | Run 1 | Run 2 | Run 3 (new eval) |
|---|---|---|---|
| Eval examples | 6 | 6 | 12 |
| Baseline (no lessons, no playbook) | 0.60 | 0.95 | 0.62 |
| 6 practice tasks | 0.8, 1.0, 1.0, 1.0, 1.0, 0.8 | 0.8, 1.0, 0.9, 1.0, 1.0, 0.8 | 0.8, 1.0, 0.8, 1.0, 0.9, 1.0 |
| Self-study, 3 generated questions | 1.0, 0.8, 1.0 | 1.0, 1.0, 0.8 | 1.0, 1.0, 0.8 |
| Same examples, with the learned lessons | 0.73 | 0.53 | 0.62 |
| Candidate playbook | 0.65, not adopted | 0.67, adopted | 0.66, not adopted |
| LLM calls | 67 | 67 | 100 |

**What this does and does not show.** The loop runs end to end on a free model: it revises weak answers,
writes lessons that match the policy (for example "add three business days when a refund over $1,000 needs
finance approval") and gates playbook changes on an eval. It does **not** yet show a reliable improvement:

- With the original eval, scores were too noisy to read. The same 6 examples scored 0.60 in one run and 0.95
  in the other with nothing learned, and the lessons helped in run 1 (0.60 → 0.73) but hurt in run 2
  (0.95 → 0.53). Run 2 even adopted a playbook that scored below its own baseline.
- With the new eval (run 3), the baseline came out at 0.62, close to run 1's 0.60. The lessons made no
  difference (0.62 → 0.62). The candidate playbook scored +0.04: that clears the old +0.02 bar, so the old
  rule would have adopted it, but it did not beat the noise across the 12 examples, so it was refused.

So the new eval stops the agent from adopting changes that only look better by luck. Whether learning gives a
repeatable gain on this demo is still open; more runs, or `DEMO_TRIALS=2` to average two answers per example,
would answer it.

Changes made after runs 1 and 2 to make the eval more reliable:
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
#   "learn": false gives a single fast call with no learning overhead; the episode is still recorded,
#   so users can rate or correct fast answers too.
#   If grading or a revision fails, you still get the best answer so far, plus "learning_error".

# 4. correct it
sia /v1/agents/$ID/feedback -d '{"episode_id":"...","rating":2,"correction":"..."}'

# 5. let it study its documents, then consolidate
sia /v1/agents/$ID/study   -d '{"rounds":5}'
sia /v1/agents/$ID/improve -d '{"trials":2}'   # trials: answers per example, averaged (1-3)
#   -> {adopted, old_score, new_score, active_version, playbook}
sia /v1/agents/$ID/evaluate -d '{"trials":2}'   # score the agent as it is now; changes nothing

# 6. inspect / control
GET  /v1/agents/$ID              # stats: learning curve, lessons, playbook version, improve_recommended
GET  /v1/agents/$ID/lessons      # POST /lessons/{id}/disable to veto a bad lesson
GET  /v1/agents/$ID/episodes
GET  /v1/agents/$ID/playbooks    # POST /playbooks/{version}/activate to roll back (0 = none)

# 7. account
GET  /v1/usage?days=30           # requests and LLM calls per action, for billing
POST /v1/keys/rotate             # new API key (shown once); the old one stops working, data is kept
```

Create customers (for your signup/billing flow): `POST /v1/tenants` with `X-Admin-Key`; the returned
`api_key` is shown once and stored only as a hash.

## Cost per call

`run` with learning ≈ 3–7 model calls (answer, critique, up to 2 revise+critique, reflection).
`improve` ≈ 1 + 4 × examples × trials. `evaluate` ≈ 2 × examples × trials. `study` ≈ 5 per round.
Every response reports `llm_calls`, and every LLM-using request is recorded in a usage ledger
(`GET /v1/usage`), including failed ones, since those calls still ran on the customer's key. The ledger is
kept when an agent is deleted, so billing history survives.
Free tiers are rate-limited; the client backs off and retries on 429.

## Deploy

```bash
docker build -t sia .
```

```bash
docker run -p 8000:8000 -v sia-data:/data -e SIA_ADMIN_KEY=change-me sia
```

Put it behind HTTPS. Security defaults: provider keys never persisted, tenant keys hashed, per-tenant isolation
on every route, custom LLM base URLs and Ollama disabled unless `SIA_ALLOW_CUSTOM_BASE_URL=1` (prevents SSRF),
customers can rotate a leaked key without losing data, and each tenant is limited to `SIA_RATE_LIMIT` requests
per minute (default 120, `0` turns it off; over the limit returns `429` with `Retry-After`).

## Not built yet (add when you have paying users)

- Billing itself (invoices, payment provider, plans). The usage ledger is the input for it.
- A shared rate limiter. The current one is in memory, per process, so each instance enforces its own limit.
- Storage quotas per tenant (documents up to 2M characters each, no cap on how many).
- Lesson poisoning: comments and corrections become lessons that go into the prompt. Ratings retire bad ones
  over time and `/lessons/{id}/disable` lets a human veto one, but nothing screens them on the way in.
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
