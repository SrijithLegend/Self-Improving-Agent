# SIA: Self-Improving Agent API

> Agents that learn your domain from every answer, correction and document.

Bring your own model key. Use it through a REST API, or self-host it with one Docker command.

- Docs (interactive OpenAPI): `/docs`
- Source: https://github.com/SrijithLegend/Self-Improving-Agent
- Short machine-readable summary: `/llms.txt`
- New: playbook updates now need a measured win.

Works with: Gemini, Claude, OpenAI, Groq, Ollama (also OpenRouter, Together, Mistral, DeepSeek).

## Mission

Most agents forget everything between calls. Every conversation starts from the same prompt, so the correction a user gave yesterday is lost by tomorrow, and the same mistake comes back.

Fine-tuning can fix that, but it is costly, slow to repeat and ties you to one provider. SIA keeps what an agent learns as readable text instead: short lessons, a playbook and your own documents. You can inspect any of it, veto a lesson you disagree with and roll the playbook back to an earlier version.

## How it learns

1. **Within a task: draft, critique, revise.** SIA has the model answer, critique the answer against your rubric
   and revise it, then keeps the best attempt. In a real run the first draft fell short, so it took 3 attempts to
   reach a full score.
2. **Across tasks: lessons that earn trust.** After a task, SIA writes down what generalizes as short lessons, and
   the next task starts with them. In three real runs in a row, run 1 saved 2 lessons, run 2 used 2, and run 3
   used 4. Lessons it wrote in an earlier run include "prioritise EU withdrawal rights over usage limits".
3. **Consolidation: a new playbook, only if it wins.** SIA folds the lessons into a candidate playbook and scores it
   against the current one on the same eval examples, and switches only if the candidate scores clearly higher.
   In two real runs: 0.73 against 0.65 (kept the current one) and 0.53 against 0.67 (adopted).

Also included: feedback, self-study, rollback, bring your own key.

## API in 60 seconds

Every call takes your SIA key (`Authorization: Bearer $SIA_KEY`). Calls that use a model also take your own model key (`X-LLM-Provider`, default `gemini`, and `X-LLM-Key`), which is used for that request and never stored.

```bash
# helper: POSTs when -d is given, GETs otherwise
sia() { curl -s -H "Authorization: Bearer $SIA_KEY" -H "X-LLM-Key: $LLM_KEY" \
  -H "Content-Type: application/json" "localhost:8000$@"; }

# 1. create an agent: its subject, instructions and what a good answer looks like
sia /v1/agents -d '{"name":"tax-bot","domain":"Indian personal income tax",
  "instructions":"Cite the section of the Act. Show calculations.",
  "rubric":"Correct section cited; arithmetic right; flags when a CA is needed."}'

# 2. teach it with a document
sia /v1/agents/$ID/knowledge -d '{"title":"Section 80C","content":"..."}'

# 3. ask it something: it critiques, revises and learns as it works
sia /v1/agents/$ID/run -d '{"task":"Can I claim my PPF under 80C?"}'
# -> {answer, score, episode_id, attempts, critique, lessons_used, new_lessons, llm_calls}

# 4. correct it when it's wrong (kept as pending until you verify it)
sia /v1/agents/$ID/feedback -d '{"episode_id":"...","rating":2,"correction":"..."}'
```

Errors: `400` bad input or LLM config, `401` bad SIA key, `404` not found, `429` rate limit, `502` the LLM provider failed.

## Setup prompt for a coding agent

```text
Integrate the SIA self-improving agent API into this project.
- Base URL: read SIA_URL from env (default http://localhost:8000). Auth header: Authorization: Bearer $SIA_KEY.
- LLM key: send X-LLM-Provider and X-LLM-Key headers (default provider gemini). Never log or store the LLM key.
- Flow: POST /v1/agents to create one agent; POST /v1/agents/{id}/knowledge for docs;
  POST /v1/agents/{id}/run with {"task": ...}; POST /v1/agents/{id}/feedback with {"episode_id", "rating", "correction"}.
- Show the user the `answer`; keep `episode_id` so the user can rate it.
- Put the client in one module with typed request/response models and handle 400/401/404/502.
```

## Results

Two real runs of `examples/demo.py` on the free `gemini-flash-latest` model, each from an empty database, scored on the same six hand-written examples of a refund policy.

| Score | Run 1 | Run 2 |
|---|---|---|
| Baseline (no lessons, no playbook) | 0.60 | 0.95 |
| With learned lessons | 0.73 | 0.53 |
| Candidate playbook | 0.65, not adopted | 0.67, adopted |

*Six examples graded by the model are too noisy to prove a gain. We're expanding the eval set and will publish the next run here.*

## Pricing

- **Self-host: free.** Run it with Docker on your own machine or server, with your own model keys. Your data stays in your database. The source is under the [Functional Source License](https://github.com/SrijithLegend/Self-Improving-Agent/blob/main/LICENSE.md) (FSL-1.1-ALv2): free to use, modify and self-host, but not to resell as a competing service. Each release becomes Apache 2.0 two years after it ships. `docker build -t sia . && docker run -p 8000:8000 -v sia-data:/data --env-file .env sia`
- **Hosted: coming soon.** No prices yet.

Privacy policy: `/privacy` · Terms of service: `/terms`

© 2026 Srijith · FSL-1.1-ALv2
