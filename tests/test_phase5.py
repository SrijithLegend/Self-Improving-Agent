"""Phase 5 of SELF_IMPROVING_SPEC.md: promotion rules, autonomy levels, canaries, the live monitor, the kill switch."""
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

import loop_demo  # noqa: E402
from sia_devtools import SIAClient  # noqa: E402
from sia_devtools.errors import Conflict, ValidationError  # noqa: E402
from sia_devtools.store import LOCAL_ORG, DevStore  # noqa: E402
from sia_loop import config, cycle, jobs, models, monitor, promoter  # noqa: E402
from test_devtools import hosted, make_api, org  # noqa: E402

NAME = config.CONFIG_NAME


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "factory", loop_demo.OfflineModel)
    monkeypatch.setenv("SIA_LLM_KEY_GEMINI", "offline")
    dev = DevStore(str(tmp_path / "loop.db"))
    pid, did = loop_demo.setup(dev)
    return dev, pid, did


@pytest.fixture
def winner(demo):
    """A cycle at autonomy 1 that ends with an improved holdout verdict, waiting for a decision."""
    dev, pid, did = demo
    dev.update_project(LOCAL_ORG, pid, {"loop": {"autonomy": 1}})
    rec = cycle.run_cycle(dev, LOCAL_ORG, pid, payload={"operators": ["rewrite_playbook"]})
    assert rec["outcome"] == "awaiting_approval"
    hold, dev_cmp = (dev.comparison(LOCAL_ORG, pid, rec[k]) for k in ("holdout_comparison", "dev_comparison"))
    dev.update_project(LOCAL_ORG, pid, {"loop": {"autonomy": 2}})
    return dev, pid, rec["best"], hold, dev_cmp


def failed_rules(decision):
    return [r["rule"] for r in decision["rules"] if not r["ok"]]


def test_promoter_refuses_each_of_the_7_rules(winner):
    dev, pid, best, hold, dev_cmp = winner
    s = lambda **loop: dev.update_project(LOCAL_ORG, pid, {"loop": loop})  # noqa: E731
    cfg = config.parse(dev.active_version(LOCAL_ORG, pid, NAME)["content"])
    cases = {
        "holdout improved": lambda: dict(cmp_holdout={**hold, "verdict": "inconclusive"}),
        "no critical regressions": lambda: dict(cmp_dev={**dev_cmp, "critical_regressions": [7]}),
        "valid config and locked text": lambda: (dev.update_project(LOCAL_ORG, pid, {
            "locked_text": [loop_demo.LOCKED, "A new safety rule the candidate doesn't have."]}), {})[1],
        "enough holdout examples": lambda: (s(holdout_min_examples=10_000), {})[1],
        "promotions per day": lambda: (s(max_promotions_per_day=0), {})[1],
        "independent judge": lambda: (s(scorers=["judge"], judge={"provider": "gemini",
                                                                  "model": cfg["model"]["chain"][0]}), {})[1],
        "loop not paused": lambda: (s(paused=True), {})[1],
    }
    base = dict(cmp_holdout=hold, cmp_dev=dev_cmp)
    snapshot = dev.project(LOCAL_ORG, pid)["settings"]
    for rule, setup in cases.items():
        args = {**base, **setup()}
        d = promoter.decide(dev, LOCAL_ORG, pid, best, args["cmp_holdout"], args["cmp_dev"]) if rule != \
            "holdout improved" else {"rules": [{"rule": r, "ok": ok} for r, ok, _ in promoter.rules(
                dev, LOCAL_ORG, pid, best, args["cmp_holdout"], args["cmp_dev"])]}
        assert failed_rules(d) == [rule], (rule, failed_rules(d))
        assert dev.version(LOCAL_ORG, pid, NAME, best)["status"] == "candidate", rule  # nothing was approved
        assert dev.canary(LOCAL_ORG, pid, NAME) is None
        dev.update_project(LOCAL_ORG, pid, {k: snapshot[k] for k in ("locked_text", "loop")})
    # a non-improved holdout verdict is a rejection, not a wait
    other = dev.create_version(LOCAL_ORG, pid, config.dumps({**cfg, "playbook": f"- {loop_demo.LOCKED}\n- x"}),
                               name=NAME)["version"]
    assert promoter.decide(dev, LOCAL_ORG, pid, other, {**hold, "verdict": "no_improvement"})["status"] == "rejected"
    # with every rule holding, autonomy 2 approves and starts a canary (and schedules the monitor)
    d = promoter.decide(dev, LOCAL_ORG, pid, best, hold, dev_cmp)
    assert d["outcome"] == "canary_started" and failed_rules(d) == []
    assert dev.version(LOCAL_ORG, pid, NAME, best)["status"] == "approved"
    assert dev.canary(LOCAL_ORG, pid, NAME)["pct"] == 10
    assert [j["kind"] for j in jobs.jobs(dev, LOCAL_ORG, pid)] == ["monitor"]
    assert dev.active_version(LOCAL_ORG, pid, NAME)["version"] == 1  # production hasn't moved


