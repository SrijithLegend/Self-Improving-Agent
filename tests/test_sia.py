"""Offline tests: a scripted fake LLM drives the whole learning loop deterministically."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sia.agent import Agent  # noqa: E402
from sia.llm import LLM, LLMError, parse_json  # noqa: E402
from sia.store import Store, chunk_text, fts_query  # noqa: E402

LESSON = "Always state the governing rule before giving the conclusion."


class FakeLLM:
    """Answers 'GOOD' only when coached (by reviewer feedback or a playbook saying so); grades GOOD 9/10, else 4/10."""

    def __init__(self):
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
        return {"lessons": [LESSON]}


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


@pytest.fixture
def agent(store):
    t = store.create_tenant("acme")
    return store.create_agent(t["tenant_id"], "tax", "personal income tax")


def test_chunking_and_fts_sanitizing():
    chunks = chunk_text("a" * 2500 + "\n\nshort para")
    assert [len(c) for c in chunks] == [1000, 1000, 500 + 2 + 10]
    assert fts_query('NEAR(x) "drop" OR * tax-deadline') == '"near" OR "drop" OR "tax" OR "deadline"'
    assert parse_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json("no json here")


def test_knowledge_search_is_scoped_to_agent(store, agent):
    other = store.create_agent("t2", "x", "y")
    store.add_doc(agent["id"], "rules", "The filing deadline is July 31.\n\nPenalties apply after.")
    store.add_doc(other["id"], "rules", "The filing deadline is March 1.")
    hits = store.search_chunks(agent["id"], "when is the filing deadline?")
    assert hits and all("July" in h["text"] for h in hits)


def test_lessons_dedupe_credit_and_prune(store, agent):
    lid = store.add_lesson(agent["id"], LESSON, "reflection")
    assert lid and store.add_lesson(agent["id"], LESSON.lower(), "reflection") is None  # near-duplicate
    for _ in range(5):
        store.credit_lessons([lid], 0.0)  # keeps coinciding with failure
    assert store.lessons(agent["id"]) == []  # retired
    assert store.lessons(agent["id"], include_inactive=True)[0]["uses"] == 5


def test_run_revises_learns_and_records(store, agent):
    llm = FakeLLM()
    out = Agent(store, llm, agent).run("When must I file?")
    assert out["answer"] == "GOOD answer" and out["score"] == 0.9 and out["attempts"] == 2
    assert out["new_lessons"] == [LESSON]
    assert store.episode(agent["id"], out["episode_id"])["score"] == 0.9
    # next task retrieves the learned lesson and credits it with the outcome
    out2 = Agent(store, llm, agent).run("Must I state the rule?")
    assert LESSON in out2["lessons_used"]
    assert store.lessons(agent["id"])[0]["uses"] == 1


def test_no_learning_mode_is_one_call(store, agent):
    llm = FakeLLM()
    out = Agent(store, llm, agent).run("hi", learn=False)
    assert llm.calls == 1 and out["critique"] is None and out["lessons_used"] == []
    assert store.episode(agent["id"], out["episode_id"])["score"] is None  # recorded, unscored
    Agent(store, llm, agent).feedback(out["episode_id"], 2, correction="better")  # fast answers can be corrected
    assert store.stats(agent["id"])["episodes"] == 0  # unscored episodes stay out of the learning curve


def test_failed_learning_keeps_the_answer(store, agent):
    class FlakyGrader(FakeLLM):
        def json(self, system, prompt, temperature=0.2):
            raise LLMError("grader overloaded")

    out = Agent(store, FlakyGrader(), agent).run("deadline?")
    assert out["answer"] == "draft answer" and out["score"] is None and "overloaded" in out["learning_error"]
    assert store.episode(agent["id"], out["episode_id"])


def test_learning_curve_windows_do_not_overlap(store, agent):
    for score in (0.2, 0.4, 0.8):
        store.add_episode(agent["id"], "t", "a", score, {}, 1, [])
    st = store.stats(agent["id"])
    assert st["avg_score_first_20"] == 0.2 and st["avg_score_last_20"] == 0.8


def test_system_prompt_order():
    from sia.agent import build_system
    s = build_system({"domain": "tax", "instructions": "Be brief."}, "- play", [{"text": "lesson"}],
                     [{"title": "doc", "text": "fact"}])
    order = [s.index(x) for x in ("tax", "Be brief.", "- play", "lesson", "fact", "missing")]
    assert order == sorted(order)
    assert "missing" in build_system({"domain": "tax", "instructions": ""}, "", [], [])


def test_feedback_correction_becomes_eval_example(store, agent):
    llm = FakeLLM()
    a = Agent(store, llm, agent)
    ep = a.run("deadline?", max_revisions=0)["episode_id"]
    a.feedback(ep, 1, correction="July 31")
    assert store.examples(agent["id"])[0]["expected"] == "July 31"
    assert store.episode(agent["id"], ep)["rating"] == 1
    with pytest.raises(KeyError):
        a.feedback("nope", 5)


def test_study_and_gated_improve(store, agent):
    llm = FakeLLM()
    a = Agent(store, llm, agent)
    with pytest.raises(ValueError):
        a.improve()  # no eval set yet
    store.add_doc(agent["id"], "guide", "The filing deadline is July 31.")
    assert a.study(rounds=1)["studied"] == 1 and store.examples(agent["id"])

    first = a.improve()
    assert first["adopted"] and first["new_score"] > first["old_score"]
    assert store.active_playbook(agent["id"])["content"] == "- Always be GOOD"

    second = a.improve()  # identical candidate: no gain, so the active version must not change
    assert not second["adopted"] and second["active_version"] == first["active_version"]
    assert store.active_playbook(agent["id"])["version"] == first["active_version"]

    assert not store.activate_playbook(agent["id"], 99)  # unknown version leaves current active
    assert store.active_playbook(agent["id"])["version"] == first["active_version"]
    assert store.activate_playbook(agent["id"], 0) and store.active_playbook(agent["id"])["version"] == 0


def test_adoption_needs_gain_beyond_noise(store, agent):
    from sia.agent import significant_gain
    assert significant_gain([0.4, 0.4, 0.4], [0.9, 0.9, 0.9])          # consistent gain
    assert not significant_gain([0.5, 0.5], [0.5, 0.5])                # equal
    assert not significant_gain([0.5, 0.5], [0.51, 0.52])              # below the margin
    assert not significant_gain([0.5, 0.5, 0.5, 0.5], [1.0, 0.0, 1.0, 0.2])  # +0.05 on average, but pure noise

    llm = FakeLLM()
    store.add_example(agent["id"], "deadline?", "July 31", "user")
    out = Agent(store, llm, agent).improve(trials=2)
    assert out["adopted"] and out["trials"] == 2
    assert llm.calls == 1 + 2 * 2 * 2  # playbook draft + 2 playbooks x 2 trials x (answer + grade)


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


def test_api_flow_and_tenant_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("SIA_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("SIA_ADMIN_KEY", "admin")
    import importlib

    from fastapi.testclient import TestClient

    import sia.api
    api = importlib.reload(sia.api)
    monkeypatch.setattr(api, "LLM", lambda *a, **k: FakeLLM())
    c = TestClient(api.app)

    assert c.post("/v1/tenants", json={"name": "a"}).status_code == 403
    k1 = c.post("/v1/tenants", json={"name": "a"}, headers={"X-Admin-Key": "admin"}).json()["api_key"]
    k2 = c.post("/v1/tenants", json={"name": "b"}, headers={"X-Admin-Key": "admin"}).json()["api_key"]
    h1, h2 = {"Authorization": f"Bearer {k1}", "X-LLM-Key": "x"}, {"Authorization": f"Bearer {k2}"}

    assert c.get("/v1/agents").status_code == 401
    aid = c.post("/v1/agents", json={"name": "tax", "domain": "tax"}, headers=h1).json()["id"]
    assert c.get(f"/v1/agents/{aid}", headers=h2).status_code == 404  # other tenant can't see it

    assert c.post(f"/v1/agents/{aid}/knowledge", json={"title": "g", "content": "Deadline July 31."},
                  headers=h1).status_code == 201
    r = c.post(f"/v1/agents/{aid}/run", json={"task": "deadline?"}, headers=h1).json()
    assert r["score"] == 0.9 and r["llm_calls"] > 1
    assert c.post(f"/v1/agents/{aid}/feedback", json={"episode_id": r["episode_id"], "rating": 2,
                                                       "correction": "July 31"}, headers=h1).status_code == 200
    assert c.post(f"/v1/agents/{aid}/improve", json={}, headers=h1).json()["adopted"]
    stats = c.get(f"/v1/agents/{aid}", headers=h1).json()["stats"]
    assert stats["episodes"] == 1 and stats["playbook_version"] == 1 and stats["active_lessons"] >= 1
    assert c.post(f"/v1/agents/{aid}/playbooks/7/activate", headers=h1).status_code == 404
    assert c.delete(f"/v1/agents/{aid}/knowledge/nope", headers=h1).status_code == 404
    assert c.delete(f"/v1/agents/{aid}/examples/999", headers=h1).status_code == 404
    assert c.post(f"/v1/agents/{aid}/lessons/999/disable", headers=h1).status_code == 404
    lid = c.get(f"/v1/agents/{aid}/lessons", headers=h1).json()[0]["id"]
    assert c.post(f"/v1/agents/{aid}/lessons/{lid}/disable", headers=h1).json()["active"] is False
    assert c.post(f"/v1/agents/{aid}/lessons/{lid}/disable", headers=h2).status_code == 404
    assert c.delete(f"/v1/agents/{aid}", headers=h1).status_code == 204
    assert c.get(f"/v1/agents/{aid}", headers=h1).status_code == 404
