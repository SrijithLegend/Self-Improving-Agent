"""Phase 6 of SELF_IMPROVING_SPEC.md: triggers and back-off, notifications, the CLI and the dashboard loop panel."""
import importlib
import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

import loop_demo  # noqa: E402
from sia_devtools import SIAClient, ValidationError  # noqa: E402
from sia_devtools.cli import main as cli  # noqa: E402
from sia_devtools.store import LOCAL_ORG, DevStore  # noqa: E402
from sia_loop import config, jobs, models, notify  # noqa: E402


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "factory", loop_demo.OfflineModel)
    monkeypatch.setenv("SIA_LLM_KEY_GEMINI", "offline")
    dev = DevStore(str(tmp_path / "loop.db"))
    pid, did = loop_demo.setup(dev)
    return dev, pid, did


def age_examples(dev, seconds=7200):
    """The seeded examples existed before the (backdated) cycles below, as they would in real use."""
    with dev._conn(write=True) as c:
        c.execute("UPDATE dataset_examples SET created=created-?", (seconds,))


def finished_cycle(dev, pid, outcome, created, trigger="schedule"):
    j = jobs.enqueue(dev, LOCAL_ORG, pid, "cycle", {"trigger": trigger})
    with dev._conn(write=True) as c:
        c.execute("UPDATE jobs SET status='done', created=?, result=? WHERE id=?",
                  (created, json.dumps({"outcome": outcome}), j["id"]))


# --- triggers and back-off --------------------------------------------------------------------------------------
def test_back_off_doubles_after_3_empty_cycles():
    empty = lambda n, trigger="schedule": [{"result": {"outcome": "no_improvement"}, "payload": {  # noqa: E731
        "trigger": trigger}}] * n
    assert [jobs.interval_hours(24, empty(n)) for n in range(7)] == [24, 24, 24, 48, 96, 168, 168]  # cap: 7 days
    promoted = [{"result": {"outcome": "canary_started"}, "payload": {}}]
    assert jobs.interval_hours(24, empty(2) + promoted + empty(5)) == 24      # a promotion resets the count
    data = [{"result": {"outcome": "no_improvement"}, "payload": {"trigger": "new_examples"}}]
    assert jobs.interval_hours(24, empty(1) + data + empty(9)) == 24          # so does a data-triggered cycle
    assert jobs.interval_hours(10, empty(4)) == 40


def test_triggers_schedule_new_examples_and_back_off(agent):
    dev, pid, did = agent
    age_examples(dev)
    now = time.time()
    assert jobs.due(dev, LOCAL_ORG, pid, now) == "schedule"  # never run: due now
    finished_cycle(dev, pid, "no_improvement", now - 3600)
    assert jobs.due(dev, LOCAL_ORG, pid, now) is None        # ran an hour ago; every 24 h
    assert jobs.due(dev, LOCAL_ORG, pid, now + 25 * 3600) == "schedule"
    for i in (2, 3):  # three empty cycles in a row: the next one waits 48 h instead of 24
        finished_cycle(dev, pid, "no_improvement", now - 3600 + i)
    assert jobs.due(dev, LOCAL_ORG, pid, now + 25 * 3600) is None
    assert jobs.due(dev, LOCAL_ORG, pid, now + 49 * 3600) == "schedule"
    dev.add_examples(LOCAL_ORG, pid, did, [{"input": f"new case {i}", "expected_output": "DECISION: NO"}
                                           for i in range(10)])
    assert jobs.due(dev, LOCAL_ORG, pid, now) == "new_examples"   # new verified data doesn't wait for the schedule
    queued = jobs.tick(dev, now, force=True)
    assert [j["payload"]["trigger"] for j in queued] == ["new_examples"]
    assert jobs.due(dev, LOCAL_ORG, pid, now) is None and jobs.tick(dev, now, force=True) == []  # one at a time
    # nothing for paused loops, autonomy 0, or your own app (its loop runs where your code runs)
    with dev._conn(write=True) as c:
        c.execute("DELETE FROM jobs")
    for change in ({"loop": {"paused": True}}, {"loop": {"paused": False, "autonomy": 0}}, {"kind": "app"}):
        dev.update_project(LOCAL_ORG, pid, change)
        assert jobs.due(dev, LOCAL_ORG, pid, now) is None, change


def test_failure_rise_triggers_a_cycle(agent):
    dev, pid, _ = agent
    age_examples(dev)
    finished_cycle(dev, pid, "no_improvement", time.time() - 60)
    now = time.time()
    old = [{"id": f"o{i}", "name": "agent.run", "started": now - 3 * 86400 + i, "scores": {"validator:decision": 1}}
           for i in range(300)]
    new = [{"id": f"n{i}", "name": "agent.run", "started": now - 3600 + i, "scores": {
        "validator:decision": int(i % 3 != 0)}} for i in range(120)]
    dev.log_runs(LOCAL_ORG, pid, old + new)
    assert jobs.due(dev, LOCAL_ORG, pid, now) == "failure_rise"


