> **Historical.** This is the original brief that turned the managed-agent prototype into SIA DevTools. It
> describes plans and file layouts from before launch (for example `phase1/`, since removed). For what the
> product is now, read the [README](../README.md); for launch decisions, [LAUNCH_NOTES.md](LAUNCH_NOTES.md).

# PROJECT: Turn Self-Improving Agent into a Commercial AI Developer Platform

Repository: https://github.com/SrijithLegend/Self-Improving-Agent

Working product name: **SIA DevTools**

Product category: AI agent evaluation, feedback-driven improvement, and regression prevention infrastructure.

## YOUR ROLE

Act as a senior AI infrastructure engineer, Python SDK architect, full-stack SaaS engineer, application security engineer, and startup CTO.

Your task is to transform my existing Self-Improving Agent repository into a functional, developer-friendly platform that AI engineers can integrate into their applications.

Do not merely create a landing page, a mock dashboard, or a new project that ignores my existing implementation.

Inspect the actual repository first. Understand the current implementation, preserve useful code, identify technical gaps, and build the smallest commercially useful product before adding unnecessary features.

Make reasonable engineering decisions independently. Do not stop after the audit; proceed through the implementation phases and run tests after each significant change.

# 1. PRODUCT VISION

SIA DevTools helps developers improve the reliability of their AI applications through a repeatable engineering workflow:

1. Capture AI-agent executions.
2. Inspect failures and collect human feedback.
3. Build reusable evaluation datasets from verified examples.
4. Identify recurring failure patterns.
5. Generate candidate prompt, instruction, or playbook improvements.
6. Compare the current and candidate versions on the same evaluation dataset.
7. Detect regressions and uncertain improvements.
8. Promote improvements only through an explicit, auditable release process.
9. Track whether the changes improve real application quality over time.

The core promise is:

**Turn AI-agent failures into tested improvements, with evidence that changes help before they reach production.**

Do not claim that the underlying language model retrains itself. The product improves application behavior through evaluation, feedback, lessons, retrieval, and controlled instruction or prompt versioning.

# 2. FIRST: AUDIT THE EXISTING REPOSITORY

Inspect the complete project before changing it.

Pay particular attention to:

- `sia/agent.py`
- `sia/store.py`
- `sia/api.py`
- `sia/llm.py`
- `sia/__main__.py`
- `examples/`
- `tests/`
- `phase1/`
- `README.md`
- `SPEC.md`
- Dependency and configuration files

Confirm which files actually exist before relying on these paths.

Understand and test the current implementations of:

- `run()`
- `critique()`
- `_save_lessons()`
- `feedback()`
- `study()`
- `scores()`
- `evaluate()`
- `improve()`
- `significant_gain()`

Inspect how the current SQLite schema stores episodes, lessons, evaluation examples, document chunks, feedback, and playbook versions.

Run the existing test suite and record the baseline.

Identify:
- Broken or incomplete functionality.
- Missing API capabilities.
- Persistence limitations.
- Weak evaluation methodology.
- Security risks.
- Missing SDK functionality.
- Missing developer documentation.
- Potential compatibility issues.

Produce a concise audit, then implement the product in phases.

Do not rewrite functioning modules just to make the architecture look cleaner. Refactor only when there is a concrete technical benefit.

# 3. DEFINE THE FIRST PRODUCT WEDGE

The first release must not attempt to compete with every AI observability platform on every feature.

Focus on this workflow:

**Capture a failure → create a verified test case → propose an improvement → evaluate it → prevent regressions.**

The initial target customer is a small AI startup or independent developer using Python to build an LLM-powered application or agent.

The developer should be able to integrate SIA into an existing application without rewriting the agent or adopting a specific agent framework.

Prioritize framework-agnostic instrumentation and evaluation. Add framework-specific adapters only where justified.

# 4. BUILD A PYTHON SDK

Create a distributable Python package with a clear public API.

Use a suitable package name such as `sia-devtools` if available and appropriate. Verify naming and packaging constraints before publishing.

Provide an intuitive interface along these lines:

