"""Phase 0 of SELF_IMPROVING_SPEC.md: the fixes S1-S4 that the self-improving loop depends on."""
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sia.store import Store  # noqa: E402
from sia_devtools import SIAClient, ValidationError  # noqa: E402
from sia_devtools.store import LOCAL_ORG, DevStore, regex_problem  # noqa: E402
from test_devtools import make_api, org  # noqa: E402

ADMIN_H = {"X-Admin-Key": "admin-key-for-tests-0123456789abcdef"}


# --- S1 ---------------------------------------------------------------------------------------------------------
def test_custom_regex_cannot_stall_writes(tmp_path, monkeypatch):
    evil = "(a+)+$"
    # local mode: the catastrophic pattern is refused up front, so it can never run against stored text
    sia = SIAClient(project="p", local=str(tmp_path / "l.db"))
    t0 = time.perf_counter()
    with pytest.raises(ValidationError, match="exponential"):
        sia.update_settings(redact=["secrets", evil])
    assert time.perf_counter() - t0 < 1
    for bad in ("(a|ab)*c", "((a+)?)+", "(?:x+y?)*z"):
        assert regex_problem(bad), bad
    for ok in (r"\bORD-\d{6}\b", "[a-z]+@corp", "(?:foo|bar)", r"(ab)+", r"[\w.]+"):
        assert regex_problem(ok) is None, ok

    # a safe custom pattern still works locally; scanning is capped at the stored size
    sia.update_settings(redact=["secrets", r"ORD-\d{6}"])
    with sia.start_run("r", "order ORD-123456 and " + "a" * 200_000 + "!") as run:
        pass
    stored = sia.get_run(run.id)["input"]
    assert "[REDACTED:custom]" in stored["preview"] if isinstance(stored, dict) else "[REDACTED:custom]" in stored

    # a pattern stored before this fix (bypassing validation) is refused when used, not run: writes stay fast
    st = DevStore(str(tmp_path / "l.db"))
    with sqlite3.connect(st.path) as c:
        c.execute("UPDATE projects SET settings=json_set(settings, '$.redact', json_array(?))", (evil,))
    t0 = time.perf_counter()
    with pytest.raises(ValidationError):
        st.log_runs(LOCAL_ORG, sia.project_id, [{"id": "x", "name": "r", "input": "a" * 40 + "!"}])
    assert time.perf_counter() - t0 < 1
    assert st.project(LOCAL_ORG, sia.project_id)["settings"]["redact"] == [evil]  # still readable, so fixable

    # hosted server: custom patterns are refused outright, named rules are fine
    api, c = make_api(tmp_path, monkeypatch)
    h = {"Authorization": f"Bearer {org(c)['api_key']}"}
    pid = c.post("/v1/projects", json={"name": "p"}, headers=h).json()["id"]
    r = c.patch(f"/v1/projects/{pid}", json={"settings": {"redact": [evil]}}, headers=h)
    assert r.status_code == 400 and "local mode" in r.json()["detail"]
    assert c.patch(f"/v1/projects/{pid}", json={"settings": {"redact": [r"ORD-\d+"]}}, headers=h).status_code == 400
    assert c.patch(f"/v1/projects/{pid}", json={"settings": {"redact": ["secrets", "email"]}},
                   headers=h).status_code == 200


# --- S2 ---------------------------------------------------------------------------------------------------------
def _rows_for(db, org_id, ids):
    """Every row in every table that still points at the organization or one of its ids."""
    found = {}
    with sqlite3.connect(db) as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND sql NOT LIKE "
                                          "'CREATE VIRTUAL%' AND name NOT LIKE '%fts%' AND name NOT LIKE 'chunks%'")]
        tables += [t for t in ("chunks", "lessons_fts", "knowledge") if c.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()]
        for t in tables:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
            for col in cols:
                if col in ("org_id", "tenant_id") or (col == "id" and t == "tenants"):
                    n = c.execute(f"SELECT COUNT(*) FROM {t} WHERE {col}=?", (org_id,)).fetchone()[0]
                elif col in ("project_id", "agent_id", "dataset_id") or (col == "id" and t in ("projects", "agents",
                                                                                             "datasets")):
                    n = c.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} IN ({','.join('?' * len(ids))})",
                                  ids).fetchone()[0]
                else:
                    continue
                if n:
                    found[f"{t}.{col}"] = n
    return found


