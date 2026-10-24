"""Operator commands for a hosted server: backup, restore, maintenance, doctor, logcheck. Called by the `sia` CLI (sia_devtools/cli.py).

Each returns an exit code and prints what it did; errors raise OpsError (exit 4).
"""
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from collections import Counter
from contextlib import closing
from pathlib import Path

import httpx

BACKUP_NAME = re.compile(r"^sia-\d{8}-\d{6}\.db$")


class OpsError(Exception):
    pass


def integrity_check(path):
    """'ok', or SQLite's description of what is wrong."""
    try:
        with closing(sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)) as c:
            return "; ".join(r[0] for r in c.execute("PRAGMA integrity_check"))
    except sqlite3.DatabaseError as e:
        return str(e)


def copy_db(src, dest, standalone=False):
    """sqlite3's online backup: a consistent copy even while the server is writing (WAL mode). standalone switches
    the copy out of WAL mode, so it is one self-contained file: opening a WAL copy read-only (the integrity check)
    leaves -wal and -shm files behind that nothing ever cleans up."""
    with closing(sqlite3.connect(src, timeout=30)) as s, closing(sqlite3.connect(dest)) as d:
        s.backup(d)
        if standalone:
            d.execute("PRAGMA journal_mode=DELETE")


def remove_db(path):
    """Delete a database file and any -wal / -shm side files next to it."""
    for p in (Path(path), Path(f"{path}-wal"), Path(f"{path}-shm")):
        p.unlink(missing_ok=True)


def backups(out_dir):
    """Backup files in out_dir, oldest first (the timestamped names sort by time)."""
    return sorted(p for p in Path(out_dir).glob("sia-*.db") if BACKUP_NAME.match(p.name))


def backup(db, out_dir="backups", keep=14):
    if keep < 1:
        raise OpsError("--keep must be at least 1")
    if not Path(db).is_file():
        raise OpsError(f"database {db} not found")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / time.strftime("sia-%Y%m%d-%H%M%S.db", time.gmtime())
    if dest.exists():
        raise OpsError(f"{dest} already exists; wait a second and run it again")
    try:
        copy_db(db, dest, standalone=True)
    except sqlite3.Error as e:
        remove_db(dest)
        raise OpsError(f"backup failed: {e}")
    if (check := integrity_check(dest)) != "ok":
        remove_db(dest)  # never let a bad copy count as the newest backup, or push a good one out of retention
        raise OpsError(f"integrity check failed on the copy, so it was deleted: {check}")
    old = backups(out)[:-keep]
    for p in old:
        remove_db(p)  # side files too, including ones left by backups made before they were standalone
    print(f"backup {dest} ({dest.stat().st_size:,} bytes, integrity ok); {len(old)} old backup(s) deleted, "
          f"{min(keep, len(backups(out)))} kept")
    return 0


def server_reachable(base_url):
    try:
        httpx.get(base_url.rstrip("/") + "/health", timeout=3)
        return True
    except httpx.HTTPError:
        return False


def restore(db, file, base_url):
    """Replace the live database with a backup. The server must be stopped: it would keep serving from memory and
    write over the restored file."""
    if server_reachable(base_url):
        raise OpsError(f"a server answers at {base_url}: stop it first (restore never runs under a live server)")
    if (check := integrity_check(file)) != "ok":
        raise OpsError(f"{file} failed its integrity check, nothing was changed: {check}")
    safety = None
    if Path(db).is_file():
        safety = Path(db).with_name(time.strftime(f"{Path(db).stem}-before-restore-%Y%m%d-%H%M%S.db", time.gmtime()))
        copy_db(db, safety, standalone=True)
    copy_db(file, db)  # page-by-page copy into the existing file, so its -wal/-shm stay consistent
    print(f"restored {db} from {file}" + (f"; the previous database is saved as {safety}" if safety else ""))
    return 0


REJECTED_REQUEST_DAYS, UNPAID_ORDER_DAYS = 90, 30