```python
from sia_devtools import SIAClient

sia = SIAClient(
    api_key="YOUR_API_KEY",
    project="my-ai-agent",
)

run = sia.start_run(
    name="answer_customer_question",
    input={"question": "What is the refund policy?"},
)

try:
    answer = my_existing_agent("What is the refund policy?")

    run.log_output({
        "answer": answer,
    })

    run.end(status="success")

except Exception as exc:
    run.log_error(str(exc))
    run.end(status="error")
    raise
```

This is an illustrative API design. Implement a consistent, working API rather than copying the example blindly.

The SDK must support:

- Run creation and completion.
- Inputs and outputs.
- Error capture.
- Latency measurement.
- Token and cost metadata when available.
- Tags and metadata.
- User feedback.
- Custom scores.
- Dataset evaluation.
- Explicit flush or synchronization where required.
- Timeouts and bounded retries.
- Offline or local mode if practical.
- Clear exceptions and useful type hints.

Requirements:

- Support Python 3.11 or a clearly documented compatible range.
- Use typed public interfaces.
- Provide predictable error handling.
- Avoid blocking the user's agent unnecessarily.
- Do not swallow application exceptions silently.
- Never expose API keys in logs or error messages.
- Avoid making network requests automatically during module import.
- Keep optional integrations optional.

Include a decorator or context-manager interface if it makes instrumentation easier.

Provide a synchronous SDK first. Add asynchronous support only if it can be implemented and tested correctly.

# 5. IMPLEMENT TRACE AND RUN STORAGE

Each run should capture useful execution metadata.

Use a structured schema with fields such as:

- Project ID.
- Run ID.
- Parent run ID where applicable.
- Run name.
- Input and output.
- Start and end timestamps.
- Duration.
- Status.
- Error details.
- Model and provider metadata.
- Token usage and cost when supplied.
- Tags.
- Custom metadata.
- Feedback and evaluation references.

Design the schema so that future agent frameworks can represent nested runs and tool calls.

Do not claim to capture every internal model or tool call automatically unless an integration actually instruments those calls.

Support:
- Single-run inspection.
- Run history.
- Filtering by status, project, tag, and time.
- Search where practical.
- Failure inspection.
- Trace export.

Use bounded payload sizes and configurable data-redaction rules.

Provide a local development mode backed by SQLite if that simplifies the first release. Use a managed database only when needed for a multi-user hosted deployment.

# 6. BUILD AN EVALUATION ENGINE

This is one of the product's core components.

Developers must be able to define datasets containing inputs, expected outputs, optional context, and custom metadata.

Provide a dataset API along these lines:

```python
dataset = sia.create_dataset("refund-policy-tests")

dataset.add_example(
    input={"question": "Can I get a refund?"},
    expected_output={
        "answer": "Refunds are available under the stated policy."
    },
)
```

Support:
- Versioned datasets or immutable evaluation snapshots.
- Adding, editing, and deleting examples with clear history.
- Replaying an application or supplied evaluator against a dataset.
- Custom scoring functions.
- Exact-match and structured-output checks where appropriate.
- Rule-based assertions.
- Optional model-based grading.
- Per-example scores and aggregate metrics.
- Exportable evaluation results.
- Reproducible comparisons between runs.

Do not use an LLM judge as the sole source of truth when deterministic assertions or human-reviewed answers are available.

Store the model, prompt/playbook version, scorer version, dataset version, and evaluation configuration used for each result.

Separate deterministic tests from model-based evaluations.

# 7. TURN HUMAN CORRECTIONS INTO VERIFIED EVALUATION EXAMPLES

Implement a workflow that lets developers annotate runs.

Support:
- Positive and negative feedback.
- A corrected answer.
- A failure category.
- An optional explanation.
- An optional reviewer identifier.
- Timestamp and source run.
- A review or verification status.

Allow an authorized developer to turn a corrected run into a dataset example.

Do not automatically treat every user correction as correct. Corrections can be inaccurate, malicious, ambiguous, or contradictory.

Implement review states such as:
- Pending.
- Verified.
- Rejected.

Only verified examples should enter the trusted evaluation dataset by default.

Preserve provenance so developers can trace each example back to its original execution and correction.