# --- notifications --------------------------------------------------------------------------------------------
def test_webhook_deliveries_are_hmac_signed_and_retried(agent, monkeypatch):
    dev, pid, _ = agent
    with pytest.raises(ValidationError):
        dev.update_project(LOCAL_ORG, pid, {"loop": {"webhook_url": "http://hooks.example/sia"}})  # https only
    assert notify.emit(dev, LOCAL_ORG, pid, "loop.paused", {}) is None  # no URL: nothing is queued
    dev.update_project(LOCAL_ORG, pid, {"loop": {"webhook_url": "https://hooks.example/sia"}})
    got, fail = [], [True]

    def receiver(request):
        got.append(request)
        return httpx.Response(500 if fail[0] else 204)
    monkeypatch.setattr(notify, "client", httpx.Client(transport=httpx.MockTransport(receiver)))
    assert notify.emit(dev, LOCAL_ORG, pid, "not.an.event", {}) is None
    notify.emit(dev, LOCAL_ORG, pid, "canary.started", {"version": 2, "pct": 10})
    jobs.work(dev, once=True)
    assert len(got) == 1 and got[0].headers["X-SIA-Event"] == "canary.started"
    retry = [j for j in jobs.jobs(dev, LOCAL_ORG, pid, kind="notify") if j["status"] == "queued"]
    assert len(retry) == 1 and retry[0]["payload"]["attempt"] == 2  # the failed delivery is tried again later
    fail[0] = False
    with dev._conn(write=True) as c:
        c.execute("UPDATE jobs SET run_after=0 WHERE id=?", (retry[0]["id"],))
    jobs.work(dev, once=True)
    req = got[-1]
    secret = notify.secret(dev, LOCAL_ORG, pid)
    ts, sig, body = req.headers["X-SIA-Timestamp"], req.headers["X-SIA-Signature"], req.content
    assert notify.verify(secret, ts, body, sig)
    assert json.loads(body) == {"event": "canary.started", "project_id": pid, "data": {"version": 2, "pct": 10},
                                "ts": json.loads(body)["ts"]}
    assert not notify.verify(secret, ts, body.replace(b'"pct":10', b'"pct":50'), sig)  # tampered
    assert not notify.verify("whsec_other", ts, body, sig)                             # wrong secret
    assert not notify.verify(secret, str(int(ts) - 3600), body, sig)                   # replayed later
    rotated = notify.secret(dev, LOCAL_ORG, pid, rotate=True)
    assert rotated != secret and notify.secret(dev, LOCAL_ORG, pid) == rotated


def test_the_loop_emits_its_events(agent, monkeypatch):
    dev, pid, _ = agent
    dev.update_project(LOCAL_ORG, pid, {"loop": {"webhook_url": "https://hooks.example/sia", "autonomy": 2}})
    from sia_loop import cycle
    cycle.run_cycle(dev, LOCAL_ORG, pid, payload={"operators": ["rewrite_playbook", "edit_system_prompt"]})
    events = [j["payload"]["event"] for j in jobs.jobs(dev, LOCAL_ORG, pid, 50, kind="notify")]
    assert {"candidate.created", "candidate.rejected", "version.auto_approved", "canary.started"} <= set(events)


# --- the CLI ----------------------------------------------------------------------------------------------------
APP = '''
import re
RULE = "Members get 60 days to return; everyone else gets 30."

def answer(question, prompt):
    days = int(re.search(r"bought (\\d+) days", question).group(1))
    member, final = "member" in question, "final-sale" in question
    ok = not final and days <= (60 if member and RULE in prompt else 30)
    return f"DECISION: {'YES' if ok else 'NO'}"
'''


class PromptWriter:
    calls = 0

    def text(self, system, prompt, temperature=0.7, as_json=False):
        self.calls += 1
        current = prompt.split("CURRENT INSTRUCTIONS:\n")[1].split("\n\nFAILURES")[0]
        learned = " Members get 60 days to return; everyone else gets 30." if "member" in prompt else ""
        return json.dumps({"content": current + learned})


