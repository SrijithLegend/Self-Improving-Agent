# SIA DevTools — Practical Change Guide

This guide is for making focused changes to the current code without asking an AI coding tool to rewrite the repository.

## 1. Understand the two paths first

- Managed-agent behavior: sia/agent.py, sia/store.py, sia/llm.py and the /v1/agents routes in sia/api.py.
- Developer-tools SDK: sia_devtools/client.py, sia_devtools/store.py, sia_devtools/scoring.py, sia_devtools/errors.py and the /v1/projects routes in sia/api.py.
- CLI: sia_devtools/cli.py; python -m sia delegates through sia/__main__.py.
- Security and operations: sia/security.py, sia/billing.py, sia/ops.py.
- Tests: tests/test_sia.py, test_devtools.py, test_improvements.py, test_security.py, test_billing.py, test_launch.py.

Do not assume the managed-agent “learning” loop is the same thing as DevTools prompt-version release control.

## 2. Set up a development environment

The project declares Python >=3.11 in pyproject.toml and CI tests Python 3.11 and 3.12.

From a fresh clone, a typical setup is:

~~~bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
~~~

The dev extra includes pytest and server dependencies. The SDK's core dependency is httpx. Do not copy production secrets into a local .env. Use .env.example as the safe reference and set only the variables needed for the task.

## 3. Run the app and tests

~~~bash
python -m pytest -q
sia --help
sia eval --help
sia dashboard
~~~

The exact subcommand structure should be checked with the installed CLI's --help output. The dashboard command is intended for local use and reads the local SQLite store. A live LLM key is not needed for the fake-LLM unit tests; a real provider key is needed for live-model demos or operations that actually call a provider.

This guide does not claim these commands were run during report preparation.

## 4. Trace a request through the code

### Managed-agent request

Start at the route in sia/api.py: POST /v1/agents/{agent_id}/run. Follow the request model RunIn, authentication dependency, owned_agent, llm(), metered(), call(), then Agent.run() in sia/agent.py. Inside run, inspect _context(), critique(), reflect(), Store methods and LLM.text/json.

### DevTools trace

Start at SIAClient.trace or SIAClient.start_run in sia_devtools/client.py. Follow Run.end(), record serialization/redaction, _enqueue, and the local/hosted sender path. On the server, follow POST /v1/projects/{project_id}/runs in sia/api.py into DevStore.

### Evaluation/release

Start in sia_devtools/scoring.py for scorer behavior and compare_scores(). Follow the SDK or CLI command in sia_devtools/client.py / sia_devtools/cli.py, then the API persistence route if hosted. Do not change a verdict rule without tests at threshold boundaries.

## 5. Find the right file for a bug

| Symptom | Start here | Then inspect |
|---|---|---|
| Bad managed-agent answer | sia/agent.py | build_system, retrieval, rubric, LLM response |
| Lesson not reused | sia/store.py | relevant_lessons, FTS query, utility and active flag |
| Candidate accepted/rejected unexpectedly | sia_devtools/scoring.py | compare_scores, policy, critical cases |
| Trace missing in hosted mode | sia_devtools/client.py | queue, sender, retries, last_error; then API logs |
| Cross-project access concern | sia/api.py | auth dependency and project ownership lookup; tests/test_security.py |
| Database field missing | correct store module | migration list, row mapping and tests |
| Model provider error | sia/llm.py | provider config, _post, response parsing and retry |
| Backup/restore problem | sia/ops.py | docs/RUNBOOK.md and tests/test_launch.py |
| Payment verification | sia/billing.py | signature functions and tests/test_billing.py |
| Dashboard rendering | sia/dashboard.html or web/ | escaping, CSP and route |

## 6. Make a small change safely

1. State the behavior you want in one sentence.
2. Find the public entry point and implementation.
3. Search for callers of the function before changing its signature.
4. Add a failing test that captures the current bug.
5. Implement the smallest change that makes the test pass.
6. Run the targeted test file.
7. Run the full test suite.
8. Inspect the diff and check that unrelated files did not change.
9. Update docs, .env.example and compatibility notes where necessary.
10. Commit the change.

Avoid broad refactors while fixing a narrow bug. If a function is public in sia_devtools/__init__.py or the SDK, preserve compatibility or document a breaking change.

## 7. Change the database schema

There are two stores: sia/store.py and sia_devtools/store.py. Use the one that owns the data.

- Add a new numbered migration to the store's migration list.
- Do not modify only the live database or rely on CREATE TABLE IF NOT EXISTS to add columns.
- Test an empty database and a database created from the previous schema.
- Ensure the migration is recorded and can safely run once.
- Update insert/update/read mappings and API schemas if the field is exposed.
- Add a backup/restore consideration for production data.

Do not assume a column is a real foreign key unless the SQL explicitly declares it and SQLite enforcement is configured.

## 8. Add a new API endpoint