# 8. IMPLEMENT THE IMPROVEMENT ENGINE

Reuse the existing agent improvement logic where appropriate.

The engine should:

1. Inspect evaluation failures and verified feedback.
2. Identify recurring patterns.
3. Extract candidate reusable lessons.
4. Generate a candidate prompt or instruction playbook.
5. Evaluate the candidate and current version on the same frozen evaluation set.
6. Compare per-example and aggregate results.
7. Check for regressions in important cases.
8. Record the decision and supporting evidence.
9. Retain the current active version if improvement is insufficient.
10. Allow explicit approval before production promotion.

Support improvement targets such as:
- A system prompt.
- An instruction playbook.
- A retrieval or answer policy.
- An output-format instruction.

Keep arbitrary application code modification out of the initial release.

Do not automatically rewrite a developer's source code or deploy a new agent implementation.

The improvement engine must not be allowed to alter its own safety rules or evaluation policy simply to obtain a better score.

# 9. MAKE EVALUATION DECISIONS RELIABLE

Preserve and improve the existing `significant_gain()` logic if it is valid after inspection.

Use the same examples and comparable evaluation settings for current and candidate versions.

Implement:
- Per-example score differences.
- Mean improvement.
- Variance or uncertainty estimates where appropriate.
- Configurable adoption thresholds.
- Minimum sample requirements.
- Regression detection.
- Evaluation history.
- A clear reason for every adoption or rejection decision.

Where scores come from nondeterministic model-based graders, account for evaluation noise.

If there are too few examples to establish a meaningful improvement, return an inconclusive result rather than declaring success.

For critical evaluation sets, allow regression rules that must pass regardless of the aggregate score.

Keep separate:
- Candidate generation.
- Candidate evaluation.
- Candidate approval.
- Production activation.

No candidate should silently replace a production version.

# 10. ADD A CI REGRESSION GATE

Provide a CLI that developers can use locally and in CI.

Example target interface:

```bash
sia eval run --dataset refund-policy-tests
sia eval compare --baseline current --candidate candidate
sia eval gate --min-score 0.85 --max-regressions 0
```

These are example commands; implement and document a consistent CLI.

The gate should:
- Exit successfully when configured criteria pass.
- Exit with a nonzero code when mandatory checks fail.
- Report inconclusive results separately from passing results.
- Produce readable terminal output.
- Optionally write JSON results for CI systems.
- Avoid requiring a hosted account for local evaluation if local mode is supported.

Add a documented GitHub Actions example that evaluates a candidate prompt or playbook before a release.

Do not claim the gate protects arbitrary application behavior unless the developer has supplied relevant evaluation cases and integrated the appropriate application execution.

# 11. PROVIDE A FRAMEWORK-AGNOSTIC API

Developers should be able to instrument their own Python function without adopting a specific orchestration framework.

Start with:
- Direct function instrumentation.
- Manual run logging.
- Dataset-based evaluation.
- Custom scorer registration.

Keep integration points open for future adapters to:
- LangChain and LangGraph.
- LlamaIndex.
- Plain Google GenAI applications.
- Plain OpenAI-compatible applications.
- Other agent frameworks.

Do not implement every integration in the first release.

Build one complete example using a simple Python agent and one example using a supported LLM provider.

Use mock providers for automated tests so the standard test suite does not require API keys.

# 12. BUILD A DEVELOPER DASHBOARD

Create a clean, responsive web interface that uses real backend data.

Reuse an existing frontend if one exists. Otherwise choose a suitable React and TypeScript stack.

Required screens:

## Overview
- Total runs.
- Error rate.
- Evaluation scores.
- Latency.
- Token usage and estimated cost where available.
- Recent failures.
- Latest improvement decision.

## Projects
- Create and inspect projects.
- Manage project settings.
- Display API key metadata.
- Revoke or rotate keys.
- Show SDK installation instructions.

## Runs and traces
- Browse runs.
- Filter and search.
- Inspect inputs, outputs, errors, and metadata.
- View nested execution steps where available.
- Display redacted payloads safely.

## Datasets
- Create datasets.
- Add and edit examples.
- Review corrected examples.
- Display dataset versions and evaluation history.