def test_canary_is_sticky_per_unit(tmp_path):
    sia = SIAClient(project="app", local=str(tmp_path / "c.db"))
    v1, v2 = (sia.create_version(text)["version"] for text in ("Be helpful.", "Be helpful and brief."))
    sia.approve(v1)
    sia.activate(v1)
    sia.approve(v2, override_reason="test")
    with pytest.raises(ValidationError):
        sia.start_canary(v1, 10)  # the active version can't be its own canary
    sia.start_canary(v2, 30)
    picks = {u: sia.get_prompt(unit=u, ttl=0).version for u in range(1000)}
    assert all(sia.get_prompt(unit=u, ttl=0).version == picks[u] for u in range(0, 1000, 7))  # the same every time
    share = Counter(picks.values())[v2] / 1000
    assert 0.25 < share < 0.35
    assert sia.get_prompt(ttl=0).version == v1  # without a unit, behaviour is unchanged
    with sia.start_run("answer", "q", version=sia.get_prompt(unit=next(u for u, v in picks.items() if v == v2),
                                                             ttl=0)) as run:
        pass
    assert sia.get_run(run.id)["version"] == v2  # the run records which variant served it
    sia.promote_canary()
    assert sia.get_prompt(unit=1, ttl=0).version == v2 and sia.versions()[0]["active"]


def test_hosted_canary_routes(tmp_path, monkeypatch):
    api, c = make_api(tmp_path, monkeypatch)
    key = org(c)["api_key"]
    sia = hosted(api, key)
    v1, v2 = (sia.create_version(t)["version"] for t in ("A", "B"))
    sia.approve(v1)
    sia.activate(v1)
    sia.approve(v2, override_reason="test")
    h = {"Authorization": f"Bearer {key}"}
    base = f"/v1/projects/{sia.project_id}/versions"
    assert c.post(f"{base}/system_prompt/{v2}/canary", json={"pct": 51}, headers=h).status_code == 422
    assert c.post(f"{base}/system_prompt/{v2}/canary", json={"pct": 50}, headers=h).json()["pct"] == 50
    got = {c.get(f"{base}/active", params={"unit": f"user{i}"}, headers=h).json()["variant"] for i in range(40)}
    assert got == {"active", "canary"}
    assert c.post(f"{base}/system_prompt/canary/stop", json={"reason": "enough"}, headers=h).json()["version"] == v2
    assert c.post(f"{base}/system_prompt/canary/promote", headers=h).status_code == 404
    assert c.get(f"{base}/active", params={"unit": "user1"}, headers=h).json()["variant"] == "active"


