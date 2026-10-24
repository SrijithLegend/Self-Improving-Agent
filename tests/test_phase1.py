"""Phase 1 of SELF_IMPROVING_SPEC.md: the jobs queue and worker, dataset splits and pending examples, outcomes."""
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sia_devtools import SIAClient, ValidationError, exact_match  # noqa: E402
from sia_devtools.cli import main as cli  # noqa: E402
from sia_devtools.errors import Conflict  # noqa: E402
from sia_devtools.store import LOCAL_ORG, DevStore, split_for  # noqa: E402
from sia_loop import jobs  # noqa: E402
from test_devtools import hosted, make_api, org  # noqa: E402


@pytest.fixture
def sia(tmp_path):
    return SIAClient(project="p", local=str(tmp_path / "l.db"))


def cases(n):
    return [{"input": f"question {i}", "expected_output": f"answer {i}"} for i in range(n)]


# --- jobs -----------------------------------------------------------------------------------------------------------
def test_job_claim_is_exclusive_under_4_threads(sia):
    dev = sia._store
    for _ in range(60):
        jobs.enqueue(dev, LOCAL_ORG, sia.project_id, "noop")
    claimed, lock = [], threading.Lock()

    def grab(i):
        while job := jobs.claim(dev, f"w{i}"):
            with lock:
                claimed.append((job["id"], job["locked_by"]))

    threads = [threading.Thread(target=grab, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 60 and len({j for j, _ in claimed}) == 60  # every job once, none twice
    assert jobs.claim(dev, "late") is None


def test_stale_heartbeat_requeues_then_fails(sia):
    dev, pid = sia._store, sia.project_id
    job = jobs.enqueue(dev, LOCAL_ORG, pid, "noop")
    now = time.time()
    for attempt in range(1, jobs.MAX_ATTEMPTS + 1):
        got = jobs.claim(dev, "crashy", now=now)
        assert got["id"] == job["id"] and got["attempts"] == attempt
        assert jobs.requeue_stale(dev, now=now + 10) == (0, 0)  # still heartbeating recently: left alone
        requeued, failed = jobs.requeue_stale(dev, now=now + jobs.STALE_AFTER + 1)
        assert (requeued, failed) == ((1, 0) if attempt < jobs.MAX_ATTEMPTS else (0, 1))
    done = jobs.get(dev, LOCAL_ORG, job["id"])
    assert done["status"] == "failed" and "stopped responding" in done["error"]
    assert not jobs.finish(dev, job["id"], "crashy", "done")  # a requeued or failed job can't be finished late


def test_worker_runs_jobs_records_failures_and_respects_cancel(sia):
    dev, pid = sia._store, sia.project_id
    ok = jobs.enqueue(dev, LOCAL_ORG, pid, "noop")
    boom = jobs.enqueue(dev, LOCAL_ORG, pid, "study")  # no handler for it below
    later = jobs.enqueue(dev, LOCAL_ORG, pid, "noop", run_after=time.time() + 3600)
    gone = jobs.enqueue(dev, LOCAL_ORG, pid, "noop")
    jobs.cancel(dev, LOCAL_ORG, gone["id"])
    with pytest.raises(Conflict):
        jobs.cancel(dev, LOCAL_ORG, gone["id"])
    budget = {"noop": lambda d, j: {"_status": "skipped_budget", "estimate": 9}}
    assert jobs.work(dev, budget, once=True) == 2
    got = {j["id"]: j for j in jobs.jobs(dev, LOCAL_ORG, pid)}
    assert got[ok["id"]]["status"] == "skipped_budget" and got[ok["id"]]["result"] == {"estimate": 9}
    assert got[boom["id"]]["status"] == "failed" and "no handler" in got[boom["id"]]["error"]
    assert got[later["id"]]["status"] == "queued" and got[gone["id"]]["status"] == "cancelled"
    assert jobs.enqueue(dev, LOCAL_ORG, pid, "noop", unique=True)["id"] == later["id"]  # one queued at a time


def test_sia_worker_once_runs_a_noop_job(tmp_path, capsys):
    db = str(tmp_path / "w.db")
    dev = DevStore(db)
    pid = dev.create_project(LOCAL_ORG, "p")["id"]
    job = jobs.enqueue(dev, LOCAL_ORG, pid, "noop")
    assert cli(["worker", "--once", "--db", db]) == 0
    assert "ran 1 job" in capsys.readouterr().out
    assert jobs.get(dev, LOCAL_ORG, job["id"])["status"] == "done"


def test_jobs_api_is_scoped_to_the_org(tmp_path, monkeypatch):
    api, c = make_api(tmp_path, monkeypatch)
    h1, h2 = ({"Authorization": f"Bearer {org(c)['api_key']}"} for _ in range(2))
    pid = c.post("/v1/projects", json={"name": "p"}, headers=h1).json()["id"]
    job = jobs.enqueue(api.dev, api.store.tenant_by_key(h1["Authorization"][7:])["id"], pid, "noop")
    assert c.get(f"/v1/projects/{pid}/jobs", headers=h1).json()[0]["id"] == job["id"]
    assert c.get(f"/v1/jobs/{job['id']}", headers=h2).status_code == 404
    assert c.post(f"/v1/jobs/{job['id']}/cancel", headers=h2).status_code == 404
    assert c.post(f"/v1/jobs/{job['id']}/cancel", headers=h1).json()["status"] == "cancelled"


# --- dataset splits -----------------------------------------------------------------------------------------------
def test_split_is_deterministic_and_survives_edits(sia):
    ds = sia.dataset("d")
    ids = ds.add_examples(cases(60))
    rows = {e["id"]: e for e in ds.examples()}
    assert all(rows[i]["split"] == split_for(ds.id, i, 30) for i in ids)
    holdout = [i for i in ids if rows[i]["split"] == "holdout"]
    assert 5 < len(holdout) < 35  # about 30% of 60
    # an edit is a new row, but keeps the split of the example it replaces, however many times it's edited
    for eid in (holdout[0], next(i for i in ids if rows[i]["split"] == "dev")):
        new = ds.update_example(eid, expected_output="changed")
        newer = ds.update_example(new, expected_output="changed again")
        assert {e["id"]: e["split"] for e in ds.examples()}[newer] == rows[eid]["split"]
    # changing holdout_pct later never moves existing examples
    sia.update_settings(loop={"holdout_pct": 50})
    assert {e["id"]: e["split"] for e in ds.examples() if e["id"] in ids} == {
        i: rows[i]["split"] for i in ids if i in {e["id"] for e in ds.examples()}}


def test_holdout_never_in_dev_snapshot_and_evaluations_are_per_split(sia):
    ds = sia.dataset("d")
    ds.add_examples(cases(40))
    dev, hold = ds.examples(split="dev"), ds.examples(split="holdout")
    assert dev and hold and not {e["id"] for e in dev} & {e["id"] for e in hold}
    assert {e["split"] for e in dev} == {"dev"} and {e["split"] for e in hold} == {"holdout"}
    assert len(dev) + len(hold) == len(ds.examples())
    with pytest.raises(ValidationError):
        ds.examples(split="test")
    seen = []
    ev = sia.evaluate(ds, lambda q: seen.append(q) or q.replace("question", "answer"), [exact_match], split="dev")
    assert ev["summary"]["n"] == len(dev) and set(seen) == {e["input"] for e in dev}
    hold_ev = sia.evaluate(ds, lambda q: "x", [exact_match], split="holdout")
    with pytest.raises(ValidationError, match="splits"):
        sia.compare(ev, hold_ev)  # a dev result can never stand in for a holdout one
    assert sia.compare(ev, sia.evaluate(ds, lambda q: "x", [exact_match], split="dev"))["verdict"] == "regressed"


def test_pending_examples_are_excluded_until_verified(sia):
    ds = sia.dataset("d")
    ds.add_examples(cases(3))
    out = sia._store.add_examples(LOCAL_ORG, sia.project_id, ds.id, [{"input": "synthetic q", "expected_output": "a"}],
                                  status="pending", split="dev", source="synthetic")
    pid_ = out["ids"][0]
    assert pid_ not in {e["id"] for e in ds.examples()}
    listed = {e["id"]: e for e in ds.snapshot(include_pending=True)["examples"]}
    assert listed[pid_]["status"] == "pending" and listed[pid_]["split"] == "dev"
    assert sia.evaluate(ds, lambda q: q, [exact_match])["summary"]["n"] == 3
    before = ds.refresh().version
    new = ds.review_example(pid_, "verified")
    assert new != pid_ and {e["id"]: e for e in ds.examples()}[new]["status"] == "verified"
    assert len(ds.examples(version=before)) == 3  # the snapshot from before verification is unchanged
    with pytest.raises(Exception):
        ds.review_example(new, "verified")  # already verified
    other = sia._store.add_examples(LOCAL_ORG, sia.project_id, ds.id, [{"input": "bad", "expected_output": "b"}],
                                    status="pending")["ids"][0]
    assert ds.review_example(other, "rejected") is None
    assert other not in {e["id"] for e in ds.snapshot(include_pending=True)["examples"]}


def test_old_databases_get_their_splits_backfilled_once(tmp_path):
    from sia_devtools.store import MIGRATIONS, migrate
    db = str(tmp_path / "old.db")
    migrate(db, MIGRATIONS[:1])  # devtools-001 only: a database from before splits
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO projects VALUES ('p', 'local', 'p', '{}', 0)")
        c.execute("INSERT INTO datasets VALUES ('d', 'p', 'd', 2, 0)")
        c.executemany("INSERT INTO dataset_examples (id, dataset_id, input, expected, added_in, removed_in, replaces) "
                      "VALUES (?,?,?,?,?,?,?)", [(i, "d", f'"q{i}"', '"a"', 1, None, None) for i in range(1, 41)]
                      + [(41, "d", '"q1 edited"', '"a"', 2, None, 1)])
    dev = DevStore(db)
    split = {e["id"]: e["split"] for e in dev.dataset(LOCAL_ORG, "p", "d")["examples"]}
    assert all(split[i] == split_for("d", i, 30) for i in range(2, 41))
    assert split[41] == split_for("d", 1, 30)  # the edit follows its root
    with sqlite3.connect(db) as c:
        c.execute("UPDATE dataset_examples SET split='dev'")
    DevStore(db)  # the backfill ran once and never runs again
    assert {e["split"] for e in DevStore(db).dataset(LOCAL_ORG, "p", "d")["examples"]} == {"dev"}


# --- outcomes -------------------------------------------------------------------------------------------------------
def test_outcomes_are_recorded_and_auto_verify_only_into_dev(sia):
    with sia.start_run("answer", "Refund order 7?") as run:
        run.log_output("DECISION: YES")
    assert sia.outcome(run, False, source="refunds-db")["example_id"] is None
    got = sia.get_run(run.id)
    assert got["scores"]["outcome"] == 0 and got["metadata"]["outcome_source"] == "refunds-db"
    sia.update_settings(loop={"auto_verify_outcomes": True})
    out = sia.outcome(run, True, source="refunds-db")
    eval_ds = sia.dataset("eval")
    ex = {e["id"]: e for e in eval_ds.examples()}[out["example_id"]]
    assert ex["split"] == "dev" and ex["expected_output"] == "DECISION: YES" and ex["source"] == "outcome"
    assert sia.outcome(run, True, source="refunds-db")["example_id"] is None  # once per run
    with pytest.raises(ValidationError):
        sia.outcome(run, "yes", source="x")


def test_outcome_route_accepts_ingest_keys(tmp_path, monkeypatch):
    api, c = make_api(tmp_path, monkeypatch)
    full = org(c)["api_key"]
    sia = hosted(api, full)
    with sia.start_run("answer", "q") as run:
        run.log_output("a")
    sia.flush()
    ingest = c.post("/v1/keys", json={"name": "app", "scope": "ingest"},
                    headers={"Authorization": f"Bearer {full}"}).json()["api_key"]
    r = c.post(f"/v1/projects/{sia.project_id}/runs/{run.id}/outcome", json={"ok": True, "source": "billing"},
               headers={"Authorization": f"Bearer {ingest}"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert c.post(f"/v1/projects/{sia.project_id}/runs/nope/outcome", json={"ok": True, "source": "b"},
                  headers={"Authorization": f"Bearer {ingest}"}).status_code == 404


def test_loop_settings_are_validated(sia):
    s = sia.project_settings()
    assert s["loop"]["autonomy"] == 0 and s["kind"] == "app"  # an upgraded project behaves exactly as before
    for bad in ({"autonomy": 4}, {"holdout_pct": 10}, {"canary_pct": 51}, {"max_calls_per_cycle": 5},
                {"every_hours": 200}, {"allowed_tools": ["rm_rf"]}, {"webhook_url": "http://x.example"},
                {"judge": {"provider": "anthropic"}}, {"paused": "yes"}, {"unknown": 1}):
        with pytest.raises(ValidationError):
            sia.update_settings(loop=bad)
    s = sia.update_settings(loop={"autonomy": 2, "webhook_url": "https://hooks.example/sia"})
    assert s["loop"]["autonomy"] == 2 and s["loop"]["holdout_pct"] == 30  # other keys keep their values
