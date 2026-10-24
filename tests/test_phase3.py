"""Phase 3 of SELF_IMPROVING_SPEC.md: the actor, its tools, agent-made macros and output validators."""
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sia_loop import actor, config, models  # noqa: E402
from sia_loop.tools import builtin, macro  # noqa: E402
from sia_loop.tools.registry import Registry  # noqa: E402
from test_devtools import make_api, org  # noqa: E402

POLICY = ("Returns are accepted within 30 days of purchase (60 days for members). Refunds are the price minus a 15% "
          "restocking fee. Final-sale items can never be returned.")
CASES = {  # task -> the correct answer, which needs the policy (search) and exact arithmetic (calculator)
    "Can I return boots bought 20 days ago for Rs 1200?": "DECISION: YES. Refund: Rs 1020.0",
    "Can I return a tent bought 45 days ago for Rs 5000?": "DECISION: NO. Refund: Rs 0",
    "Member here: can I return a jacket bought 45 days ago for Rs 2000?": "DECISION: YES. Refund: Rs 1700.0",
    "Can I return a final-sale shirt bought 3 days ago for Rs 800?": "DECISION: NO. Refund: Rs 0",
}


class ScriptedModel:
    """Behaves like a careful tool-using model: looks the policy up, does the arithmetic with the calculator, then
    answers. Everything it knows about the policy comes from the tool results in its prompt."""

    def __init__(self, *a, **k):
        self.calls, self.prompts = 0, []

    def json(self, system, prompt, temperature=0.2):
        self.calls += 1
        self.prompts.append((system, prompt))
        if "ANSWER TO REVIEW" in prompt:
            return {"score": 9, "issues": [], "suggestion": ""}
        task = prompt.split("TASK:\n")[1].split("\n\nTOOL RESULTS")[0]
        results = prompt.split("TOOL RESULTS SO FAR:\n")[1] if "TOOL RESULTS SO FAR" in prompt else ""
        if "search_knowledge(" not in results:
            return {"tool": "search_knowledge", "args": {"query": "return policy days refund"}}
        days, price = (int(x) for x in re.findall(r"(\d+) days ago for Rs (\d+)", task)[0])
        window = 60 if "Member" in task and "60 days for members" in results else 30
        ok = days <= window and "final-sale" not in task
        if ok and "calculator(" not in results:
            fee = re.search(r"(\d+)% restocking", results).group(1)
            return {"tool": "calculator", "args": {"expression": f"{price} * (1 - {fee}/100)"}}
        refund = results.rsplit("-> ", 1)[1].strip() if ok else "0"
        return {"final": f"DECISION: {'YES' if ok else 'NO'}. Refund: Rs {refund}"}

    def text(self, system, prompt, temperature=0.7, as_json=False):
        self.calls += 1
        return "plain answer"


def cfg(**kw):
    return config.default_config(**kw)


# --- built-in tools -------------------------------------------------------------------------------------------
def test_calculator_rejects_names_and_calls():
    assert builtin.calculator("1200 * (1 - 15/100)") == "1020.0"
    assert builtin.calculator("round(10/3, 2) + max(1, 2) - min(4, 5) % 3") == "4.33"
    assert builtin.calculator("2 ** 10 // 3") == "341"
    for evil in ("__import__('os').system('id')", "open('/etc/passwd')", "x + 1", "().__class__", "abs(-1)",
                 "lambda: 1", "[1, 2]", "1 if 1 else 2", "round.__self__", "max(*[1])", "round(x=1)",
                 "9 ** 9 ** 9", "1/0", "'a' * 3", "", "1" * 201, "(-8) ** 0.5", "f'{1}'"):
        with pytest.raises(builtin.ToolError):
            builtin.calculator(evil)