def test_delete_org_leaves_no_rows(tmp_path, monkeypatch):
    api, c = make_api(tmp_path, monkeypatch)
    a, b = org(c), org(c)
    h = {"Authorization": f"Bearer {a['api_key']}"}
    c.post(f"/v1/admin/orgs/{a['tenant_id']}/plan", json={"plan": "pro"}, headers=ADMIN_H)  # room for 2 projects
    pid = c.post("/v1/projects", json={"name": "p"}, headers=h).json()["id"]
    c.post(f"/v1/projects/{pid}/runs", json={"runs": [{"id": "r1", "name": "x", "input": "q"}]}, headers=h)
    c.post(f"/v1/projects/{pid}/runs/r1/feedback", json={"rating": -1, "correction": "y"}, headers=h)
    did = c.post(f"/v1/projects/{pid}/datasets", json={"name": "d"}, headers=h).json()["id"]
    c.post(f"/v1/projects/{pid}/datasets/{did}/examples", json={"examples": [{"input": "q", "expected_output": "y"}]},
           headers=h)
    c.post(f"/v1/projects/{pid}/versions", json={"content": "v1"}, headers=h)
    aid = c.post("/v1/projects", json={"name": "agent", "settings": {"kind": "agent"}}, headers=h).json()["id"]
    c.post(f"/v1/projects/{aid}/knowledge", json={"title": "t", "content": "facts"}, headers=h)
    c.post(f"/v1/projects/{aid}/tables", json={"name": "t", "rows": [{"a": 1}]}, headers=h)
    api.dev.add_lesson(a["tenant_id"], aid, "Always cite the rule.", "test")
    from sia_loop import jobs, notify
    jobs.enqueue(api.dev, a["tenant_id"], aid, "noop")
    notify.secret(api.dev, a["tenant_id"], aid)
    api.store.add_payment("order_1", a["tenant_id"], "pro", 100, "INR")
    other = {"Authorization": f"Bearer {b['api_key']}"}
    keep = c.post("/v1/projects", json={"name": "p"}, headers=other).json()["id"]

    assert c.delete(f"/v1/admin/orgs/{a['tenant_id']}").status_code == 403
    assert c.delete(f"/v1/admin/orgs/{a['tenant_id']}", headers=ADMIN_H).status_code == 204
    assert c.delete(f"/v1/admin/orgs/{a['tenant_id']}", headers=ADMIN_H).status_code == 404
    assert _rows_for(api.DB, a["tenant_id"], [pid, aid, did]) == {}
    assert c.get("/v1/me", headers=h).status_code == 401
    assert api.store.q("SELECT COUNT(*) n FROM usage WHERE tenant_id='deleted'")[0]["n"] > 0  # kept, anonymised
    assert c.get(f"/v1/projects/{keep}", headers=other).status_code == 200  # the other org is untouched

    # one project on its own
    third = {"Authorization": f"Bearer {org(c)['api_key']}"}
    pid2 = c.post("/v1/projects", json={"name": "q"}, headers=third).json()["id"]
    c.post(f"/v1/projects/{pid2}/runs", json={"runs": [{"id": "r1", "name": "x"}]}, headers=third)
    assert c.delete(f"/v1/projects/{pid2}", headers=other).status_code == 404  # not theirs
    assert c.delete(f"/v1/projects/{pid2}", headers=third).status_code == 204
    assert _rows_for(api.DB, "none", [pid2]) == {}
    assert c.get(f"/v1/projects/{keep}", headers=other).status_code == 200


# --- S3 ---------------------------------------------------------------------------------------------------------
def test_llm_quota_returns_402(tmp_path, monkeypatch):
    from sia_loop import models
    from test_sia import FakeLLM
    api, c = make_api(tmp_path, monkeypatch)
    monkeypatch.setattr(models, "factory", FakeLLM)
    monkeypatch.setitem(api.PLANS, "free", {**api.PLANS["free"], "llm_calls_per_month": 3})
    h = {"Authorization": f"Bearer {org(c)['api_key']}", "X-LLM-Key": "x"}
    aid = c.post("/v1/projects", json={"name": "a", "settings": {"kind": "agent"}}, headers=h).json()["id"]
    first = c.post(f"/v1/projects/{aid}/tasks", json={"task": "q"}, headers=h)
    assert first.status_code == 200 and first.json()["llm_calls"] >= 3
    r = c.post(f"/v1/projects/{aid}/tasks", json={"task": "q"}, headers=h)
    assert r.status_code == 402 and "LLM calls" in r.json()["detail"]


def test_doctor_fails_on_shared_default_llm_key(tmp_path, monkeypatch):
    from sia import ops
    db = str(tmp_path / "d.db")
    st = Store(db)
    st.create_tenant("a")
    monkeypatch.setenv("SIA_DEFAULT_LLM_KEY", "k")
    level = lambda: next(lv for lv, check, _ in ops.doctor(db, str(tmp_path)) if check == "SIA_DEFAULT_LLM_KEY")
    assert level() == "WARN"
    st.create_tenant("b")
    assert level() == "FAIL"


# --- S4 ---------------------------------------------------------------------------------------------------------
def test_concurrent_add_playbook_unique(tmp_path):
    """Playbooks are agent_config versions now: 4 writers x 150 versions must get 600 distinct numbers."""
    dev = DevStore(str(tmp_path / "c.db"))
    pid = dev.create_project(LOCAL_ORG, "p")["id"]
    errors = []

    def work(i):
        try:
            for n in range(150):
                dev.create_version(LOCAL_ORG, pid, f"pb {i}.{n}")
        except Exception as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    versions = [v["version"] for v in dev.versions(LOCAL_ORG, pid)]
    assert not errors and len(versions) == 600 and len(set(versions)) == 600


def test_duplicate_playbook_versions_are_renumbered_by_the_migration(tmp_path):
    db = str(tmp_path / "old.db")
    from sia.store import MIGRATIONS
    from sia_devtools.store import migrate
    migrate(db, MIGRATIONS[:4])  # a database from before the fix
    with sqlite3.connect(db) as c:
        c.executemany("INSERT INTO playbooks (agent_id, version, content, active) VALUES (?,?,?,0)",
                      [("a", 1, "x"), ("a", 1, "y"), ("a", 2, "z"), ("b", 1, "w")])
    Store(db)
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT agent_id, version, content FROM playbooks ORDER BY id").fetchall() == [
            ("a", 1, "x"), ("a", 2, "y"), ("a", 3, "z"), ("b", 1, "w")]
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO playbooks (agent_id, version) VALUES ('a', 1)")