## Evaluations
- Start an evaluation.
- Inspect per-example results.
- Compare baseline and candidate versions.
- Display score changes and regression warnings.
- Export results.

## Improvement laboratory
- View extracted lessons.
- Inspect candidate playbooks.
- Compare versions.
- Review acceptance criteria.
- Approve or reject a candidate.
- Roll back to a previous approved version.

## Settings and usage
- Project and organization settings.
- Usage accounting.
- Data-retention settings.
- API key management.
- Rate limits and plan entitlements.

All statistics must come from actual data. Do not populate the dashboard with fake results or fabricated performance improvements.

# 13. HOSTED API AND MULTI-TENANT ARCHITECTURE

Build a hosted API that can support multiple independent developers and organizations.

Use FastAPI if it fits the existing repository.

Implement:
- Authentication.
- Organization and project ownership.
- API key authentication for SDK requests.
- Secure key storage, using hashes for keys that only need verification.
- Key rotation and revocation.
- Authorization on every protected operation.
- Database migrations.
- Request validation.
- Pagination.
- Rate limits.
- Upload and payload limits where relevant.
- Health checks.
- Structured logs.
- Timeouts and bounded retries.

Every run, dataset, feedback item, lesson, evaluation, and playbook version must be scoped to the authorized organization and project.

Never trust a project ID supplied by the client without checking authorization.

Write explicit tests proving that one organization cannot access another organization's records.

Keep secrets on the server. Never embed private API keys in frontend code.

Do not send customer payloads to external LLM providers unless the relevant feature requires it and the data flow is disclosed.

Add configurable redaction for sensitive data before storing or transmitting traces.

Do not store secrets, passwords, or sensitive personal information in evaluation datasets by default.

# 14. DESIGN THE SYSTEM FOR FAILURE

Model calls, network connections, and background jobs will fail.

Handle:
- Provider timeouts.
- Invalid model responses.
- Rate-limit responses.
- Partial trace submission.
- Duplicate requests.
- Interrupted evaluation jobs.
- Failed candidate generation.
- Concurrent improvement attempts.
- Database errors.

Use idempotency where appropriate.

Keep long-running evaluations outside request transactions. Add job status tracking if evaluation duration warrants it.

Never mark a run, evaluation, or improvement job successful before its actual completion.

Do not silently discard failed trace submissions. Expose useful diagnostics without breaking the developer's application.

# 15. TESTING REQUIREMENTS

Expand the existing tests to cover the following.

SDK:
- Run lifecycle.
- Error capture.
- Metadata serialization.
- Timeout behavior.
- API authentication failures.
- Local mode.
- Custom scoring.

Datasets and evaluations:
- Dataset persistence.
- Version consistency.
- Per-example results.
- Aggregate metrics.
- Deterministic scoring.
- Malformed examples.
- Reproducible comparisons.

Improvement engine:
- A genuinely better candidate.
- A worse candidate.
- An inconclusive candidate.
- A candidate with a critical regression.
- Insufficient evaluation examples.
- Malicious feedback.
- Duplicate feedback.
- Failed model calls.
- Approval and rollback.

Security:
- Cross-tenant access attempts.
- Revoked API keys.
- Invalid project identifiers.
- Oversized inputs.
- Sensitive-data redaction.
- Unauthorized playbook activation.

CI:
- Passing gate.
- Failing gate.
- Inconclusive gate.
- Machine-readable result output.

Use deterministic mocks for normal automated tests. Keep live LLM tests optional.

Run the full suite after implementation. Report actual test results and do not fabricate passing tests.

# 16. PRICING AND MONETIZATION READINESS

Prepare the product for a freemium developer business.

Treat the following as initial hypotheses, not validated prices.

Free:
- One project.
- Local evaluation support.
- Limited hosted runs.
- Small evaluation datasets.
- Basic score comparison.

Pro:
- Multiple projects.
- Higher hosted usage.
- Team collaboration.
- Extended history.
- Advanced evaluation comparisons.
- CI integrations and improvement workflows.

Team:
- Multiple developers.
- Organization-level access control.
- Shared datasets.
- Audit history.
- Configurable retention.
- Higher usage limits.