def test_http_get_is_refused_unless_allowlisted():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/"})
        return httpx.Response(200, text="rate: 7%")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    on = cfg(tools={"enabled": ["http_get"], "macros": [], "max_steps": 6})
    reg = lambda allow, public=True: Registry(http_allowlist=allow, http_client=client,  # noqa: E731
                                              resolve=lambda h: public)
    assert reg([]).call("http_get", {"url": "https://rates.example/today"}, on).startswith("error: rates.example "
                                                                                            "is not on")
    assert reg(["rates.example"]).call("http_get", {"url": "https://rates.example/today"}, on) == "HTTP 200\nrate: 7%"
    for url in ("http://rates.example/today", "https://rates.example:8443/x", "https://user:pw@rates.example/",
                "file:///etc/passwd"):
        assert reg(["rates.example"]).call("http_get", {"url": url}, on).startswith("error:"), url
    assert "public address" in reg(["rates.example"], public=False).call("http_get", {"url": "https://rates.example/"},
                                                                         on)
    assert "redirects" in reg(["rates.example"]).call("http_get", {"url": "https://rates.example/moved"}, on)
    assert seen == ["https://rates.example/today", "https://rates.example/moved"]  # nothing else was fetched
    assert "disabled" in reg(["rates.example"]).call("http_get", {"url": "https://rates.example/today"}, cfg())
    assert not builtin.public_address("localhost") and not builtin.public_address("10.0.0.1")
    with pytest.raises(Exception):  # and a project has to opt in before a config may even enable it
        config.validate(on, {"locked_text": [], "loop": {"allowed_tools": ["calculator"], "allowed_models": [
            "gemini-flash-latest"], "allow_macros": True}})


def test_macro_templating_cannot_execute_code():
    sent = []

    class Echo:
        calls = 0

        def json(self, system, prompt, temperature=0.2):
            sent.append(prompt)
            return {"result": "WITHIN"}

    m = macro.validate_macro({"name": "window", "kind": "prompt", "template": "days={days} note={note}",
                              "input_schema": {"type": "object", "properties": {"days": {"type": "integer"},
                                                                                "note": {"type": "string"}}},
                              "output_schema": {"type": "string", "enum": ["WITHIN", "OUTSIDE"]}})
    payloads = ["{0.__class__.__mro__}", "__import__('os').system('echo pwned')", "{days.__class__}", "${7*7}",
                "{{note}}", "%s%s%n", "\x00"]
    for p in payloads:
        assert macro.run_macro(m, {"days": 3, "note": p}, Echo(), None) == "WITHIN"
        assert f"note={p}" in sent[-1]  # passed through as characters, never evaluated
    assert macro.run_macro(m, {"days": "3"}, Echo(), None).startswith("error: $.days should be integer")
    pipe = macro.validate_macro({"name": "calc", "kind": "pipeline", "steps": [
        {"tool": "calculator", "args": {"expression": "{expr}"}}]}, enabled_tools={"calculator"})
    c = cfg(tools={"enabled": ["calculator"], "macros": [pipe], "max_steps": 6})
    assert Registry().call("calc", {"expr": "__import__('os')"}, c).startswith("[calculator] error:")
    assert Registry().call("calc", {"expr": "6*7"}, c) == "[calculator] 42"


# --- the actor loop -------------------------------------------------------------------------------------------
class Loopy:
    """Never stops calling tools."""

    def __init__(self, tool="calculator"):
        self.calls, self.tool = 0, tool

    def json(self, system, prompt, temperature=0.2):
        self.calls += 1
        return {"tool": self.tool, "args": {"expression": "1+1"}}

    def text(self, system, prompt, temperature=0.7, as_json=False):
        self.calls += 1
        return "gave up looping"


def test_tool_loop_stops_at_max_steps():
    llm = Loopy()
    c = cfg(tools={"enabled": ["calculator"], "macros": [], "max_steps": 3},
            revise={"max_revisions": 0, "pass_score": 0.75, "critic": "self"})
    out = actor.run_task(c, "loop forever", actor.ActorContext(llm=llm, tools=Registry()))
    assert len(out.transcript) == 3 and out.answer == "gave up looping" and llm.calls == 4 and out.calls == 4


def test_disabled_tool_returns_an_error_to_the_model():
    llm = Loopy("http_get")
    c = cfg(tools={"enabled": ["calculator"], "macros": [], "max_steps": 2},
            revise={"max_revisions": 0, "pass_score": 0.75, "critic": "self"})
    out = actor.run_task(c, "fetch it", actor.ActorContext(llm=llm, tools=Registry()))
    assert all(s["result"].startswith("error: unknown or disabled tool 'http_get'") for s in out.transcript)
    assert "calculator" in out.transcript[0]["result"]  # the model is told what it can use instead


