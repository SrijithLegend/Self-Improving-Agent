"""Hosted agents (kind="agent" projects) through the API, the model client, and the server's shell.

A scripted fake LLM drives the agent deterministically. The old managed-agents API (/v1/agents) is gone: its data is
migrated into projects (tests/test_migration_agents.py) and its tables are dropped once that has run.
"""
import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sia.llm import LLM, LLMError, parse_json  # noqa: E402
from sia_devtools.store import LOCAL_ORG, DevStore, chunk_text, fts_query  # noqa: E402
from sia_loop import actor, config, jobs, models  # noqa: E402
from sia_loop.tools.registry import Registry  # noqa: E402

LESSON = "Always state the governing rule before giving the conclusion."
ADMIN = "admin-key-for-tests-0123456789abcdef"


class FakeLLM:
    """Answers 'GOOD' only when coached (by reviewer feedback or a playbook saying so); grades GOOD 9/10, else 4/10."""

    def __init__(self, *a, **k):
        self.calls = 0

    def text(self, system, prompt, temperature=0.7, as_json=False):
        self.calls += 1
        if "improving the operating playbook" in system:
            return "- Always be GOOD"
        if "Reviewer feedback" in prompt or "Always be GOOD" in system:
            return "GOOD answer"
        return "draft answer"

    def json(self, system, prompt, temperature=0.2):
        self.calls += 1
        if "ANSWER TO REVIEW" in prompt:
            answer = prompt.split("ANSWER TO REVIEW:")[1]
            good = "GOOD" in answer.split("RUBRIC:")[0].split("REFERENCE")[0]
            return {"score": 9 if good else 4, "issues": [] if good else ["too vague"], "suggestion": "be specific"}
        if "exam questions" in system:
            return {"question": "What is the filing deadline?", "answer": "July 31"}
        if "minimal, targeted edits" in system:
            return {}
        if "coach" in system:
            return {"lessons": [LESSON]}
        if "TASK:\n" in prompt:  # the actor's turn
            return {"final": "GOOD answer" if "Always be GOOD" in system else "draft answer"}
        return {}


@pytest.fixture(autouse=True)
def fake_models(monkeypatch):
    monkeypatch.setattr(models, "factory", FakeLLM)
    monkeypatch.setenv("SIA_LLM_KEY_GEMINI", "server-key")


@pytest.fixture
def dev(tmp_path):
    return DevStore(str(tmp_path / "t.db"))


@pytest.fixture
def agent(dev):
    pid = dev.create_project(LOCAL_ORG, "tax", {"kind": "agent", "agent": {"domain": "personal income tax"}})["id"]
    config.bootstrap(dev, LOCAL_ORG, pid)
    return pid


def no_revise(dev, org, pid):
    """A person switches the agent to a config without the self-critique loop, so only its playbook decides."""
    v = dev.create_version(org, pid, config.dumps(config.default_config(revise={
        "max_revisions": 0, "pass_score": 0.75, "critic": "self"})), name="agent_config")["version"]
    dev.approve(org, pid, "agent_config", v, override_reason="test setup")
    dev.activate(org, pid, "agent_config", v)
    # a deterministic agent: one trial is enough, and every applicable operator is tried, so the outcome doesn't
    # depend on which ones are sampled (150 examples need a bigger per-cycle cap)
    dev.update_project(org, pid, {"loop": {"trials": 1, "candidates_per_cycle": 9, "max_calls_per_cycle": 1000}})
    return v


def test_chunking_and_fts_sanitizing():
    chunks = chunk_text("a" * 2500 + "\n\nshort para")
    assert [len(c) for c in chunks] == [1000, 1000, 500 + 2 + 10]
    assert fts_query('NEAR(x) "drop" OR * tax-deadline') == '"near" OR "drop" OR "tax" OR "deadline"'
    assert parse_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json("no json here")


