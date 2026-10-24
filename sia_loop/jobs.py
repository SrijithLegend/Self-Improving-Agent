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


def enqueue(dev, org, pid, kind, payload=None, run_after=None, unique=False):
    """Queue a job for a project. unique=True returns the job of this kind already queued or running, if any,
    instead of adding a second one."""
    if kind not in KINDS:
        raise ValidationError(f"job kind must be one of {KINDS}")
    with dev._conn(write=True) as c:
        dev._project(c, org, pid)
        if unique and (row := c.execute("SELECT * FROM jobs WHERE project_id=? AND kind=? AND status IN "
                                        "('queued', 'running') ORDER BY created LIMIT 1", (pid, kind)).fetchone()):
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
def default_handlers():
    """Job kind -> fn(dev, job) -> result dict. A result with "_status" finishes the job with that status (for
    example skipped_budget)."""
    return {"noop": lambda dev, job: {"ok": True}}


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