1. Define a Pydantic request model with explicit field bounds. Use Strict for DevTools models where unknown fields should be rejected.
2. Add the route in sia/api.py.
3. Choose the right auth dependency: key_auth, tenant, owned_agent or project ownership check.
4. Scope every database lookup by the authenticated organization/project/agent.
5. Return typed errors or predictable HTTP statuses.
6. Add tests for valid input, invalid input, missing resource, wrong scope, and wrong tenant.
7. Update README/API docs if the endpoint is public.

An unguessable ID is not authorization. Always check ownership.

## 9. Add a deterministic scorer

Implement the existing scorer contract in sia_devtools/scoring.py: output, expected, example. Keep the function deterministic and side-effect free when possible. Add it to the built-in registry/resolver as appropriate and export it through sia_devtools/__init__.py if it is public. Test correct, incorrect, empty and malformed inputs.

## 10. Investigate a failed model call

1. Record provider, model and HTTP status without exposing keys.
2. Inspect LLM.__init__, text(), _text() and _post() in sia/llm.py.
3. Check whether the error is transient (429/5xx/network) or permanent (authentication/invalid request).
4. Confirm whether the configured fallback chain is exhausted.
5. Test retry logic using mocked HTTP responses.
6. Verify error sanitization still removes the API key.
7. Avoid logging full prompts or customer documents unless the data-handling policy explicitly permits it.

## 11. Read a stack trace

- Start at the last line: exception type and message.
- Find the first frame that belongs to the SIA repository.
- Identify the input and state at that call.
- Follow one caller upward at a time.
- Do not “fix” the topmost framework frame unless the trace points to a framework defect.
- Add a test for the smallest reproducible input.

For hosted requests, use X-Request-ID to find the corresponding server log line. Logs intentionally omit request bodies and headers.

## 12. Use breakpoints or structured logging

Set a breakpoint before the suspected call and inspect safe values: IDs, counts, status, score, elapsed time and error class. Do not inspect or print API keys or sensitive user payloads into logs.

When adding logs, prefer structured fields such as request_id, project_id, status, duration_ms, model name and retry count. Avoid raw prompts, outputs, Authorization headers and query strings.

## 13. Review a Git diff

~~~bash
git status --short
git diff --stat
git diff --check
git diff
~~~

Review for:
- Accidental application changes in a documentation-only task.
- Secrets, local database files and generated artifacts.
- Unrelated formatting or renames.
- Missing migration or tests.
- Changes to public API behavior without documentation.
- A test that passes for the wrong reason.

## 14. Revert a bad change

If uncommitted, restore only the files you intentionally changed; do not use destructive reset commands when other work may exist. If committed and not shared, create a corrective commit or use a carefully reviewed revert. If already shared, prefer git revert so history remains auditable. Back up databases before testing migration rollback or restore procedures.

## 15. How to work independently from Claude Code

1. **Inspect with IDE navigation/search.** Use “Go to Definition,” “Find All References,” and repository-wide search for a function name.
2. **Trace function calls.** Start at the API route or public SDK method and follow each call one level deeper.
3. **Read stack traces.** Identify exception type, failing line, and the first SIA-owned frame.
4. **Use breakpoints or logging.** Inspect IDs, counts and states; redact secrets.
5. **Write a failing test.** The test should demonstrate the bug before the fix.
6. **Implement the smallest fix.** Avoid rewriting adjacent modules.
7. **Run targeted tests.** Example: python -m pytest tests/test_sia.py -q.
8. **Run the full suite.** python -m pytest -q.
9. **Review the diff.** Check scope, secrets, migration and compatibility.
10. **Commit with a clear message.** Make one coherent change per commit.

Use Claude Code as a pair programmer, not as the only person who understands the project. Ask it to explain a proposed diff, name the tests, and identify risks. Do not accept claims that tests passed unless the tool output shows the command and result.

## 16. Feature-to-file map

| Feature | Primary files | Tests/docs to update |
|---|---|---|
| Managed-agent draft/critique | sia/agent.py, sia/llm.py | tests/test_sia.py |
| Lesson retrieval | sia/store.py | tests/test_sia.py |
| Feedback verification | sia/agent.py, sia/store.py, sia/api.py | tests/test_sia.py, tests/test_security.py |
| SDK traces | sia_devtools/client.py, sia_devtools/store.py | tests/test_devtools.py |
| Datasets/version history | sia_devtools/client.py, sia_devtools/store.py, sia/api.py | tests/test_devtools.py |
| Scoring and gate | sia_devtools/scoring.py | tests/test_improvements.py |
| API auth | sia/api.py, sia/security.py, stores | tests/test_security.py |
| Billing | sia/billing.py, sia/api.py | tests/test_billing.py |
| Backup/restore | sia/ops.py | tests/test_launch.py, docs/RUNBOOK.md |
| CLI | sia_devtools/cli.py | CLI tests and README |
| Dashboard | sia/dashboard.html, web/ | security/launch tests |