def maintenance(db, now=None):
    """Delete what has outlived its retention, even in idle projects (ingest applies run retention only when new
    runs arrive). Idempotent. Prints one JSON line with the counts."""
    from sia_devtools.store import DevStore

    from .store import PLANS, Store
    now = now or time.time()
    st = Store(db)
    DevStore(db)  # opening both applies their migrations, so every table below exists
    plans = {r["id"]: r["plan"] for r in st.q(f"SELECT t.id, {Store.PLAN_SQL} plan FROM tenants t", now)}
    with closing(sqlite3.connect(db, timeout=30)) as c, c:
        runs = 0
        for pid, org, settings in c.execute("SELECT id, org_id, settings FROM projects").fetchall():
            # the shorter of the plan's retention (an expired paid plan counts as free) and the project's own setting
            days = [d for d in ((PLANS.get(plans.get(org)) or {}).get("retention_days"),
                                json.loads(settings or "{}").get("retention_days")) if d]
            if days:
                runs += c.execute("DELETE FROM runs WHERE project_id=? AND started<?",
                                  (pid, now - min(days) * 86400)).rowcount
        requests = c.execute("DELETE FROM access_requests WHERE status='rejected' AND decided<?",
                             (now - REJECTED_REQUEST_DAYS * 86400,)).rowcount
        orders = c.execute("DELETE FROM payments WHERE status='created' AND created<?",
                           (now - UNPAID_ORDER_DAYS * 86400,)).rowcount
    print(json.dumps({"ts": round(now, 3), "event": "maintenance", "runs_deleted": runs,
                      "rejected_access_requests_deleted": requests, "unpaid_orders_deleted": orders}))
    return 0


PLACEHOLDER_PRICES = {"pro": 199900, "team": 799900}  # billing.settings() defaults
BACKUP_MAX_AGE_HOURS = 26  # a daily backup, plus slack


def doctor(db, backup_dir="backups", now=None):
    """Launch readiness. Returns [(level, check, detail)], level PASS / WARN / FAIL."""
    from . import billing
    from . import security as sec
    now, out = now or time.time(), []
    add = lambda level, check, detail: out.append((level, check, detail))  # noqa: E731
    env = os.environ
    local = env.get("SIA_LOCAL_MODE") == "1"

    problem = sec.admin_key_problem(env.get("SIA_ADMIN_KEY", ""))
    add("FAIL" if problem else "PASS", "SIA_ADMIN_KEY", problem or "set and strong")
    hosts = env.get("SIA_ALLOWED_HOSTS", "").strip()
    add("PASS" if hosts else "WARN" if local else "FAIL", "SIA_ALLOWED_HOSTS",
        hosts or "unset: any Host header is accepted" + (" (fine in local mode)" if local else ""))
    proxies = env.get("SIA_TRUSTED_PROXIES", "").strip()
    add("PASS" if proxies else "WARN", "SIA_TRUSTED_PROXIES",
        proxies or "unset: only a proxy on 127.0.0.1 is trusted; behind any other proxy, rate limits see its address")
    contact = env.get("SIA_CONTACT_EMAIL", "").strip()
    add("PASS" if contact else "FAIL", "SIA_CONTACT_EMAIL", contact or "unset: the site's Contact links go to GitHub")

    cfg = billing.settings()
    if not (cfg["key_id"] and cfg["key_secret"]):
        add("FAIL", "Razorpay keys", "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must both be set")
    elif cfg["key_id"].startswith("rzp_test_"):
        add("WARN", "Razorpay keys", "test mode (rzp_test_): fine for the beta, switch to live keys to take payments")
    else:
        add("PASS", "Razorpay keys", "live keys")
    add("PASS" if cfg["webhook_secret"] else "FAIL", "RAZORPAY_WEBHOOK_SECRET",
        "set" if cfg["webhook_secret"] else "unset: the webhook refuses every event")

    add(*_db_check(db))
    if env.get("SIA_DEFAULT_LLM_KEY"):  # one server key paying for every organization's model calls
        orgs = _count_orgs(db)
        add("FAIL" if orgs > 1 else "WARN", "SIA_DEFAULT_LLM_KEY", f"set while {orgs} organizations exist: every "
            "organization's model calls go to your key; unset it and have callers send X-LLM-Key"
            if orgs > 1 else "set: fine for a single-organization self-hosted server")
    recent = [p for p in backups(backup_dir) if now - p.stat().st_mtime < BACKUP_MAX_AGE_HOURS * 3600]         if Path(backup_dir).is_dir() else []
    add("PASS" if recent else "FAIL", "Backups", f"newest: {recent[-1].name}" if recent else
        f"no backup in {backup_dir} from the last {BACKUP_MAX_AGE_HOURS} hours (schedule `sia backup`)")
    placeholder = [k for k, v in PLACEHOLDER_PRICES.items() if cfg["prices"][k] == v]
    add("WARN" if placeholder else "PASS", "Prices", f"still the placeholder defaults: {', '.join(placeholder)} "
        "(set SIA_PRICE_PRO_PAISE / SIA_PRICE_TEAM_PAISE)" if placeholder else
        f"pro {cfg['prices']['pro']}, team {cfg['prices']['team']} ({cfg['currency']}, smallest unit)")
    return out


