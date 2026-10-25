"""The model client's provider branches, the CLI's everyday commands, and the tools' edge cases."""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sia.llm import LLM, LLMError  # noqa: E402
from sia_devtools import SIAClient  # noqa: E402
from sia_devtools.cli import main as cli  # noqa: E402
from sia_devtools.errors import ValidationError  # noqa: E402
from sia_loop import config, evaluator  # noqa: E402
from sia_loop.tools import builtin, macro  # noqa: E402
from sia_loop.tools.registry import Registry  # noqa: E402


def mock(llm, handler):
    seen = []

    def h(request):
        seen.append(request)
        return handler(request)
    llm.http = httpx.Client(transport=httpx.MockTransport(h))
    return seen


def test_anthropic_and_openai_compatible_requests():
    a = LLM("anthropic", "sk-ant-secret", "claude-haiku-5-5")
    sent = mock(a, lambda r: httpx.Response(200, json={"content": [{"text": '{"final": "ok"}'}],
                                                        "usage": {"input_tokens": 12, "output_tokens": 3}}))
    assert a.json("sys", "p") == {"final": "ok"} and a.calls == 1
    body = json.loads(sent[0].content)
    assert sent[0].headers["x-api-key"] == "sk-ant-secret" and body["system"].startswith("sys")
    assert a.last_usage == {"model": "claude-haiku-5-5", "prompt_tokens": 12, "completion_tokens": 3}

    o = LLM("groq", "gsk-secret", "llama-3.3-70b")
    sent = mock(o, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}],
                                                        "usage": {"prompt_tokens": 5, "completion_tokens": 1}}))
    assert o.text("sys", "p") == "hi" and sent[0].url.host == "api.groq.com"
    assert json.loads(sent[0].content)["messages"][0] == {"role": "system", "content": "sys"}

    mock(o, lambda r: httpx.Response(401, text="bad key gsk-secret"))
    with pytest.raises(LLMError) as e:
        o.text("s", "p")
    assert "gsk-secret" not in str(e.value) and "***" in str(e.value)  # the key never leaks into errors
    mock(o, lambda r: httpx.Response(200, json={"choices": []}))
    with pytest.raises(LLMError, match="no content"):
        o.text("s", "p")
    g = LLM("gemini", "k", "m")
    mock(g, lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(LLMError, match="no content"):
        g.text("s", "p")


def test_a_reply_that_never_finishes_times_out(monkeypatch):
    import sia.llm
    g = LLM("gemini", "k", "m")
    g.tries = 1
    sent = mock(g, lambda r: httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}))
    assert g.text("s", "p") == "ok"
    assert json.loads(sent[0].content)["generationConfig"]["maxOutputTokens"] == 4096  # no runaway generations
    assert "thinkingConfig" not in json.loads(sent[0].content)["generationConfig"]
    g.thinking = "minimal"
    g.text("s", "p")
    assert json.loads(sent[1].content)["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    monkeypatch.setattr(sia.llm, "CALL_DEADLINE", -1)  # every reply is already past the deadline
    with pytest.raises(LLMError, match="no complete reply"):
        g.text("s", "p")


def test_config_thinking_level(tmp_path):
    s = SIAClient(project="t", local=str(tmp_path / "t.db")).project_settings()
    ok = config.default_config(model={**config.DEFAULT_CONFIG["model"], "thinking": "minimal"})
    assert config.validate(ok, s)["model"]["thinking"] == "minimal"
    with pytest.raises(ValidationError, match="model.thinking"):
        config.validate(config.default_config(model={**config.DEFAULT_CONFIG["model"], "thinking": "lots"}), s)


def test_cli_everyday_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "c.db")
    base = ["--project", "shop", "--local", db]
    (tmp_path / "p1.txt").write_text("Be helpful.", encoding="utf-8")
    (tmp_path / "p2.txt").write_text("Be helpful and brief.", encoding="utf-8")
    assert cli(["versions", "create", *base, "--file", "p1.txt"]) == 0
    assert cli(["versions", "approve", "1", *base]) == 0 and cli(["versions", "activate", "1", *base]) == 0
    assert cli(["versions", "create", *base, "--file", "p2.txt"]) == 0
    assert cli(["versions", "reject", "2", *base, "--reason", "no evidence"]) == 0
    assert cli(["versions", "show", "1", *base]) == 0 and "Be helpful." in capsys.readouterr().out
    assert cli(["versions", "list", *base]) == 0 and "rejected" in capsys.readouterr().out
    sia = SIAClient(project="shop", local=db)
    with sia.start_run("answer", "q") as run:
        run.log_output("a")
    sia.feedback(run, -1, correction="better")
    assert cli(["runs", "list", *base]) == 0 and run.id[:12] in capsys.readouterr().out
    assert cli(["runs", "show", run.id, *base]) == 0 and '"output": "a"' in capsys.readouterr().out
    assert cli(["runs", "export", *base, "--out", "runs.jsonl"]) == 0 and (tmp_path / "runs.jsonl").exists()
    fid = sia.list_feedback()[0]["id"]
    assert cli(["feedback", "list", *base]) == 0 and fid[:12] in capsys.readouterr().out
    assert cli(["feedback", "review", fid, *base, "--verify", "--dataset", "eval"]) == 0
    assert "added as example" in capsys.readouterr().out
    assert cli(["canary", "start", *base]) == 4  # needs a version
    assert cli(["loop", "status", *base]) == 0 and "autonomy   1" in capsys.readouterr().out
    assert cli(["loop", "history", *base]) == 0 and "0 cycle(s)" in capsys.readouterr().out
    assert cli(["loop", "run", *base]) == 4  # your own app needs --target
    monkeypatch.setenv("SIA_DB", str(tmp_path / "server.db"))
    assert cli(["create-org", "acme"]) == 0
    org = json.loads(capsys.readouterr().out)["tenant_id"]
    assert cli(["set-plan", org, "pro"]) == 0 and cli(["set-plan", "nope", "pro"]) == 4