def test_a_task_revises_records_and_learns_into_probation(dev, agent):
    out = actor.perform(dev, LOCAL_ORG, agent, "When must I file?")
    assert out["answer"] == "GOOD answer" and out["self_critique"] == 0.9 and out["attempts"] == 2
    assert out["new_lessons"] == [LESSON]
    run = dev.run(LOCAL_ORG, agent, out["run_id"])
    assert run["scores"]["self_critique"] == 0.9 and run["version_name"] == "agent_config"
    # the lesson waits in probation: it is not in the next prompt until a promoted config pins it (spec P1)
    out2 = actor.perform(dev, LOCAL_ORG, agent, "Must I state the rule?")
    assert out2["lessons_used"] == [] and dev.lessons(LOCAL_ORG, agent)[0]["state"] == "probation"


def test_no_learning_mode_is_one_call(dev, agent):
    out = actor.perform(dev, LOCAL_ORG, agent, "hi", revise=False, learn=False)
    assert out["llm_calls"] == 1 and out["critique"] is None and out["new_lessons"] == []
    assert dev.run(LOCAL_ORG, agent, out["run_id"])["scores"] == {}  # recorded, unscored


def test_failed_learning_keeps_the_answer(dev, agent, monkeypatch):
    class FlakyGrader(FakeLLM):
        def json(self, system, prompt, temperature=0.2):
            if "ANSWER TO REVIEW" in prompt:
                raise LLMError("grader overloaded")
            return super().json(system, prompt, temperature)

    monkeypatch.setattr(models, "factory", FlakyGrader)
    out = actor.perform(dev, LOCAL_ORG, agent, "deadline?")
    assert out["answer"] == "draft answer" and out["self_critique"] is None and "overloaded" in out["error"]
    assert dev.run(LOCAL_ORG, agent, out["run_id"])


def test_system_prompt_order():
    ctx = actor.ActorContext(llm=None, tools=Registry(), domain="tax", lessons={1: "lesson"},
                             exemplars=[{"input": "q", "expected_output": "a"}])
    cfg = {"system_prompt": "Be brief.", "playbook": "- play", "lessons": [1],
           "tools": {"enabled": [], "macros": [], "max_steps": 1}}
    s = actor.build_system(cfg, ctx, [{"title": "doc", "text": "fact"}])
    order = [s.index(x) for x in ("tax", "Be brief.", "- play", "lesson", "Task: q", "fact", "missing")]
    assert order == sorted(order)


def test_study_evaluate_and_cycles_are_jobs(dev, agent):
    no_revise(dev, LOCAL_ORG, agent)
    dev.add_knowledge(LOCAL_ORG, agent, "guide", "The filing deadline is July 31.")
    job = jobs.enqueue(dev, LOCAL_ORG, agent, "study", {"rounds": 1})
    assert jobs.work(dev, once=True) == 1 and jobs.get(dev, LOCAL_ORG, job["id"])["result"]["studied"] == 1
    ds = dev.datasets(LOCAL_ORG, agent, "eval")[0]["id"]
    pending = dev.dataset(LOCAL_ORG, agent, ds, include_pending=True)["examples"]
    assert [e["status"] for e in pending] == ["pending"] and pending[0]["split"] == "dev"  # never its own exam
    dev.add_examples(LOCAL_ORG, agent, ds, [{"input": f"question {i}?", "expected_output": "GOOD"}
                                            for i in range(150)])  # ~45 held out: promotion needs 20
    ev = jobs.enqueue(dev, LOCAL_ORG, agent, "evaluate")
    jobs.work(dev, once=True)
    assert jobs.get(dev, LOCAL_ORG, ev["id"])["result"]["score"] == 0.0  # scored by `contains`, not by itself
    first = jobs.enqueue(dev, LOCAL_ORG, agent, "cycle", unique=True)
    assert jobs.enqueue(dev, LOCAL_ORG, agent, "cycle", unique=True)["id"] == first["id"]  # one cycle at a time
    jobs.work(dev, once=True)
    rec = jobs.get(dev, LOCAL_ORG, first["id"])["result"]
    # a new hosted agent is at autonomy 2: the holdout winner goes into a canary; production stays put
    assert rec["outcome"] == "canary_started" and rec["holdout"]["candidate"] > rec["holdout"]["baseline"]
    assert dev.active_version(LOCAL_ORG, agent, "agent_config")["version"] == 2