def runs(version, n, *, errors=0, outcome_fails=None, started=None):
    started = started or time.time()
    return [{"id": f"v{version}-{started}-{i}", "name": "agent.run", "version_name": NAME, "version": version,
             "started": started + i * 0.001, "status": "error" if i < errors else "success",
             "scores": {} if outcome_fails is None else {"outcome": 0 if i < outcome_fails else 1}}
            for i in range(n)]


def test_two_proportion_rollback_on_injected_errors(winner):
    dev, pid, best, hold, dev_cmp = winner
    dev.update_project(LOCAL_ORG, pid, {"loop": {"canary_min_runs": 50, "canary_min_hours": 0}})
    promoter.decide(dev, LOCAL_ORG, pid, best, hold, dev_cmp)
    dev.log_runs(LOCAL_ORG, pid, runs(1, 60, errors=1) + runs(best, 40, errors=6))
    assert [a["action"] for a in monitor.check(dev, LOCAL_ORG, pid)] == ["waiting"]  # 40 < canary_min_runs
    dev.log_runs(LOCAL_ORG, pid, runs(best, 30, errors=5, started=time.time() + 1))
    acts = monitor.check(dev, LOCAL_ORG, pid)
    assert acts[0]["action"] == "rolled_back" and "error rate: 11/70 vs 1/60" in acts[0]["reason"]
    assert dev.canary(LOCAL_ORG, pid, NAME) is None and dev.active_version(LOCAL_ORG, pid, NAME)["version"] == 1
    v = dev.version(LOCAL_ORG, pid, NAME, best)
    assert v["status"] == "rejected" and "z =" in v["decision_reason"]
    assert any(a["action"] == "version.revoke" and a["actor"] == "sia-loop" for a in dev.audit(LOCAL_ORG, pid))
    assert monitor.z_worse(5, 100, 6, 100) < monitor.Z_MAX  # noise is not a regression


def test_level_2_waits_for_a_person_and_level_3_promotes(winner):
    dev, pid, best, hold, dev_cmp = winner
    dev.update_project(LOCAL_ORG, pid, {"loop": {"canary_min_runs": 30, "canary_min_hours": 0}})
    promoter.decide(dev, LOCAL_ORG, pid, best, hold, dev_cmp)
    dev.log_runs(LOCAL_ORG, pid, runs(1, 40, outcome_fails=10) + runs(best, 40, outcome_fails=2))
    assert monitor.check(dev, LOCAL_ORG, pid)[0]["action"] == "ready"  # autonomy 2: a person promotes
    assert dev.active_version(LOCAL_ORG, pid, NAME)["version"] == 1
    dev.update_project(LOCAL_ORG, pid, {"loop": {"autonomy": 3}})
    assert monitor.check(dev, LOCAL_ORG, pid)[0]["action"] == "promoted"
    assert dev.active_version(LOCAL_ORG, pid, NAME)["version"] == best

    # the 48-hour watch: the new version fails live, so the previous one comes back
    dev.log_runs(LOCAL_ORG, pid, runs(best, 40, errors=12, started=time.time() + 5))
    acts = monitor.check(dev, LOCAL_ORG, pid)
    assert acts[-1]["action"] == "rolled_back_after_promotion" and acts[-1]["to"] == 1
    assert dev.active_version(LOCAL_ORG, pid, NAME)["version"] == 1


def test_autonomy_3_needs_a_real_exam(tmp_path):
    sia = SIAClient(project="p", local=str(tmp_path / "a.db"))
    assert sia.project_settings()["loop"]["autonomy"] == 1  # a new app project proposes; a person approves
    ds = sia.dataset("eval")
    ds.add_examples([{"input": "q", "expected_output": "a"}])
    with pytest.raises(ValidationError, match="critical"):
        sia.update_settings(loop={"autonomy": 3})
    ds.add_example("q2", "b", critical=True)
    with pytest.raises(ValidationError, match="holdout_min_examples"):
        sia.update_settings(loop={"autonomy": 3, "holdout_min_examples": 10})
    assert sia.update_settings(loop={"autonomy": 3})["loop"]["autonomy"] == 3
    assert sia.update_settings(loop={"autonomy": 0})["loop"]["autonomy"] == 0  # lowering is always allowed
    assert any(a["action"] == "loop.autonomy" for a in sia.audit_log())