def test_tools_refuse_bad_input_politely():
    rows = [{"sku": "A1", "name": "tent", "price": 9000}, {"sku": "B2", "name": "lamp", "price": 900}]
    reg = Registry(search=lambda q, k: [], get_table=lambda n: rows if n == "catalog" else None)
    c = config.default_config(tools={"enabled": ["lookup_table", "search_knowledge", "calculator"], "macros": [],
                                     "max_steps": 3})
    assert json.loads(reg.call("lookup_table", {"table": "catalog", "where": {"sku": "b2"}}, c)) == [rows[1]]
    assert reg.call("lookup_table", {"table": "nope", "where": {"a": 1}}, c).startswith("error: no table")
    assert reg.call("lookup_table", {"table": "catalog"}, c).startswith("error:")
    assert reg.call("search_knowledge", {"query": "  "}, c).startswith("error:")
    assert reg.call("search_knowledge", {"query": "tents"}, c) == "no matching documents"
    assert reg.call("calculator", "1+1", c).startswith("error: tool arguments must be")
    assert reg.call("calculator", {"expression": 7}, c).startswith("error:")
    many = [{"sku": f"S{i}", "kind": "x"} for i in range(30)]
    assert "first 20 of 30" in builtin.lookup_table(lambda n: many, "t", {"kind": "x"})


def test_schema_subset_checks_every_kind_of_rule():
    s = {"type": "object", "required": ["a"], "additionalProperties": False, "properties": {
        "a": {"type": "string", "maxLength": 3}, "n": {"type": "integer", "minimum": 0, "maximum": 5},
        "xs": {"type": "array", "items": {"type": "string", "enum": ["x", "y"]}}}}
    macro.check_schema_shape(s)
    assert macro.schema_errors({"a": "ab", "n": 3, "xs": ["x"]}, s) == []
    errs = macro.schema_errors({"n": 9, "xs": ["z"], "extra": 1}, s)
    assert any("a is required" in e for e in errs) and any("out of range" in e for e in errs)
    assert any("not allowed" in e for e in errs) and any("one of" in e for e in errs)
    assert macro.schema_errors({"a": "abcd"}, s) == ["$.a has the wrong length"]
    assert macro.schema_errors(True, {"type": "integer"}) == ["$ should be integer"]
    for bad in ({"type": "date"}, {"pattern": "x"}, {"enum": []}, {"properties": {"a": {"type": "nope"}}}):
        with pytest.raises(ValidationError):
            macro.check_schema_shape(bad)
    assert macro.spec(macro.validate_macro({"name": "p", "kind": "pipeline", "steps": [
        {"tool": "calculator", "args": {"expression": "{x} * 2"}}]}, enabled_tools={"calculator"}))["args"] == {
        "type": "object", "properties": {"x": {"type": "string"}}}


def test_evaluator_scorer_settings():
    s = {"loop": {"scorers": ["exact_match", "json_match", "regex:^DECISION"], "judge": None,
                  "allow_same_model_judge": False}, "agent": None}
    assert [x.name for x in evaluator.scorers_for(s)] == ["exact_match", "json_match", "regex:^DECISION"]
    with pytest.raises(ValidationError):
        evaluator.scorers_for({**s, "loop": {**s["loop"], "scorers": ["judge"]}})  # no judge model given
    with pytest.raises(ValidationError):
        evaluator.check_judge({**s, "loop": {**s["loop"], "scorers": ["judge"]}}, config.default_config())
    assert evaluator.check_judge(s, config.default_config()) is None  # deterministic only: nothing to check