def test_cli_loop_run_for_your_own_app(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shopapp.py").write_text(APP, encoding="utf-8")
    db = str(tmp_path / "app.db")
    sia = SIAClient(project="shop", local=db)
    sia.dataset("eval").add_examples(loop_demo.cases(150))
    v = sia.create_version(loop_demo.BASE_PROMPT)
    sia.approve(v["version"])
    sia.activate(v["version"])
    monkeypatch.setattr("sia_devtools.cli._loop_llm", lambda a: PromptWriter())
    base = ["--project", "shop", "--local", db]
    assert cli(["loop", "run", *base, "--target", "shopapp:answer", "--scorer", "contains"]) == 0
    out = capsys.readouterr().out
    assert "holdout" in out and "cycle: awaiting_approval" in out  # a new app project is at autonomy 1
    cand = [x for x in sia.versions() if x["status"] == "candidate"]
    assert len(cand) == 1 and "Members get 60 days" in cand[0]["content"]

    sia.update_settings(loop={"autonomy": 2})
    assert cli(["loop", "run", *base, "--target", "shopapp:answer", "--scorer", "contains"]) == 0
    assert "cycle: canary_started" in capsys.readouterr().out
    assert cli(["loop", "status", *base]) == 0 and "canary @" in capsys.readouterr().out
    assert cli(["canary", "promote", *base]) == 0 and "active for everyone" in capsys.readouterr().out
    assert cli(["loop", "pause", *base]) == 0 and sia.project_settings()["loop"]["paused"]
    assert cli(["loop", "run", *base, "--target", "shopapp:answer"]) == 0
    assert "cycle: paused" in capsys.readouterr().out
    assert cli(["loop", "resume", *base]) == 0 and not sia.project_settings()["loop"]["paused"]


def test_cli_loop_run_and_history_for_a_hosted_agent(agent, monkeypatch, capsys, tmp_path):
    dev, pid, _ = agent
    dev.update_project(LOCAL_ORG, pid, {"loop": {"candidates_per_cycle": 9}})  # try every operator: deterministic
    base = ["--project", "returns-agent", "--local", dev.path]
    assert cli(["loop", "run", *base]) == 0 and "cycle: canary_started" in capsys.readouterr().out
    assert cli(["loop", "history", *base]) == 0 and "canary_started" in capsys.readouterr().out
    assert cli(["canary", "stop", *base, "--name", "agent_config", "--reason", "manual"]) == 0
    assert dev.canary(LOCAL_ORG, pid, config.CONFIG_NAME) is None


# --- the dashboard (a Playwright smoke test against a real server) ----------------------------------------------
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_dashboard_loop_panel_smoke(agent, monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    import uvicorn
    dev, pid, _ = agent
    jobs.enqueue(dev, LOCAL_ORG, pid, "cycle", {"operators": ["rewrite_playbook"]})
    assert jobs.run_one(dev, "test", jobs.default_handlers())["result"]["outcome"] == "canary_started"
    dev.add_lesson(LOCAL_ORG, pid, "State the return window before deciding.", "reflection")
    monkeypatch.setenv("SIA_LOCAL_MODE", "1")
    monkeypatch.setenv("SIA_DB", dev.path)
    import sia.api
    api = importlib.reload(sia.api)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(channel="chrome", headless=True)
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"no Chrome to drive: {e}")
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/dashboard")
            page.wait_for_selector("#proj")
            page.select_option("#proj", pid)
            page.goto(f"http://127.0.0.1:{port}/dashboard#/loop")
            panel = page.wait_for_selector("[data-panel=loop]")
            assert "running" in panel.inner_text().lower() and page.input_value("#aut") == "2"
            assert "canary_started" in page.inner_text("main").lower() and "in probation" in page.inner_text(
                "main").lower()
            assert "State the return window" in page.inner_text("main")
            page.click("text=Pause the loop")
            page.click("dialog button.danger-fill")
            page.wait_for_selector("[data-panel=loop] >> text=paused")
            assert dev.project(LOCAL_ORG, pid)["settings"]["loop"]["paused"]
            page.click("text=Veto")
            page.wait_for_selector("text=Restore")
            assert dev.lessons(LOCAL_ORG, pid, "retired")
            page.goto(f"http://127.0.0.1:{port}/dashboard#/lab/agent_config/2")
            page.wait_for_selector("text=Changes from @1")
            assert "playbook: +3 bullets" in page.inner_text("main")
            assert not errors, errors
            browser.close()
    finally:
        server.should_exit = True
        t.join(5)


def test_a_provider_outage_stops_the_cycle_without_a_decision(agent, monkeypatch):
    dev, pid, _ = agent
    from sia.llm import Unavailable

    class Outage(loop_demo.OfflineModel):
        def json(self, system, prompt, temperature=0.2):
            if "Members get 60 days" in system:  # the moment the winning candidate is being evaluated
                raise Unavailable("gemini error 429: quota exceeded")
            return super().json(system, prompt, temperature)
    monkeypatch.setattr(models, "factory", Outage)
    j = jobs.enqueue(dev, LOCAL_ORG, pid, "cycle", {"operators": ["rewrite_playbook"]})
    done = jobs.run_one(dev, "test", jobs.default_handlers())
    assert done["id"] == j["id"] and done["status"] == "failed" and "quota" in done["error"]
    # an outage is not a score of 0: nothing was decided on half-measured evidence
    v = dev.version(LOCAL_ORG, pid, config.CONFIG_NAME, 2)
    assert v["status"] == "rejected" and "stopped before a decision" in v["decision_reason"]
    assert dev.active_version(LOCAL_ORG, pid, config.CONFIG_NAME)["version"] == 1
    assert dev.canary(LOCAL_ORG, pid, config.CONFIG_NAME) is None