def test_pause_stops_the_canary_and_enqueues_nothing(winner):
    dev, pid, best, hold, dev_cmp = winner
    promoter.decide(dev, LOCAL_ORG, pid, best, hold, dev_cmp)
    queued = jobs.enqueue(dev, LOCAL_ORG, pid, "cycle")
    dev.update_project(LOCAL_ORG, pid, {"loop": {"paused": True}})
    assert dev.canary(LOCAL_ORG, pid, NAME) is None
    assert dev.active_version(LOCAL_ORG, pid, NAME)["version"] == 1  # the active version stays exactly as it was
    assert {j["status"] for j in jobs.jobs(dev, LOCAL_ORG, pid)} == {"cancelled"}
    assert jobs.get(dev, LOCAL_ORG, queued["id"])["status"] == "cancelled"
    for kind in ("cycle", "monitor"):
        with pytest.raises(Conflict):
            jobs.enqueue(dev, LOCAL_ORG, pid, kind)
    assert cycle.run_cycle(dev, LOCAL_ORG, pid)["outcome"] == "paused"
    assert monitor.check(dev, LOCAL_ORG, pid) == [{"action": "paused"}]
    paused = [a for a in dev.audit(LOCAL_ORG, pid) if a["action"] == "loop.paused"]
    assert paused and paused[0]["detail"]["canaries_stopped"] == [NAME]
    dev.update_project(LOCAL_ORG, pid, {"loop": {"paused": False}})
    assert jobs.enqueue(dev, LOCAL_ORG, pid, "cycle")["status"] == "queued"


def test_pause_route_is_the_kill_switch(tmp_path, monkeypatch):
    api, c = make_api(tmp_path, monkeypatch)
    h = {"Authorization": f"Bearer {org(c)['api_key']}"}
    pid = c.post("/v1/projects", json={"name": "bot", "settings": {"kind": "agent"}}, headers=h).json()["id"]
    assert c.get(f"/v1/projects/{pid}/loop", headers=h).json()["autonomy"] == 2  # new hosted agents: canary level
    assert c.post(f"/v1/projects/{pid}/loop/pause", headers=h).json()["paused"] is True
    assert c.post(f"/v1/projects/{pid}/loop/run", headers=h).status_code == 409
    assert c.post(f"/v1/projects/{pid}/loop/resume", headers=h).json()["paused"] is False


def test_loop_demo_offline_full_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(models, "factory", loop_demo.OfflineModel)
    monkeypatch.setenv("SIA_LLM_KEY_GEMINI", "offline")
    out = loop_demo.main(["--offline", "--db", str(tmp_path / "demo.db")])
    dev, pid = out["dev"], out["pid"]
    assert len(out["cycles"]) <= 3                                          # promoted within 3 cycles
    good = next(c for c in out["cycles"][0]["candidates"] if c["operator"] == "rewrite_playbook")["version"]
    assert out["promoted"] == good and "Members get 60 days" in config.parse(
        dev.active_version(LOCAL_ORG, pid, NAME)["content"])["playbook"]
    bad = next(c for c in out["cycles"][0]["candidates"] if c["operator"] == "edit_system_prompt")
    assert bad["status"] == "rejected"                                      # the bad config is rejected
    last = out["monitor"][-1][0]
    assert last["action"] == "rolled_back" and "negative outcomes" in last["reason"]  # the live regression too
    assert dev.version(LOCAL_ORG, pid, NAME, out["injected"])["status"] == "rejected"
    assert out["final_active"] == good
    assert "rolled_back" in capsys.readouterr().out