def _count_orgs(db):
    try:
        with closing(sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True)) as c:
            return c.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
    except sqlite3.Error:
        return 0


def _db_check(db):
    if not Path(db).is_file():
        return "FAIL", "DB", f"{db} not found"
    if (check := integrity_check(db)) != "ok":
        return "FAIL", "DB", f"integrity check failed: {check}"
    try:
        with closing(sqlite3.connect(db, timeout=5, isolation_level=None)) as c:
            c.execute("BEGIN IMMEDIATE")  # takes the write lock without changing anything
            c.execute("ROLLBACK")
    except sqlite3.Error as e:
        return "FAIL", "DB", f"{db} is not writable: {e}"
    return "PASS", "DB", f"{db} writable, integrity ok"


def logcheck(lines, minutes=10, max_error_rate=0.05, min_requests=20, max_security_events=None, now=None):
    """Read the server's JSON log lines (from `docker logs` or a file) and alert when, within the last `minutes`,
    more than max_error_rate of requests were 5xx (once there are at least min_requests), or when there were more
    than max_security_events security events. Prints one JSON line; returns 1 to alert, 0 otherwise."""
    now, since = now or time.time(), (now or time.time()) - minutes * 60
    requests = errors = 0
    events = Counter()
    for line in lines:
        start = line.find("{")  # tolerate prefixes such as docker timestamps
        try:
            d = json.loads(line[start:]) if start >= 0 else None
        except ValueError:
            continue
        if not isinstance(d, dict) or not isinstance(d.get("ts"), (int, float)) or d["ts"] < since:
            continue
        if isinstance(d.get("status"), int):
            requests += 1
            errors += d["status"] >= 500
        elif isinstance(d.get("event"), str):
            events[d["event"]] += 1
    rate = errors / requests if requests else 0.0
    reasons = []
    if requests >= min_requests and rate > max_error_rate:
        reasons.append(f"5xx rate {rate:.1%} over {max_error_rate:.1%}")
    if max_security_events is not None and sum(events.values()) > max_security_events:
        reasons.append(f"{sum(events.values())} security events over {max_security_events}")
    print(json.dumps({"ts": round(now, 3), "event": "logcheck", "minutes": minutes, "requests": requests,
                      "errors_5xx": errors, "error_rate": round(rate, 4), "security_events": dict(events),
                      "alert": bool(reasons), "reasons": reasons}))
    return 1 if reasons else 0


# --- managed agents -> projects (SELF_IMPROVING_SPEC.md section 10) -----------------------------------------------
AGENTS_MIGRATION = "legacy-005-agents-to-projects"


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:56]
    return s or None