def test_validators_are_deterministic():
    vals = [config.validate_validator({"type": "regex", "name": "decision", "pattern": r"^DECISION: (YES|NO)"}),
            config.validate_validator({"type": "json_schema", "name": "shape", "schema": {
                "type": "object", "required": ["refund"], "properties": {"refund": {"type": "number"}}}})]
    assert actor.run_validators(vals, "DECISION: YES") == {"validator:decision": 1, "validator:shape": 0}
    assert actor.run_validators(vals, '{"refund": 12}') == {"validator:decision": 0, "validator:shape": 1}


def test_offline_actor_answers_the_demo_cases_with_tools(tmp_path, monkeypatch):
    api, c = make_api(tmp_path, monkeypatch)
    monkeypatch.setattr(models, "factory", ScriptedModel)
    monkeypatch.setenv("SIA_LLM_KEY_GEMINI", "server-key")
    h = {"Authorization": f"Bearer {org(c)['api_key']}"}
    pid = c.post("/v1/projects", json={"name": "returns", "settings": {"kind": "agent", "agent": {
        "domain": "retail returns"}}}, headers=h).json()["id"]
    c.post(f"/v1/projects/{pid}/knowledge", json={"title": "Returns policy", "content": POLICY}, headers=h)
    c.post(f"/v1/projects/{pid}/tables", json={"name": "members", "csv": "id,name\n7,Asha\n"}, headers=h)
    assert c.get(f"/v1/projects/{pid}/tables", headers=h).json()[0]["rows"] == 1
    bundle = cfg(system_prompt="Decide returns.", retrieval={"enabled": False, "k": 0, "max_chars_per_chunk": 1000},
                 validators=[{"type": "regex", "name": "decision", "pattern": r"^DECISION: (YES|NO)\."}])
    v = c.post(f"/v1/projects/{pid}/versions", json={"name": "agent_config", "content": json.dumps(bundle)},
               headers=h).json()
    assert v["version"] == 2  # @1 is the config every new hosted agent starts with
    c.post(f"/v1/projects/{pid}/versions/agent_config/{v['version']}/approve",
           json={"override_reason": "set up by hand for this test"}, headers=h)
    c.post(f"/v1/projects/{pid}/versions/agent_config/{v['version']}/activate", json={}, headers=h)

    for task, want in CASES.items():
        r = c.post(f"/v1/projects/{pid}/tasks", json={"task": task, "revise": False}, headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["answer"] == want and out["checks"] == {"validator:decision": 1} and out["version"] == 2
        assert out["tools_used"][0] == "search_knowledge"
        assert ("calculator" in out["tools_used"]) == ("YES" in want)
        run = c.get(f"/v1/projects/{pid}/runs/{out['run_id']}", headers=h).json()
        assert run["version_name"] == "agent_config" and run["scores"] == {"validator:decision": 1}
        assert [ch["name"] for ch in run["children"]] == [f"tool:{t}" for t in out["tools_used"]]
    used = c.get("/v1/usage", headers=h).json()["by_action"]["tasks"]
    assert used["requests"] == 4 and used["llm_calls"] == 4 * 2 + 2  # 3 calls for each YES, 2 for each NO

    # a key sent for another provider than the config's is refused instead of being sent to the wrong API
    r = c.post(f"/v1/projects/{pid}/tasks", json={"task": "x"},
               headers={**h, "X-LLM-Provider": "openai", "X-LLM-Key": "sk-test"})
    assert r.status_code == 400 and "gemini" in r.json()["detail"]
    h2 = {"Authorization": f"Bearer {org(c)['api_key']}"}
    app = c.post("/v1/projects", json={"name": "app"}, headers=h2).json()["id"]
    r = c.post(f"/v1/projects/{app}/tasks", json={"task": "x"}, headers=h2)
    assert r.status_code == 400 and "kind" in r.json()["detail"]  # your own app's project doesn't run hosted tasks
    assert c.post(f"/v1/projects/{app}/tasks", json={"task": "x"}, headers=h).status_code == 404  # another org's


def test_tasks_need_a_server_key_or_the_callers_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SIA_LLM_KEY_GEMINI", raising=False)
    with pytest.raises(Exception, match="SIA_LLM_KEY_GEMINI"):
        models.make("gemini", ["gemini-flash-latest"])
    monkeypatch.setattr(models, "factory", lambda p, k, m: (p, k, m))
    assert models.make("gemini", ["a", "b"], key="caller") == ("gemini", "caller", "a,b")
