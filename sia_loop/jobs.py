"""Jobs: a durable queue in the jobs table, run by `sia worker`. Long work (an improvement cycle, a canary check, an
evaluation, a study round) runs here instead of holding an HTTP request open for minutes.

A worker claims one job at a time in a write transaction, so two workers never run the same job. While it runs, the
worker heartbeats every 30 s; a job whose heartbeat is older than 5 minutes goes back to the queue (a crashed
worker), and fails after 3 attempts.
"""
import json
import os
import socket
import threading
import time
import uuid

from sia_devtools.errors import Conflict, NotFound, ValidationError
from sia_devtools.store import load

KINDS = ("cycle", "monitor", "evaluate", "study", "noop")
STATUSES = ("queued", "running", "done", "failed", "skipped_budget", "cancelled")
STALE_AFTER, HEARTBEAT_EVERY, MAX_ATTEMPTS = 300, 30, 3


def _out(row):
    if row is None:
        return None
    j = dict(row)
    j["payload"], j["result"] = load(j["payload"]), load(j["result"])
    return j


PAUSABLE = ("cycle", "monitor")


def enqueue(dev, org, pid, kind, payload=None, run_after=None, unique=False):
    """Queue a job for a project. unique=True returns the job of this kind already queued or running, if any,
    instead of adding a second one; unique="queued" looks at queued jobs only. While the project's loop is paused,
    no cycle or monitor job is queued (G7)."""
    if kind not in KINDS:
        raise ValidationError(f"job kind must be one of {KINDS}")
    with dev._conn(write=True) as c:
        p = dev._project(c, org, pid)
        if kind in PAUSABLE and p["settings"]["loop"]["paused"]:
            raise Conflict("The loop is paused for this project; resume it first.")
        states = "('queued')" if unique == "queued" else "('queued', 'running')"
        if unique and (row := c.execute(f"SELECT * FROM jobs WHERE project_id=? AND kind=? AND status IN {states} "
                                        "ORDER BY created LIMIT 1", (pid, kind)).fetchone()):
            return _out(row)
        jid, now = uuid.uuid4().hex, time.time()
        c.execute("INSERT INTO jobs (id, project_id, kind, payload, run_after, created) VALUES (?,?,?,?,?,?)",
                  (jid, pid, kind, json.dumps(payload or {}), now if run_after is None else run_after, now))
        return _out(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def claim(dev, worker, now=None):
    """The oldest due job, now marked running by this worker; None if nothing is due."""
    now = time.time() if now is None else now
    with dev._conn(write=True) as c:
        return _out(c.execute("""UPDATE jobs SET status='running', locked_by=?, heartbeat=?, started=?,
            attempts=attempts+1 WHERE id=(SELECT id FROM jobs WHERE status='queued' AND run_after<=?
            ORDER BY created LIMIT 1) RETURNING *""", (worker, now, now, now)).fetchone())


def requeue_stale(dev, now=None):
    """Running jobs whose worker stopped heartbeating go back to the queue, or fail after MAX_ATTEMPTS.
    Returns (requeued, failed)."""
    now = time.time() if now is None else now
    with dev._conn(write=True) as c:
        failed = c.execute("UPDATE jobs SET status='failed', finished=?, error=? WHERE status='running' AND "
                           "heartbeat<? AND attempts>=?", (now, f"the worker stopped responding ({MAX_ATTEMPTS} "
                                                               "attempts)", now - STALE_AFTER, MAX_ATTEMPTS)).rowcount
        requeued = c.execute("UPDATE jobs SET status='queued', locked_by=NULL WHERE status='running' AND heartbeat<?",
                             (now - STALE_AFTER,)).rowcount
    return requeued, failed


def heartbeat(dev, jid, worker):
    with dev._conn(write=True) as c:
        return bool(c.execute("UPDATE jobs SET heartbeat=? WHERE id=? AND locked_by=? AND status='running'",
                              (time.time(), jid, worker)).rowcount)


def finish(dev, jid, worker, status, result=None, error=None):
    """Only the worker holding a running job can finish it, so a cancelled or requeued job keeps its state."""
    if status not in STATUSES[2:]:
        raise ValidationError(f"a finished job's status must be one of {STATUSES[2:]}")
    with dev._conn(write=True) as c:
        return bool(c.execute("UPDATE jobs SET status=?, result=?, error=?, finished=? WHERE id=? AND locked_by=? AND "
                              "status='running'", (status, json.dumps(result, default=str) if result is not None
                                                   else None, error, time.time(), jid, worker)).rowcount)


def get(dev, org, jid):
    with dev._conn() as c:
        row = c.execute("SELECT j.* FROM jobs j JOIN projects p ON p.id=j.project_id WHERE j.id=? AND p.org_id=?",
                        (jid, org)).fetchone()
    if not row:
        raise NotFound("Job not found.")
    return _out(row)


def jobs(dev, org, pid, limit=50, kind=None):
    with dev._conn() as c:
        dev._project(c, org, pid)
        return [_out(r) for r in c.execute(
            f"SELECT * FROM jobs WHERE project_id=? {'AND kind=?' if kind else ''} ORDER BY created DESC LIMIT ?",
            (pid, *([kind] if kind else []), min(max(int(limit), 1), 200)))]


def cancel(dev, org, jid):
    """A queued job never starts; a running one keeps running, but its result is discarded."""
    get(dev, org, jid)
    with dev._conn(write=True) as c:
        if not c.execute("UPDATE jobs SET status='cancelled', finished=? WHERE id=? AND status IN ('queued', "
                         "'running')", (time.time(), jid)).rowcount:
            raise Conflict("Only queued or running jobs can be cancelled.")
    return get(dev, org, jid)


# --- the worker ----------------------------------------------------------------------------------------------------
def org_of(dev, pid):
    with dev._conn() as c:
        row = c.execute("SELECT org_id FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        raise NotFound("Project not found.")
    return row[0]


def _metered(dev, org, pid):
    """A budget for one job (the project's per-cycle cap, and the month's quota) and a meter for what it spent."""
    from . import budget
    cap = dev.project(org, pid)["settings"]["loop"]["max_calls_per_cycle"]
    left = budget.quota_left(dev.path, org)
    return budget.Budget(min(cap, left) if left is not None else cap), \
        lambda action, n: n and budget.record_usage(dev.path, org, pid, action, n)


def _cycle(dev, job):
    from . import budget, cycle
    pid = job["project_id"]
    org = org_of(dev, pid)
    return cycle.run_cycle(dev, org, pid, payload=job["payload"], quota=lambda o: budget.quota_left(dev.path, o),
                           on_usage=lambda n: budget.record_usage(dev.path, org, pid, "loop", n))


def _evaluate(dev, job):
    """payload: {"version": N (default: active), "split": "dev"|"holdout"|None, "dataset": "eval", "trials": 1}"""
    from . import config, models
    from .evaluator import Evaluator
    pid, p = job["project_id"], job["payload"]
    org = org_of(dev, pid)
    version = p.get("version") or (dev.active_version(org, pid, config.CONFIG_NAME) or {}).get("version")
    if not version:
        raise ValidationError("no agent_config version to evaluate")
    ds = dev.datasets(org, pid, p.get("dataset", "eval"))
    if not ds:
        raise NotFound(f"No dataset named {p.get('dataset', 'eval')!r}.")
    bud, meter = _metered(dev, org, pid)
    try:
        ev, _ = Evaluator(dev, org, pid, llm_for=lambda cfg: bud.wrap(models.for_config(cfg))).evaluate(
            version, ds[0]["id"], p.get("split"), int(p.get("trials", 1)), "job:evaluate")
    finally:
        meter("evaluate", bud.used)
    return {"evaluation_id": ev["id"], "version": version, "split": p.get("split"), "score": ev["summary"]["score"],
            "n": ev["summary"]["n"], "llm_calls": bud.used}


def _study(dev, job):
    """Self-study on the project's documents: the model writes practice questions with answers from the source. They
    are stored as *pending* examples in the dev split: the agent never writes its own exam (G9, G2)."""
    from . import config, models
    pid = job["project_id"]
    org = org_of(dev, pid)
    p = dev.project(org, pid)
    domain = (p["settings"]["agent"] or {}).get("domain") or "this domain"
    chunks = dev.random_knowledge(org, pid, max(1, min(int(job["payload"].get("rounds", 3)), 20)))
    if not chunks:
        raise ValidationError("No knowledge to study. Add documents first.")
    active = dev.active_version(org, pid, config.CONFIG_NAME)
    if not active:
        raise ValidationError("This agent has no active agent_config yet.")
    bud, meter = _metered(dev, org, pid)
    llm, items = bud.wrap(models.for_config(config.parse(active["content"]))), []
    try:
        for ch in chunks:
            qa = llm.json(f"You write exam questions for practitioners of {domain}.",
                          f"SOURCE ({ch['title']}):\n{ch['text']}\n\nWrite one realistic task or question a user in "
                          "this domain might ask that this source answers, plus the ideal answer based strictly on the "
                          'source. Return {"question": "...", "answer": "..."}', temperature=0.8)
            q, a = (str(qa.get(k, "")).strip() for k in ("question", "answer")) if isinstance(qa, dict) else ("", "")
            if q and a:
                items.append({"input": q[:20000], "expected_output": a[:20000], "metadata": {"from": ch["title"]}})
    finally:
        meter("study", bud.used)
    if not items:
        return {"studied": 0, "example_ids": [], "llm_calls": bud.used}
    ds = dev.create_dataset(org, pid, "eval", actor="sia-loop")
    ids = dev.add_examples(org, pid, ds["id"], items, status="pending", split="dev", source="synthetic")["ids"]
    return {"studied": len(ids), "example_ids": ids, "llm_calls": bud.used}


def default_handlers():
    """Job kind -> fn(dev, job) -> result dict. A result with "_status" finishes the job with that status (for
    example skipped_budget)."""
    from . import monitor
    return {"noop": lambda dev, job: {"ok": True}, "cycle": _cycle, "evaluate": _evaluate, "study": _study,
            "monitor": monitor.handler}


def run_one(dev, worker, handlers):
    """Claim and run one due job. Returns the finished job, or None if nothing was due."""
    job = claim(dev, worker)
    if not job:
        return None
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_EVERY):
            try:
                heartbeat(dev, job["id"], worker)
            except Exception:  # noqa: BLE001 - a missed beat is retried; a stuck job is requeued by requeue_stale
                pass
    threading.Thread(target=beat, name=f"sia-heartbeat-{job['id'][:8]}", daemon=True).start()
    try:
        fn = handlers.get(job["kind"])
        if fn is None:
            raise ValueError(f"no handler for job kind {job['kind']!r}")
        result = fn(dev, job) or {}
        finish(dev, job["id"], worker, result.pop("_status", "done"), result)
    except Exception as e:  # noqa: BLE001 - the job records the failure; the worker keeps going
        finish(dev, job["id"], worker, "failed", error=f"{type(e).__name__}: {e}"[:2000])
    finally:
        stop.set()
    with dev._conn() as c:
        return _out(c.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())


def work(dev, handlers=None, once=False, concurrency=1, poll=2.0, tick=None, stop=None):
    """Run jobs until `stop` is set (forever by default). once=True runs every job that is due, then returns.
    tick(dev), if given, runs before each poll (the loop's triggers enqueue cycles there). Returns jobs run."""
    handlers = handlers or default_handlers()
    stop = stop or threading.Event()
    name = f"{socket.gethostname()}:{os.getpid()}"
    count, lock = [0], threading.Lock()

    def loop(i):
        worker = f"{name}:{i}"
        while not stop.is_set():
            if i == 0:
                requeue_stale(dev)
                if tick:
                    tick(dev)
            if run_one(dev, worker, handlers):
                with lock:
                    count[0] += 1
                continue
            if once:
                return
            stop.wait(poll)

    threads = [threading.Thread(target=loop, args=(i,), name=f"sia-worker-{i}") for i in range(max(1, concurrency))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return count[0]