def _migrate_agent(c, a, now):
    """Copy one legacy agent into a kind="agent" project with the same id, on connection c. Returns row counts."""
    from sia_devtools.store import dump, split_for, validate_settings
    from sia_loop import config

    from .store import PRIOR, PRIOR_WEIGHT
    n = Counter()
    pid, org = a["id"], a["tenant_id"]
    name = _slug(a["domain"])
    if not name or c.execute("SELECT 1 FROM projects WHERE org_id=? AND name=?", (org, name)).fetchone():
        name = "agent-" + pid[:8]
    settings = validate_settings({"kind": "agent", "agent": {"name": a["name"] or "", "domain": a["domain"] or "",
                                                             "instructions": a["instructions"] or "",
                                                             "rubric": a["rubric"] or ""}})
    c.execute("INSERT INTO projects VALUES (?,?,?,?,?)", (pid, org, name, json.dumps(settings), a["created"] or now))
    n["projects"] += 1

    for d in c.execute("SELECT * FROM docs WHERE agent_id=?", (pid,)).fetchall():
        c.execute("INSERT INTO knowledge_docs VALUES (?,?,?,?,?)", (d["id"], pid, d["title"], d["chunks"], d["created"]))
        n["knowledge_docs"] += 1
    for ch in c.execute("SELECT text, title, doc_id FROM chunks WHERE agent_id=?", (pid,)).fetchall():
        c.execute("INSERT INTO knowledge VALUES (?,?,?,?)", (ch["text"], ch["title"], pid, ch["doc_id"]))
        n["knowledge_chunks"] += 1

    # lessons: active ones in the top 12 by utility were in every prompt, so they become pinned; other active ones
    # wait in probation; inactive ones are retired
    rows = c.execute("SELECT * FROM lessons WHERE agent_id=?", (pid,)).fetchall()
    utility = lambda r: (r["total"] + PRIOR * PRIOR_WEIGHT) / (r["uses"] + PRIOR_WEIGHT)  # noqa: E731
    top = {r["id"] for r in sorted((r for r in rows if r["active"]), key=utility, reverse=True)[:config.MAX_LESSONS]}
    for r in rows:
        state = "retired" if not r["active"] else "pinned" if r["id"] in top else "probation"
        c.execute("UPDATE lessons SET project_id=?, state=? WHERE id=?", (pid, state, r["id"]))
        n[f"lessons_{state}"] += 1

    # playbooks: the active one becomes agent_config@1 (approved and active), the others rejected candidates
    playbooks = c.execute("SELECT * FROM playbooks WHERE agent_id=? ORDER BY active DESC, version",
                          (pid,)).fetchall()
    if not playbooks or not playbooks[0]["active"]:
        playbooks = [None, *playbooks]  # nothing was active: @1 is the agent with no playbook
    for i, pb in enumerate(playbooks, 1):
        cfg = config.default_config(system_prompt=a["instructions"] or "", playbook=pb["content"] if pb else "",
                                    lessons=sorted(top) if i == 1 else [])
        cfg = config.validate(cfg, settings, lesson_ids=top)
        active = i == 1
        c.execute("""INSERT INTO versions (project_id, name, version, content, status, note, created_by, decided_by,
            decision_reason, created, decided) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (pid, config.CONFIG_NAME, i, config.dumps(cfg), "approved" if active else "rejected",
                   pb["note"] if pb else "migrated agent with no active playbook", "migration", "migration",
                   "the agent's active playbook when it was migrated" if active else "inactive playbook when migrated",
                   pb["created"] if pb else now, now))
        if active:
            c.execute("INSERT INTO active_versions (project_id, name, version, activated, activated_by) VALUES "
                      "(?,?,?,?,?)", (pid, config.CONFIG_NAME, 1, now, "migration"))
        n["versions"] += 1

    # examples -> the 'eval' dataset. Synthetic ones were written by the agent itself, so they are no longer trusted:
    # pending, dev split only. Rejected ones are dropped.
    exs = c.execute("SELECT * FROM examples WHERE agent_id=? AND status!='rejected' ORDER BY id", (pid,)).fetchall()
    if exs:
        did = uuid.uuid5(uuid.NAMESPACE_URL, f"sia:{pid}:eval").hex
        c.execute("INSERT INTO datasets VALUES (?,?,?,1,?)", (did, pid, "eval", now))
        for e in exs:
            synthetic = e["source"] == "synthetic"
            status = "pending" if synthetic or e["status"] == "pending" else "verified"
            eid = c.execute("""INSERT INTO dataset_examples (dataset_id, input, expected, metadata, critical, source,
                added_in, created, status) VALUES (?,?,?,?,0,?,1,?,?)""",
                            (did, dump(e["input"]), dump(e["expected"]), dump({"legacy_example_id": e["id"]}),
                             e["source"], e["created"], status)).lastrowid
            c.execute("UPDATE dataset_examples SET split=? WHERE id=?",
                      ("dev" if synthetic else split_for(did, eid, settings["loop"]["holdout_pct"]), eid))
            n[f"examples_{status}"] += 1

    # episodes -> runs (+ feedback rows for ratings and corrections, which stay pending)
    for e in c.execute("SELECT * FROM episodes WHERE agent_id=?", (pid,)).fetchall():
        c.execute("""INSERT INTO runs (project_id, id, name, input, output, status, started, ended, tags, metadata,
            scores) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (pid, e["id"], "agent.run", dump({"task": e["task"]}), dump(e["answer"]), "success", e["created"],
                   e["created"], json.dumps(["migrated"]), dump({"attempts": e["attempts"], "lesson_ids":
                                                                 json.loads(e["lesson_ids"] or "[]")}),
                   json.dumps({"self_critique": e["score"]} if e["score"] is not None else {})))
        n["runs"] += 1
        if e["rating"] is not None or e["correction"]:
            c.execute("""INSERT INTO feedback (project_id, id, run_id, rating, correction, category, created)
                VALUES (?,?,?,?,?,?,?)""", (pid, f"legacy-{e['id']}"[:64], e["id"],
                                            1 if (e["rating"] or 0) >= 4 else -1,
                                            dump(e["correction"]) if e["correction"] else None,
                                            f"rating:{e['rating']}" if e["rating"] is not None else None, now))
            n["feedback"] += 1
    c.execute("INSERT INTO audit (project_id, actor, action, detail, created) VALUES (?,?,?,?,?)",
              (pid, "migration", "agent.migrate", json.dumps(dict(n)), now))
    return n