Do not build complex enterprise billing before validating demand.

Implement a simple plan and entitlement system with server-side limits.

Track usage accurately and distinguish included usage from overages.

Prepare a clean payment integration boundary for a supported provider. Do not pretend payments have succeeded or store payment card information.

# 17. COMPETITIVE DIFFERENTIATION

Do not attempt to replicate every feature of existing AI observability platforms.

Position the product around:
- Feedback that becomes verified regression tests.
- Reusable lessons extracted from failures.
- Candidate prompt or playbook improvement.
- Paired evaluation of current versus candidate versions.
- Regression prevention in CI.
- Transparent adoption decisions and rollback.

The product must prove this workflow works end to end.

Avoid marketing claims such as:
- “Perfect AI.”
- “Zero hallucinations.”
- “Fully autonomous self-improvement.”
- “Guaranteed accuracy gains.”

Use measured claims backed by actual evaluations.

# 18. DOCUMENTATION AND DEMO

Provide:
- A complete README.
- Installation instructions.
- SDK quickstart.
- Local development instructions.
- API documentation.
- Dataset and evaluator examples.
- Improvement workflow example.
- CI integration example.
- Configuration reference.
- `.env.example` containing placeholders only.
- Database setup and migration instructions.
- Deployment instructions.
- Security and data-handling documentation.
- Known limitations.

Create a realistic demonstration project with fictional data.

The demo must show:
1. A baseline agent makes a predictable mistake.
2. The mistake is captured.
3. A verified correction becomes an evaluation example.
4. A candidate instruction change is proposed.
5. Baseline and candidate versions are evaluated on a fixed dataset.
6. The outcome is accepted, rejected, or marked inconclusive according to actual measured results.
7. The decision and scores are displayed.

If the candidate does not improve the score, show the rejection honestly. Do not force the demonstration to produce a successful improvement.

# 19. IMPLEMENTATION PHASES

Phase 1: Audit the repository and run the baseline tests.

Phase 2: Stabilize the existing agent engine, data model, and evaluation logic.

Phase 3: Build the SDK and local run-recording interface.

Phase 4: Implement datasets, scoring, evaluation comparisons, and feedback verification.

Phase 5: Implement the improvement laboratory and CI regression gate.

Phase 6: Build the hosted API, authentication, organizations, and project isolation.

Phase 7: Build the dashboard using real backend data.

Phase 8: Add usage limits, deployment support, documentation, and a complete demo.

Phase 9: Run integration, security, and regression tests. Fix failures and document remaining limitations.

After each phase, summarize:
- Files created or modified.
- Features completed.
- Commands actually executed.
- Test results.
- Known limitations.
- Next implementation step.

Do not stop after writing a plan. Continue through the phases unless a genuine blocker requires my input.

# 20. DEFINITION OF DONE

The initial product is complete when:

1. A developer can install the SDK.
2. The developer can instrument an existing Python function.
3. Runs and errors can be recorded and inspected.
4. Developers can create evaluation datasets.
5. Evaluation results are reproducible and traceable to their configuration.
6. Verified feedback can become evaluation examples.
7. The existing improvement engine can generate a candidate instruction or playbook.
8. The system compares baseline and candidate versions on the same evaluation set.
9. A candidate with inadequate evidence or critical regressions is not automatically promoted.
10. An approved change can be activated and rolled back.
11. The CLI can enforce an evaluation gate in CI.
12. The hosted application enforces project and organization isolation.
13. The dashboard displays real stored data.
14. Automated tests cover the critical workflows.
15. A new developer can follow the README to run the example locally.

Do not claim production readiness until security, deployment, reliability, and operational limitations have been explicitly assessed.

## FINAL INSTRUCTION

Begin by auditing the existing repository and running the current tests.

Then build the smallest complete developer workflow that demonstrates the product's central value: capturing a real failure, converting a verified correction into an evaluation case, testing a candidate improvement, and preventing regressions.

Prioritize a reliable SDK, a useful evaluation engine, and a working CI gate before building a large dashboard.

Make every important improvement measurable, explainable, reversible, and backed by test results.