def test_llm_config_guards(monkeypatch):
    monkeypatch.delenv("SIA_ALLOW_CUSTOM_BASE_URL", raising=False)
    with pytest.raises(LLMError):
        LLM("gemini", "")
    with pytest.raises(LLMError):
        LLM("openai", "k", "m", base_url="http://169.254.169.254")  # SSRF guard
    with pytest.raises(LLMError):
        LLM("groq", "k")  # needs explicit model
    with pytest.raises(LLMError):
        LLM("nope", "k")
    assert LLM("gemini", "k").models == ["gemini-flash-latest", "gemini-flash-lite-latest"]


def test_model_fallback_on_overload(monkeypatch):
    import httpx
    hits = []

    def handler(request):
        hits.append(request.url.path)
        if "bad-model" in request.url.path:
            return httpx.Response(503, json={"error": "overloaded"})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    monkeypatch.setattr("time.sleep", lambda s: None)
    llm = LLM("gemini", "k", "bad-model, good-model")
    llm.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert llm.text("s", "p") == "ok"
    assert sum("bad-model" in h for h in hits) == 3 and "good-model" in hits[-1]

    llm = LLM("gemini", "k", "bad-model")
    llm.http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError):
        llm.text("s", "p")


def make_api(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("SIA_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("SIA_ADMIN_KEY", ADMIN)
    monkeypatch.delenv("SIA_LOCAL_MODE", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient

    import sia.api
    api = importlib.reload(sia.api)
    return api, TestClient(api.app)


def test_hosted_agent_api_flow_and_tenant_isolation(tmp_path, monkeypatch):
    api, c = make_api(tmp_path, monkeypatch)
    k1 = c.post("/v1/tenants", json={"name": "a"}, headers={"X-Admin-Key": ADMIN}).json()["api_key"]
    k2 = c.post("/v1/tenants", json={"name": "b"}, headers={"X-Admin-Key": ADMIN}).json()["api_key"]
    h1, h2 = {"Authorization": f"Bearer {k1}", "X-LLM-Key": "x"}, {"Authorization": f"Bearer {k2}"}
    org1 = api.store.tenant_by_key(k1)["id"]
    assert c.get("/v1/agents", headers=h1).status_code == 404  # the old managed-agents API is gone

    pid = c.post("/v1/projects", json={"name": "tax", "settings": {"kind": "agent", "agent": {
        "domain": "tax", "instructions": "Answer tax questions."}}}, headers=h1).json()["id"]
    active = c.get(f"/v1/projects/{pid}/versions/active", params={"name": "agent_config"}, headers=h1).json()
    assert active["version"] == 1 and "Answer tax questions." in active["content"]  # ready to take tasks
    assert c.get(f"/v1/projects/{pid}", headers=h2).status_code == 404  # another tenant can't see it

    r = c.post(f"/v1/projects/{pid}/tasks", json={"task": "deadline?"}, headers=h1).json()
    assert r["self_critique"] == 0.9 and r["llm_calls"] > 1 and r["new_lessons"] == [LESSON]
    assert c.post(f"/v1/projects/{pid}/tasks", json={"task": "x"}, headers=h2).status_code == 404
    fb = c.post(f"/v1/projects/{pid}/runs/{r['run_id']}/feedback", json={"rating": -1, "correction": "July 31"},
                headers=h1).json()
    assert fb["status"] == "pending"  # nothing changes until a person verifies it
    c.post(f"/v1/admin/orgs/{org1}/plan", json={"plan": "pro"}, headers={"X-Admin-Key": ADMIN})
    ds = c.post(f"/v1/projects/{pid}/datasets", json={"name": "eval"}, headers=h1).json()["id"]
    c.post(f"/v1/projects/{pid}/feedback/{fb['id']}/review", json={"status": "verified", "dataset_id": ds},
           headers=h1)
    api.dev.add_examples(org1, pid, ds, [{"input": f"q{i}?", "expected_output": "GOOD"} for i in range(150)])
    no_revise(api.dev, org1, pid)
    run = c.post(f"/v1/projects/{pid}/loop/run", headers=h1)
    assert run.status_code == 202
    jobs.work(api.dev, once=True)
    hist = c.get(f"/v1/projects/{pid}/loop/history", headers=h1).json()
    assert hist[0]["outcome"] == "canary_started" and hist[0]["calls"] > 0
    assert c.get(f"/v1/projects/{pid}/loop/history", headers=h2).status_code == 404
    lid = c.get(f"/v1/projects/{pid}/lessons", headers=h1).json()[0]["id"]
    assert c.post(f"/v1/projects/{pid}/lessons/{lid}/retire", headers=h2).status_code == 404
    assert c.post(f"/v1/projects/{pid}/lessons/{lid}/retire", headers=h1).json()["state"] == "retired"

    # metering is per tenant and survives the project's deletion
    assert c.delete(f"/v1/projects/{pid}", headers=h1).status_code == 204
    used = c.get("/v1/usage", headers=h1).json()
    assert {"tasks", "loop"} <= set(used["by_action"]) and used["llm_calls"] > 0
    assert c.get("/v1/usage", headers=h2).json()["requests"] == 0

    # key rotation: new key works, old key is dead
    k1b = c.post("/v1/keys/rotate", headers=h1).json()["api_key"]
    assert c.get("/v1/projects", headers=h1).status_code == 401
    assert c.get("/v1/projects", headers={"Authorization": f"Bearer {k1b}"}).status_code == 200


def test_legacy_agents_are_migrated_and_their_tables_dropped_on_start(tmp_path, monkeypatch):
    from test_migration_agents import AGENT, ORG, legacy_db
    legacy_db(str(tmp_path / "api.db"))
    api, c = make_api(tmp_path, monkeypatch)
    key = api.store.create_key(ORG)["api_key"]
    p = c.get(f"/v1/projects/{AGENT}", headers={"Authorization": f"Bearer {key}"}).json()
    assert p["settings"]["kind"] == "agent" and p["settings"]["agent"]["name"] == "tax"
    with sqlite3.connect(str(tmp_path / "api.db")) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {"agents", "playbooks", "episodes", "examples", "docs"} and "lessons" in tables


def test_api_rate_limit(tmp_path, monkeypatch):
    api, c = make_api(tmp_path, monkeypatch, SIA_RATE_LIMIT="3")
    h = {"Authorization": "Bearer " + c.post("/v1/tenants", json={"name": "a"},
                                              headers={"X-Admin-Key": ADMIN}).json()["api_key"]}
    assert [c.get("/v1/projects", headers=h).status_code for _ in range(4)] == [200, 200, 200, 429]
    assert int(c.get("/v1/projects", headers=h).headers["Retry-After"]) > 0


def test_landing_page_is_served_without_shadowing_the_api(tmp_path, monkeypatch):
    api, c = make_api(tmp_path, monkeypatch)
    r = c.get("/")
    # one product: the page sells SIA DevTools, not the old hosted agent
    assert r.status_code == 200 and "Ship prompt changes only when your own test cases prove they help." in r.text
    assert "Agents that learn your domain" not in r.text and "/v1/agents/" not in r.text
    assert c.get("/health").json()["ok"] and c.get("/docs").status_code == 200
    assert c.get("/v1/me").status_code == 401 and c.get("/dashboard").status_code == 200
    assert c.get("/llms.txt").status_code == 200 and c.get("/agent.md").status_code == 200
    # legal pages and a designed 404 (docs/DESIGN_RULES.md section 7)
    assert "Privacy policy" in c.get("/privacy").text and "Terms of service" in c.get("/terms").text
    missing = c.get("/no-such-page")
    assert missing.status_code == 404 and "doesn't exist" in missing.text
    assert c.get("/v1/no-such-route").headers["content-type"].startswith("application/json")
