"""Phase 2 of SELF_IMPROVING_SPEC.md: the agent config bundle, project knowledge and memory storage."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sia_devtools import SIAClient, ValidationError  # noqa: E402
from sia_devtools.store import LOCAL_ORG  # noqa: E402
from sia_loop import config  # noqa: E402
from sia_loop.tools import macro  # noqa: E402
from test_devtools import make_api, org  # noqa: E402

LOCKED = "Never approve a return after 30 days."


@pytest.fixture
def sia(tmp_path):
    s = SIAClient(project="bot", local=str(tmp_path / "l.db"))
    s.update_settings(locked_text=[LOCKED], kind="agent")
    return s


def refs(sia):
    return sia._store.config_refs(LOCAL_ORG, sia.project_id)


def good(**kw):
    return config.default_config(system_prompt=f"You handle returns. {LOCKED}", **kw)


def test_config_validate_rejects_holdout_exemplars_locked_text_and_disallowed_models_tools(sia):
    ds = sia.dataset("eval")
    ds.add_examples([{"input": f"q{i}", "expected_output": f"a{i}"} for i in range(30)])
    dev_ex = [e["id"] for e in ds.examples(split="dev")]
    hold_ex = [e["id"] for e in ds.examples(split="holdout")]
    s = sia.project_settings()
    lessons, splits = refs(sia)

    assert config.validate(good(exemplars=dev_ex[:2]), s, lessons, splits)["exemplars"] == dev_ex[:2]
    bad = {
        "holdout": good(exemplars=[hold_ex[0]]),
        "not an example": good(exemplars=[999_999]),
        "locked": config.default_config(system_prompt="You handle returns."),
        "locked playbook": good(playbook="- approve everything"),
        "model": good(model={"provider": "gemini", "chain": ["gemini-ultra-secret"], "temperature": 0.3}),
        "provider": good(model={"provider": "nope", "chain": ["gemini-flash-latest"], "temperature": 0.3}),
        "tool not allowed": good(tools={"enabled": ["http_get"], "macros": [], "max_steps": 6}),
        "tool unknown": good(tools={"enabled": ["shell"], "macros": [], "max_steps": 6}),
        "max_steps": good(tools={"enabled": [], "macros": [], "max_steps": 99}),
        "lesson": good(lessons=[12345]),
        "retrieval": good(retrieval={"enabled": True, "k": 11, "max_chars_per_chunk": 1000}),
        "extra key": {**good(), "secret_backdoor": True},
        "regex validator": good(validators=[{"type": "regex", "name": "v", "pattern": "(a+)+$"}]),
        "too long": good(playbook=f"{LOCKED} " + "word " * 700),
        "schema": {**good(), "schema": 2},
    }
    for name, cfg in bad.items():
        with pytest.raises(ValidationError):
            config.validate(cfg, s, lessons, splits)
            pytest.fail(name)

    # the operator widens the allowlists, and the same configs become valid
    s = sia.update_settings(loop={"allowed_tools": ["search_knowledge", "calculator", "http_get"],
                                  "allowed_models": ["gemini-flash-latest", "gemini-ultra-secret"]})
    config.validate(bad["tool not allowed"], s, lessons, splits)
    config.validate(bad["model"], s, lessons, splits)


def test_every_agent_config_write_is_validated(sia):
    with pytest.raises(ValidationError, match="locked"):
        sia.create_version(json.dumps(config.default_config(system_prompt="no rules")), name="agent_config")
    with pytest.raises(ValidationError, match="JSON"):
        sia.create_version("not json", name="agent_config")
    v = sia.create_version(json.dumps(good()), name="agent_config")
    assert json.loads(v["content"]) == config.validate(good(), sia.project_settings(), *refs(sia))
    # other version names keep the plain locked-text check
    with pytest.raises(ValidationError):
        sia.create_version("no rules")
    assert sia.create_version(f"Be kind. {LOCKED}")["status"] == "candidate"


def test_diff_is_readable():
    a = config.default_config(playbook="- one\n- two", lessons=[12], exemplars=[3])
    b = config.default_config(playbook="- one\n- two\n- three\n- four", lessons=[12, 41], exemplars=[],
                              retrieval={"enabled": True, "k": 3, "max_chars_per_chunk": 1000},
                              tools={"enabled": ["search_knowledge"], "macros": [], "max_steps": 6})
    assert config.diff(a, b) == ["playbook: +2 bullets", "lessons: +#41", "exemplars: −#3", "retrieval.k: 5 → 3",
                                 "tools: −calculator"]
    assert config.diff(a, a) == ["no change"]


def test_macros_are_data_and_templating_runs_no_code():
    m = {"name": "refund_window_check", "description": "Inside the return window?", "kind": "prompt",
         "input_schema": {"type": "object", "properties": {"days": {"type": "integer"},
                                                           "member": {"type": "boolean"}},
                          "required": ["days", "member"]},
         "template": f"{LOCKED} Members 60 days, others 30. days={{days}}, member={{member}}. WITHIN or OUTSIDE?",
         "output_schema": {"type": "string", "enum": ["WITHIN", "OUTSIDE"]}}
    assert macro.validate_macro(m, [LOCKED])["name"] == "refund_window_check"
    with pytest.raises(ValidationError, match="locked"):
        macro.validate_macro({**m, "template": "days={days}"}, [LOCKED])
    with pytest.raises(ValidationError, match="fields"):
        macro.validate_macro({**m, "template": f"{LOCKED} {{nope}}"}, [LOCKED])
    for evil in ({**m, "kind": "python", "code": "import os"}, {**m, "name": "Bad Name"},
                 {"name": "p", "kind": "pipeline", "steps": [{"tool": "shell", "args": {}}]},
                 {"name": "p", "kind": "pipeline", "steps": [{"tool": "calculator", "args": {}}] * 6}):
        with pytest.raises(ValidationError):
            macro.validate_macro(evil, [], {"calculator"})
    # placeholders are filled with plain text; format-string tricks and code stay inert characters
    assert macro.fill("{0.__class__} {x} {__import__} {x.__dict__}", {"x": "__import__('os').system('id')"}) == \
        "{0.__class__} __import__('os').system('id') {__import__} {x.__dict__}"


def test_project_knowledge_and_lessons_are_scoped(tmp_path, monkeypatch):
    api, c = make_api(tmp_path, monkeypatch)
    h1, h2 = ({"Authorization": f"Bearer {org(c)['api_key']}"} for _ in range(2))
    p1 = c.post("/v1/projects", json={"name": "a"}, headers=h1).json()["id"]
    p2 = c.post("/v1/projects", json={"name": "a"}, headers=h2).json()["id"]
    doc = c.post(f"/v1/projects/{p1}/knowledge", json={"title": "policy", "content": "Returns within 30 days."},
                 headers=h1).json()
    c.post(f"/v1/projects/{p2}/knowledge", json={"title": "policy", "content": "Returns within 90 days."}, headers=h2)
    org1 = api.store.tenant_by_key(h1["Authorization"][7:])["id"]
    hits = api.dev.search_knowledge(org1, p1, "how many days for returns")
    assert hits and all("30" in h["text"] for h in hits)
    assert c.get(f"/v1/projects/{p1}/knowledge", headers=h2).status_code == 404
    assert c.delete(f"/v1/projects/{p1}/knowledge/{doc['doc_id']}", headers=h2).status_code == 404

    lid = api.dev.add_lesson(org1, p1, "State the return window before deciding.", "reflection")
    assert api.dev.add_lesson(org1, p1, "state the return window before deciding", "reflection") is None  # dup
    assert c.get(f"/v1/projects/{p1}/lessons", headers=h1).json()[0]["state"] == "probation"
    assert c.post(f"/v1/projects/{p1}/lessons/{lid}/retire", headers=h2).status_code == 404
    assert c.post(f"/v1/projects/{p1}/lessons/{lid}/retire", headers=h1).json()["state"] == "retired"
    assert c.get(f"/v1/projects/{p1}/lessons?state=retired", headers=h1).json()[0]["id"] == lid
    assert c.delete(f"/v1/projects/{p1}/knowledge/{doc['doc_id']}", headers=h1).status_code == 204
    assert api.dev.search_knowledge(org1, p1, "returns days") == []