def migrate_agents(db):
    """Copy every managed agent that isn't a project yet into a kind="agent" project with the same id, all in one
    transaction. Idempotent: a second run finds nothing left to copy. The legacy tables are left as they are (they are
    dropped in a later release, after this has run). Returns the row counts."""
    from sia_devtools.store import DevStore

    from .store import Store
    Store(db)
    DevStore(db)  # both sets of tables exist before copying between them
    total, now = Counter(), time.time()
    with closing(sqlite3.connect(db, timeout=30, isolation_level=None)) as c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN IMMEDIATE")
        try:
            for a in c.execute("SELECT * FROM agents WHERE id NOT IN (SELECT id FROM projects) ORDER BY created")\
                    .fetchall():
                total.update(_migrate_agent(c, a, now))
            c.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?)", (AGENTS_MIGRATION, now))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
    return dict(total)


def auto_migrate_agents(db):
    """Run migrate_agents once, on start, if there are agents and it has never run (spec section 10)."""
    if not Path(db).is_file():
        return None
    with closing(sqlite3.connect(db, timeout=30)) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "agents" not in tables or not c.execute("SELECT 1 FROM agents LIMIT 1").fetchone():
            return None
        if "schema_migrations" in tables and c.execute("SELECT 1 FROM schema_migrations WHERE id=?",
                                                        (AGENTS_MIGRATION,)).fetchone():
            return None
    return migrate_agents(db)


# --- CLI entry points (argparse namespaces from sia_devtools/cli.py) ---------------------------------------------
def migrate_agents_cmd(a):
    counts = migrate_agents(a.db)
    print(json.dumps({"event": "migrate_agents", **counts}) if counts else "nothing to migrate: every agent is "
          "already a project")
    return 0


def backup_cmd(a):
    return backup(a.db, a.dir, a.keep)


def restore_cmd(a):
    # the same port `sia serve` uses when SIA_BASE_URL isn't set
    return restore(a.db, a.file, os.getenv("SIA_BASE_URL") or f"http://127.0.0.1:{os.getenv('PORT', '8000')}")


def maintenance_cmd(a):
    return maintenance(a.db)


def logcheck_cmd(a):
    if a.file == "-":
        return logcheck(sys.stdin, a.minutes, a.max_error_rate, a.min_requests, a.max_security_events)
    with open(a.file, encoding="utf-8", errors="replace") as f:
        return logcheck(f, a.minutes, a.max_error_rate, a.min_requests, a.max_security_events)


def doctor_cmd(a):
    results = doctor(a.db, a.backup_dir)
    for level, check, detail in results:
        print(f"{level:<5} {check:<24} {detail}")
    fails = sum(level == "FAIL" for level, _, _ in results)
    print(f"{fails} FAIL, {sum(level == 'WARN' for level, _, _ in results)} WARN")
    return 1 if fails else 0
