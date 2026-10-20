"""SQLite storage for SIA DevTools: projects, runs, feedback, datasets, evaluations, comparisons, versions, audit.

Every public method takes the caller's org id and checks that the project belongs to it, so the same code serves
local mode (org "local") and the hosted API (org = the authenticated organization). IDs from another project or
org are simply "not found".
"""
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager

from .errors import Conflict, NotFound, ValidationError
from .scoring import DEFAULT_POLICY, compare_scores, primary_score

LOCAL_ORG = "local"
MAX_FIELD = 64_000  # characters per stored JSON payload; longer payloads are truncated with a marker, not rejected
ID_RX = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
NAME_RX = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# --- redaction ----------------------------------------------------------------
REDACTORS = {
    "secrets": r"(?:sk|pk|rk|sia)[-_][A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{30,}|(?i:bearer)\s+[A-Za-z0-9._~+/\-]{16,}"
               r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    "cards": r"\b\d(?:[ -]?\d){14,15}\b",
    "email": r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
    "phone": r"\+?\d[\d ().-]{8,}\d",
}
DEFAULT_REDACT = ["secrets", "cards"]


def _luhn(digits):
    d = [int(x) for x in reversed(digits)]
    return (sum(d[0::2]) + sum(sum(divmod(2 * x, 10)) for x in d[1::2])) % 10 == 0


def redactor(rules):
    """Return fn(obj) that masks matches of the named rules (see REDACTORS) or custom regexes in every string."""
    compiled = []
    for r in rules:
        try:
            compiled.append((r if r in REDACTORS else "custom", re.compile(REDACTORS.get(r, r))))
        except re.error as e:
            raise ValidationError(f"Invalid redaction pattern {r!r}: {e}")

    def mask(name):
        if name == "cards":  # only real-looking card numbers (Luhn check), not every long id
            return lambda m: "[REDACTED:card]" if _luhn(re.sub(r"\D", "", m.group(0))) else m.group(0)
        return lambda m: f"[REDACTED:{name}]"

    subs = [(rx, mask(name)) for name, rx in compiled]

    def scrub(x):
        if isinstance(x, str):
            for rx, fn in subs:
                x = rx.sub(fn, x)
            return x
        if isinstance(x, dict):
            return {k: scrub(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [scrub(v) for v in x]
        return x
    return scrub


def dump(x):
    s = json.dumps(x, default=str, ensure_ascii=False)
    if len(s) > MAX_FIELD:
        s = json.dumps({"_truncated": True, "original_chars": len(s), "preview": s[:MAX_FIELD]})
    return s


def load(s):
    return None if s is None else json.loads(s)


# --- migrations ---------------------------------------------------------------
def migrate(path, migrations):
    """Apply each (id, sql) once, in order, atomically. Safe to run from several processes at once."""
    with closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied REAL)")
        for mid, sql in migrations:
            c.execute("BEGIN IMMEDIATE")
            try:
                if not c.execute("SELECT 1 FROM schema_migrations WHERE id=?", (mid,)).fetchone():
                    for stmt in filter(str.strip, sql.split(";")):
                        c.execute(stmt)
                    c.execute("INSERT INTO schema_migrations VALUES (?,?)", (mid, time.time()))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise


MIGRATIONS = [("devtools-001", """
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}', created REAL, UNIQUE(org_id, name));
CREATE TABLE IF NOT EXISTS runs (project_id TEXT NOT NULL, id TEXT NOT NULL, parent_id TEXT, name TEXT, input TEXT,
    output TEXT, status TEXT, error TEXT, started REAL, ended REAL, duration_ms REAL, model TEXT, provider TEXT,
    prompt_tokens INTEGER, completion_tokens INTEGER, cost REAL, tags TEXT, metadata TEXT, scores TEXT,
    version_name TEXT, version INTEGER, PRIMARY KEY (project_id, id));
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(project_id, started);
CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(project_id, parent_id);
CREATE TABLE IF NOT EXISTS feedback (project_id TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT NOT NULL,
    rating INTEGER, correction TEXT, category TEXT, explanation TEXT, reviewer TEXT,
    status TEXT NOT NULL DEFAULT 'pending', review_note TEXT, reviewed_by TEXT, reviewed REAL, example_id INTEGER,
    created REAL, PRIMARY KEY (project_id, id));
CREATE INDEX IF NOT EXISTS idx_feedback_run ON feedback(project_id, run_id);
CREATE TABLE IF NOT EXISTS datasets (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0, created REAL, UNIQUE(project_id, name));
CREATE TABLE IF NOT EXISTS dataset_examples (id INTEGER PRIMARY KEY, dataset_id TEXT NOT NULL, input TEXT,
    expected TEXT, metadata TEXT, critical INTEGER NOT NULL DEFAULT 0, source TEXT, source_run_id TEXT,
    feedback_id TEXT, replaces INTEGER, added_in INTEGER NOT NULL, removed_in INTEGER, created REAL);
CREATE INDEX IF NOT EXISTS idx_dataset_examples ON dataset_examples(dataset_id, added_in);
CREATE TABLE IF NOT EXISTS evaluations (project_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT, dataset_id TEXT,
    dataset_version INTEGER, version_name TEXT, version INTEGER, prompt_sha TEXT, config TEXT, status TEXT,
    summary TEXT, created REAL, PRIMARY KEY (project_id, id));
CREATE TABLE IF NOT EXISTS eval_results (project_id TEXT NOT NULL, eval_id TEXT NOT NULL, example_id INTEGER NOT NULL,
    output TEXT, scores TEXT, score REAL, passed INTEGER, error TEXT, duration_ms REAL,
    PRIMARY KEY (project_id, eval_id, example_id));
CREATE TABLE IF NOT EXISTS comparisons (project_id TEXT NOT NULL, id TEXT NOT NULL, baseline_eval TEXT,
    candidate_eval TEXT, policy TEXT, project_policy INTEGER, verdict TEXT, reason TEXT, stats TEXT, created REAL,
    PRIMARY KEY (project_id, id));
CREATE TABLE IF NOT EXISTS versions (project_id TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL,
    content TEXT, status TEXT NOT NULL DEFAULT 'candidate', parent_version INTEGER, lessons TEXT, patterns TEXT,
    note TEXT, created_by TEXT, decided_by TEXT, decision_reason TEXT, comparison_id TEXT, created REAL, decided REAL,
    PRIMARY KEY (project_id, name, version));
CREATE TABLE IF NOT EXISTS active_versions (project_id TEXT NOT NULL, name TEXT NOT NULL, version INTEGER,
    activated REAL, activated_by TEXT, PRIMARY KEY (project_id, name));
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, project_id TEXT, actor TEXT, action TEXT, detail TEXT,
    created REAL);
CREATE INDEX IF NOT EXISTS idx_audit ON audit(project_id, created)
""")]

RUN_STATUSES = ("running", "success", "error")
NOT_EVAL = "NOT EXISTS (SELECT 1 FROM json_each(runs.tags) WHERE value='eval')"  # production traffic only
DEFAULT_SETTINGS = {"redact": DEFAULT_REDACT, "retention_days": None, "policy": DEFAULT_POLICY, "locked_text": []}


def _id(v, what="id"):
    if not isinstance(v, str) or not ID_RX.match(v):
        raise ValidationError(f"Invalid {what}: use 1-64 letters, digits, '_', '-', '.' or ':'.")
    return v


def _num(v, what, lo=0.0, hi=None, integer=False):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo or (hi is not None and v > hi):
        raise ValidationError(f"{what} must be a number >= {lo}" + (f" and <= {hi}" if hi is not None else ""))
    return int(v) if integer else float(v)


def _text(v, what, limit, required=False):
    if v is None and not required:
        return None
    if not isinstance(v, str) or (required and not v.strip()):
        raise ValidationError(f"{what} must be a non-empty string" if required else f"{what} must be a string")
    return v[:limit]


def validate_policy(p):
    if not isinstance(p, dict) or set(p) - set(DEFAULT_POLICY):
        raise ValidationError(f"policy keys must be among: {', '.join(DEFAULT_POLICY)}")
    out = {}
    for k, v in p.items():
        if k == "max_regressions":
            out[k] = _num(v, k, 0, 10_000, integer=True)
        elif k == "min_examples":
            out[k] = _num(v, k, 1, 10_000, integer=True) or 1
        else:
            out[k] = _num(v, k, 0, 5 if k == "z" else 1)
            if out[k] is None:
                raise ValidationError(f"{k} cannot be null")
    return out


def validate_settings(new, base=None):
    s = json.loads(json.dumps(base or DEFAULT_SETTINGS))
    if not isinstance(new, dict) or set(new) - set(DEFAULT_SETTINGS):
        raise ValidationError(f"settings keys must be among: {', '.join(DEFAULT_SETTINGS)}")
    if "redact" in new:
        rules = new["redact"]
        if not isinstance(rules, list) or len(rules) > 20 or not all(isinstance(r, str) and 0 < len(r) <= 500
                                                                      for r in rules):
            raise ValidationError("redact must be a list of up to 20 rule names or regexes")
        redactor(rules)  # raises on a bad regex
        s["redact"] = rules
    if "retention_days" in new:
        s["retention_days"] = _num(new["retention_days"], "retention_days", 1, 3650, integer=True)
    if "policy" in new:
        s["policy"] = {**s["policy"], **validate_policy(new["policy"])}
    if "locked_text" in new:
        lt = new["locked_text"]
        if not isinstance(lt, list) or len(lt) > 20 or not all(isinstance(t, str) and t.strip() for t in lt):
            raise ValidationError("locked_text must be a list of up to 20 non-empty strings")
        s["locked_text"] = lt
    return s


class DevStore:
    def __init__(self, path="sia_local.db"):
        self.path = path
        migrate(path, MIGRATIONS)

    @contextmanager
    def _conn(self, write=False):
        """One connection per call. Writes take the lock up front (BEGIN IMMEDIATE), so read-then-write
        sequences such as 'next version number' cannot race between processes."""
        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as c:
            c.row_factory = sqlite3.Row
            c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    @staticmethod
    def _audit(c, pid, actor, action, detail):
        c.execute("INSERT INTO audit (project_id, actor, action, detail, created) VALUES (?,?,?,?,?)",
                  (pid, actor, action, json.dumps(detail, default=str), time.time()))

    # --- projects -------------------------------------------------------------
    @staticmethod
    def _proj(row):
        p = dict(row)
        p["settings"] = validate_settings(json.loads(p["settings"] or "{}"))
        return p

    def _project(self, c, org, pid):
        row = c.execute("SELECT * FROM projects WHERE id=? AND org_id=?", (pid, org)).fetchone()
        if not row:
            raise NotFound("Project not found.")
        return self._proj(row)

    def create_project(self, org, name, settings=None, create=True, actor=None):
        """Get or create by name. With create=False an unknown name is NotFound (for ingest-only keys)."""
        if not isinstance(name, str) or not NAME_RX.match(name):
            raise ValidationError("Project name: 1-64 letters, digits, '_', '-' or '.'.")
        with self._conn(write=True) as c:
            row = c.execute("SELECT * FROM projects WHERE org_id=? AND name=?", (org, name)).fetchone()
            if row:
                return self._proj(row)
            if not create:
                raise NotFound("Project not found.")
            s = validate_settings(settings or {})
            pid = uuid.uuid4().hex
            c.execute("INSERT INTO projects VALUES (?,?,?,?,?)", (pid, org, name, json.dumps(s), time.time()))
            self._audit(c, pid, actor, "project.create", {"name": name})
            return self._project(c, org, pid)

    def projects(self, org):
        with self._conn() as c:
            return [self._proj(r) for r in c.execute("SELECT * FROM projects WHERE org_id=? ORDER BY created", (org,))]

    def project(self, org, pid):
        with self._conn() as c:
            return self._project(c, org, pid)

    def count_projects(self, org):
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM projects WHERE org_id=?", (org,)).fetchone()[0]

    def update_project(self, org, pid, settings, actor=None):
        with self._conn(write=True) as c:
            p = self._project(c, org, pid)
            s = validate_settings(settings, p["settings"])
            c.execute("UPDATE projects SET settings=? WHERE id=?", (json.dumps(s), pid))
            self._audit(c, pid, actor, "project.settings", settings)
            return self._project(c, org, pid)

    # --- runs -----------------------------------------------------------------
    def _run_row(self, pid, r, scrub):
        if not isinstance(r, dict):
            raise ValidationError("Each run must be an object.")
        status = r.get("status", "success")
        if status not in RUN_STATUSES:
            raise ValidationError(f"status must be one of {RUN_STATUSES}")
        started = _num(r.get("started", time.time()), "started")
        ended = _num(r.get("ended"), "ended")
        duration = _num(r.get("duration_ms"), "duration_ms")
        if duration is None and ended is not None:
            duration = max(0.0, (ended - started) * 1000)
        tags = r.get("tags") or []
        if not isinstance(tags, list) or len(tags) > 20 or not all(isinstance(t, str) and len(t) <= 64 for t in tags):
            raise ValidationError("tags must be a list of up to 20 strings (max 64 chars each)")
        meta, scores = r.get("metadata") or {}, r.get("scores") or {}
        if not isinstance(meta, dict) or not isinstance(scores, dict):
            raise ValidationError("metadata and scores must be objects")
        scores = {str(k)[:64]: _num(v, f"score {k}", lo=float("-inf")) for k, v in scores.items()}
        parent = r.get("parent_id")
        return (pid, _id(r.get("id"), "run id"), _id(parent, "parent_id") if parent else None,
                _text(r.get("name"), "name", 200, required=True), dump(scrub(r.get("input"))),
                dump(scrub(r.get("output"))), status, scrub(_text(r.get("error"), "error", 10_000)), started, ended,
                duration, _text(r.get("model"), "model", 200), _text(r.get("provider"), "provider", 100),
                _num(r.get("prompt_tokens"), "prompt_tokens", integer=True),
                _num(r.get("completion_tokens"), "completion_tokens", integer=True), _num(r.get("cost"), "cost"),
                json.dumps(tags), dump(scrub(meta)), json.dumps(scores),
                _text(r.get("version_name"), "version_name", 64), _num(r.get("version"), "version", integer=True))

    def log_runs(self, org, pid, runs, retention_days=None):
        """Insert or update runs by id (idempotent: re-sending a run replaces it). Applies redaction and retention."""
        if not isinstance(runs, list):
            raise ValidationError("runs must be a list")
        with self._conn(write=True) as c:
            p = self._project(c, org, pid)
            scrub = redactor(p["settings"]["redact"])
            rows = [self._run_row(pid, r, scrub) for r in runs]
            c.executemany("""INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id, id) DO UPDATE SET parent_id=excluded.parent_id, name=excluded.name,
                input=excluded.input, output=excluded.output, status=excluded.status, error=excluded.error,
                started=excluded.started, ended=excluded.ended, duration_ms=excluded.duration_ms,
                model=excluded.model, provider=excluded.provider, prompt_tokens=excluded.prompt_tokens,
                completion_tokens=excluded.completion_tokens, cost=excluded.cost, tags=excluded.tags,
                metadata=excluded.metadata, scores=excluded.scores, version_name=excluded.version_name,
                version=excluded.version""", rows)
            days = [d for d in (retention_days, p["settings"]["retention_days"]) if d]
            if days:
                c.execute("DELETE FROM runs WHERE project_id=? AND started<?", (pid, time.time() - min(days) * 86400))
        return len(rows)

    @staticmethod
    def _run_out(row):
        r = dict(row)
        for k in ("input", "output", "tags", "metadata", "scores"):
            r[k] = load(r[k])
        return r

    def _filters(self, pid, status=None, tag=None, name=None, q=None, since=None, until=None, root_only=False,
                 version=None):
        sql, args = ["project_id=?"], [pid]
        if status:
            sql.append("status=?")
            args.append(status)
        if name:
            sql.append("name=?")
            args.append(name)
        if tag:
            sql.append("EXISTS (SELECT 1 FROM json_each(runs.tags) WHERE value=?)")
            args.append(tag)
        if q:
            sql.append("(instr(lower(name),?) OR instr(lower(input),?) OR instr(lower(output),?) "
                       "OR instr(lower(coalesce(error,'')),?))")
            args += [q.lower()] * 4
        if since is not None:
            sql.append("started>=?")
            args.append(float(since))
        if until is not None:
            sql.append("started<?")
            args.append(float(until))
        if root_only:
            sql.append("parent_id IS NULL")
        if version is not None:
            sql.append("version=?")
            args.append(int(version))
        return " AND ".join(sql), args

    def runs(self, org, pid, limit=50, offset=0, **filters):
        limit, offset = min(max(int(limit), 1), 200), max(int(offset), 0)
        with self._conn() as c:
            self._project(c, org, pid)
            where, args = self._filters(pid, **filters)
            rows = c.execute(f"SELECT * FROM runs WHERE {where} ORDER BY started DESC, id LIMIT ? OFFSET ?",
                             (*args, limit + 1, offset)).fetchall()
        return {"items": [self._run_out(r) for r in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}

    def export_runs(self, org, pid, **filters):
        with self._conn() as c:
            self._project(c, org, pid)
            where, args = self._filters(pid, **filters)
            # ponytail: loads up to 100k runs into memory; stream with a cursor if exports get bigger.
            return [self._run_out(r) for r in c.execute(
                f"SELECT * FROM runs WHERE {where} ORDER BY started LIMIT 100000", args)]

    def run(self, org, pid, rid):
        with self._conn() as c:
            self._project(c, org, pid)
            row = c.execute("SELECT * FROM runs WHERE project_id=? AND id=?", (pid, rid)).fetchone()
            if not row:
                raise NotFound("Run not found.")
            children = c.execute("""WITH RECURSIVE sub(id) AS (SELECT id FROM runs WHERE project_id=? AND parent_id=?
                UNION SELECT r.id FROM runs r JOIN sub ON r.parent_id=sub.id WHERE r.project_id=?)
                SELECT * FROM runs WHERE project_id=? AND id IN (SELECT id FROM sub) ORDER BY started""",
                                 (pid, rid, pid, pid)).fetchall()
            fb = c.execute("SELECT * FROM feedback WHERE project_id=? AND run_id=? ORDER BY created", (pid, rid))
            return {**self._run_out(row), "children": [self._run_out(r) for r in children],
                    "feedback": [self._fb_out(f) for f in fb]}

    # --- feedback -------------------------------------------------------------
    @staticmethod
    def _fb_out(row):
        f = dict(row)
        f["correction"] = load(f["correction"])
        for k in ("run_input", "run_output"):
            if k in f:
                f[k] = load(f[k])
        return f

    def add_feedback(self, org, pid, run_id, rating, correction=None, category=None, explanation=None,
                     reviewer=None, id=None):
        """Store feedback as *pending*. It only reaches a dataset when someone verifies it (review_feedback)."""
        if rating not in (-1, 1):
            raise ValidationError("rating must be 1 (good) or -1 (bad)")
        fid = _id(id, "feedback id") if id else uuid.uuid4().hex
        with self._conn(write=True) as c:
            p = self._project(c, org, pid)
            if not c.execute("SELECT 1 FROM runs WHERE project_id=? AND id=?", (pid, run_id)).fetchone():
                raise NotFound("Run not found.")
            scrub = redactor(p["settings"]["redact"])
            row = (dump(scrub(correction)) if correction is not None else None,
                   _text(category, "category", 100), scrub(_text(explanation, "explanation", 5000)),
                   _text(reviewer, "reviewer", 200))
            dup = c.execute("SELECT id FROM feedback WHERE project_id=? AND (id=? OR (run_id=? AND rating=? AND "
                            "correction IS ? AND category IS ? AND explanation IS ? AND reviewer IS ?))",
                            (pid, fid, run_id, rating, *row)).fetchone()
            if dup:  # same id, or the same feedback submitted twice: keep one copy
                return self._feedback(c, pid, dup["id"])
            c.execute("INSERT INTO feedback (project_id, id, run_id, rating, correction, category, explanation, "
                      "reviewer, created) VALUES (?,?,?,?,?,?,?,?,?)", (pid, fid, run_id, rating, *row, time.time()))
            return self._feedback(c, pid, fid)

    def _feedback(self, c, pid, fid):
        row = c.execute("""SELECT f.*, r.name run_name, r.input run_input, r.output run_output FROM feedback f
            LEFT JOIN runs r ON r.project_id=f.project_id AND r.id=f.run_id WHERE f.project_id=? AND f.id=?""",
                        (pid, fid)).fetchone()
        if not row:
            raise NotFound("Feedback not found.")
        return self._fb_out(row)

    def feedback_list(self, org, pid, status=None, run_id=None, limit=50, offset=0):
        limit, offset = min(max(int(limit), 1), 200), max(int(offset), 0)
        with self._conn() as c:
            self._project(c, org, pid)
            rows = c.execute(f"""SELECT f.*, r.name run_name, r.input run_input, r.output run_output FROM feedback f
                LEFT JOIN runs r ON r.project_id=f.project_id AND r.id=f.run_id WHERE f.project_id=?
                {'AND f.status=?' if status else ''} {'AND f.run_id=?' if run_id else ''}
                ORDER BY f.created DESC LIMIT ? OFFSET ?""",
                             (pid, *([status] if status else []), *([run_id] if run_id else []), limit + 1, offset))
            rows = rows.fetchall()
        return {"items": [self._fb_out(r) for r in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}

    def review_feedback(self, org, pid, fid, status, dataset_id=None, expected_output=None, note=None,
                        reviewer=None, critical=False):
        """Verify or reject pending feedback. Verifying with a dataset turns the run's input plus the correction
        (or an edited expected_output) into a dataset example that links back to the run and the feedback."""
        if status not in ("verified", "rejected"):
            raise ValidationError("status must be 'verified' or 'rejected'")
        with self._conn(write=True) as c:
            self._project(c, org, pid)
            fb = self._feedback(c, pid, fid)
            if fb["status"] != "pending":
                raise Conflict(f"Feedback is already {fb['status']}.")
            example_id = None
            if status == "verified" and dataset_id:
                expected = expected_output if expected_output is not None else fb["correction"]
                if expected is None:
                    raise ValidationError("This feedback has no correction; pass expected_output to verify it "
                                          "into a dataset.")
                self._dataset(c, pid, dataset_id)
                example_id = self._add_examples(c, pid, dataset_id, [
                    {"input": fb["run_input"], "expected_output": expected, "critical": critical,
                     "metadata": {"category": fb["category"], "explanation": fb["explanation"]}}],
                    source="feedback", source_run_id=fb["run_id"], feedback_id=fid)[0]
            c.execute("UPDATE feedback SET status=?, review_note=?, reviewed_by=?, reviewed=?, example_id=? "
                      "WHERE project_id=? AND id=?", (status, _text(note, "note", 2000), _text(reviewer, "reviewer",
                                                                                             200), time.time(),
                                                      example_id, pid, fid))
            self._audit(c, pid, reviewer, f"feedback.{status}", {"feedback_id": fid, "example_id": example_id})
            return self._feedback(c, pid, fid)

    # --- datasets -------------------------------------------------------------
    def _dataset(self, c, pid, did):
        row = c.execute("SELECT * FROM datasets WHERE id=? AND project_id=?", (did, pid)).fetchone()
        if not row:
            raise NotFound("Dataset not found.")
        return dict(row)

    @staticmethod
    def _snapshot(c, did, version):
        return c.execute("SELECT * FROM dataset_examples WHERE dataset_id=? AND added_in<=? AND "
                         "(removed_in IS NULL OR removed_in>?) ORDER BY id", (did, version, version)).fetchall()

    @staticmethod
    def _ex_out(row):
        e = dict(row)
        e["input"], e["expected_output"], e["metadata"] = load(e.pop("input")), load(e.pop("expected")), load(
            e["metadata"])
        e["critical"] = bool(e["critical"])
        return e

    def create_dataset(self, org, pid, name, actor=None):
        if not isinstance(name, str) or not NAME_RX.match(name):
            raise ValidationError("Dataset name: 1-64 letters, digits, '_', '-' or '.'.")
        with self._conn(write=True) as c:
            self._project(c, org, pid)
            row = c.execute("SELECT * FROM datasets WHERE project_id=? AND name=?", (pid, name)).fetchone()
            if row:
                return dict(row)
            did = uuid.uuid4().hex
            c.execute("INSERT INTO datasets VALUES (?,?,?,0,?)", (did, pid, name, time.time()))
            self._audit(c, pid, actor, "dataset.create", {"dataset_id": did, "name": name})
            return self._dataset(c, pid, did)

    def datasets(self, org, pid, name=None):
        with self._conn() as c:
            self._project(c, org, pid)
            rows = c.execute(f"""SELECT d.*, (SELECT COUNT(*) FROM dataset_examples e WHERE e.dataset_id=d.id
                AND e.removed_in IS NULL) examples FROM datasets d WHERE project_id=? {'AND name=?' if name else ''}
                ORDER BY created""", (pid, *([name] if name else []))).fetchall()
            return [dict(r) for r in rows]

    def dataset(self, org, pid, did, version=None):
        """The dataset with its examples as of `version` (default: current). Old versions are immutable."""
        with self._conn() as c:
            self._project(c, org, pid)
            d = self._dataset(c, pid, did)
            v = d["version"] if version is None else int(version)
            if not 0 <= v <= d["version"]:
                raise ValidationError(f"Dataset has versions 0-{d['version']}.")
            return {**d, "snapshot_version": v, "examples": [self._ex_out(r) for r in self._snapshot(c, did, v)]}

    def count_examples(self, org, pid, did):
        with self._conn() as c:
            self._project(c, org, pid)
            return c.execute("SELECT COUNT(*) FROM dataset_examples WHERE dataset_id=? AND removed_in IS NULL",
                             (did,)).fetchone()[0]

    @staticmethod
    def _bump(c, did):
        return c.execute("UPDATE datasets SET version=version+1 WHERE id=? RETURNING version", (did,)).fetchone()[0]

    def _add_examples(self, c, pid, did, items, source="user", source_run_id=None, feedback_id=None, replaces=None):
        if not isinstance(items, list) or not items:
            raise ValidationError("examples must be a non-empty list")
        scrub = redactor(self._proj(c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())
                         ["settings"]["redact"])
        rows = []
        for i, ex in enumerate(items):
            if not isinstance(ex, dict) or ex.get("input") is None:
                raise ValidationError(f"example {i}: 'input' is required")
            if set(ex) - {"input", "expected_output", "metadata", "critical"}:
                raise ValidationError(f"example {i}: allowed fields are input, expected_output, metadata, critical")
            if not isinstance(ex.get("metadata") or {}, dict):
                raise ValidationError(f"example {i}: metadata must be an object")
            rows.append((dump(scrub(ex["input"])), dump(scrub(ex.get("expected_output"))),
                         dump(ex.get("metadata") or {}), int(bool(ex.get("critical")))))
        v = self._bump(c, did)
        ids = [c.execute("""INSERT INTO dataset_examples (dataset_id, input, expected, metadata, critical, source,
            source_run_id, feedback_id, replaces, added_in, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                         (did, *r, source, source_run_id, feedback_id, replaces, v, time.time())).lastrowid
               for r in rows]
        self._audit(c, pid, None, "dataset.add", {"dataset_id": did, "version": v, "example_ids": ids})
        return ids

    def add_examples(self, org, pid, did, items):
        with self._conn(write=True) as c:
            self._project(c, org, pid)
            self._dataset(c, pid, did)
            return self._add_examples(c, pid, did, items)

    def _live_example(self, c, did, eid):
        row = c.execute("SELECT * FROM dataset_examples WHERE id=? AND dataset_id=? AND removed_in IS NULL",
                        (eid, did)).fetchone()
        if not row:
            raise NotFound("Example not found (or already replaced or deleted).")
        return self._ex_out(row)

    def update_example(self, org, pid, did, eid, fields):
        """Edits never change history: the old row is closed at the new version and a new row replaces it."""
        with self._conn(write=True) as c:
            self._project(c, org, pid)
            self._dataset(c, pid, did)
            old = self._live_example(c, did, eid)
            merged = {k: old[k] for k in ("input", "expected_output", "metadata", "critical")}
            merged.update({k: v for k, v in fields.items() if v is not None})
            new_id = self._add_examples(c, pid, did, [merged], old["source"], old["source_run_id"],
                                        old["feedback_id"], replaces=eid)[0]
            v = c.execute("SELECT version FROM datasets WHERE id=?", (did,)).fetchone()[0]
            c.execute("UPDATE dataset_examples SET removed_in=? WHERE id=?", (v, eid))
            return new_id

    def delete_example(self, org, pid, did, eid):
        with self._conn(write=True) as c:
            self._project(c, org, pid)
            self._dataset(c, pid, did)
            self._live_example(c, did, eid)
            v = self._bump(c, did)
            c.execute("UPDATE dataset_examples SET removed_in=? WHERE id=?", (v, eid))
            self._audit(c, pid, None, "dataset.delete", {"dataset_id": did, "version": v, "example_id": eid})

    def dataset_history(self, org, pid, did):
        with self._conn() as c:
            self._project(c, org, pid)
            d = self._dataset(c, pid, did)
            rows = c.execute("SELECT id, added_in, removed_in, replaces, source FROM dataset_examples "
                             "WHERE dataset_id=?", (did,)).fetchall()
        out = {v: {"version": v, "added": [], "edited": [], "removed": []} for v in range(1, d["version"] + 1)}
        replaced = {r["replaces"] for r in rows if r["replaces"]}
        for r in rows:
            out[r["added_in"]]["edited" if r["replaces"] else "added"].append(r["id"])
            if r["removed_in"] and r["id"] not in replaced:
                out[r["removed_in"]]["removed"].append(r["id"])
        return sorted(out.values(), key=lambda h: -h["version"])

    # --- evaluations ----------------------------------------------------------
    def record_evaluation(self, org, pid, ev, actor=None):
        """Store a finished evaluation. It must have one result per example of the pinned dataset version,
        otherwise it is refused: an interrupted run is never recorded as complete."""
        if not isinstance(ev, dict):
            raise ValidationError("evaluation must be an object")
        eid = _id(ev["id"], "evaluation id") if ev.get("id") else uuid.uuid4().hex
        config = ev.get("config") or {}
        scorers = config.get("scorers") or []
        if not isinstance(scorers, list) or not scorers or not all(isinstance(s, dict) and s.get("name")
                                                                  for s in scorers):
            raise ValidationError("config.scorers must list the scorers used ({name, version, kind})")
        kinds = {s["name"]: s.get("kind", "deterministic") for s in scorers}
        threshold = _num(config.get("pass_threshold", 0.75), "pass_threshold", 0, 1)
        with self._conn(write=True) as c:
            self._project(c, org, pid)
            existing = c.execute("SELECT id FROM evaluations WHERE project_id=? AND id=?", (pid, eid)).fetchone()
            if existing:
                return self._evaluation(c, pid, eid)
            d = self._dataset(c, pid, ev.get("dataset_id"))
            v = ev.get("dataset_version")
            if not isinstance(v, int) or not 0 <= v <= d["version"]:
                raise ValidationError("dataset_version must be an existing version of the dataset")
            want = {r["id"] for r in self._snapshot(c, d["id"], v)}
            results = ev.get("results")
            if not isinstance(results, list):
                raise ValidationError("results must be a list")
            got = [r.get("example_id") for r in results if isinstance(r, dict)]
            if len(got) != len(results) or len(set(got)) != len(got):
                raise ValidationError("results must be objects with unique example_id values")
            if set(got) - want:
                raise ValidationError(f"results reference examples not in dataset version {v}: {sorted(set(got) - want)}")
            if want - set(got):
                raise ValidationError(f"evaluation incomplete: {len(want - set(got))} example(s) have no result")
            if not want:
                raise ValidationError("the dataset version has no examples")
            vname, vnum = ev.get("version_name"), ev.get("version")
            if vnum is not None and not c.execute("SELECT 1 FROM versions WHERE project_id=? AND name=? AND version=?",
                                                  (pid, vname or "system_prompt", vnum)).fetchone():
                raise ValidationError("version does not exist")
            rows, scores_all = [], []
            for r in results:
                sc = r.get("scores") or {}
                if not isinstance(sc, dict) or set(sc) - set(kinds):
                    raise ValidationError(f"example {r['example_id']}: scores must use the configured scorer names")
                sc = {k: _num(x, f"score {k}", 0, 1) for k, x in sc.items()}
                score = primary_score(sc, kinds) if sc else 0.0
                scores_all.append((score, sc, r))
                rows.append((pid, eid, r["example_id"], dump(r.get("output")), json.dumps(sc), score,
                             int(score >= threshold), _text(r.get("error"), "error", 5000),
                             _num(r.get("duration_ms"), "duration_ms")))
            n = len(rows)
            lat = [r[8] for r in rows if r[8] is not None]
            summary = {"n": n, "score": round(sum(s for s, _, _ in scores_all) / n, 4),
                       "pass_rate": round(sum(r[6] for r in rows) / n, 4),
                       "errors": sum(1 for r in rows if r[7]),
                       "by_scorer": {k: round(sum(sc.get(k, 0) for _, sc, _ in scores_all) / n, 4) for k in kinds},
                       "primary": "deterministic" if "deterministic" in kinds.values() else "model",
                       "avg_latency_ms": round(sum(lat) / len(lat), 1) if lat else None}
            c.execute("INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (pid, eid, _text(ev.get("name"), "name", 200), d["id"], v, vname if vnum is not None else None,
                       vnum, _text(ev.get("prompt_sha"), "prompt_sha", 64), json.dumps(config), "completed",
                       json.dumps(summary), time.time()))
            c.executemany("INSERT INTO eval_results VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self._audit(c, pid, actor, "evaluation.create", {"evaluation_id": eid, "score": summary["score"]})
            return self._evaluation(c, pid, eid)

    def _evaluation(self, c, pid, eid, results=True):
        row = c.execute("""SELECT e.*, d.name dataset_name FROM evaluations e LEFT JOIN datasets d ON d.id=e.dataset_id
            WHERE e.project_id=? AND e.id=?""", (pid, eid)).fetchone()
        if not row:
            raise NotFound("Evaluation not found.")
        e = dict(row)
        e["config"], e["summary"] = load(e["config"]), load(e["summary"])
        if results:
            e["results"] = [{**dict(r), "output": load(r["output"]), "scores": load(r["scores"]),
                             "passed": bool(r["passed"]), "input": load(r["input"]),
                             "expected_output": load(r["expected"]), "critical": bool(r["critical"])}
                            for r in c.execute("""SELECT r.*, x.input, x.expected, x.critical FROM eval_results r
                                JOIN dataset_examples x ON x.id=r.example_id WHERE r.project_id=? AND r.eval_id=?
                                ORDER BY r.example_id""", (pid, eid))]
            for r in e["results"]:
                r.pop("expected", None)
        return e

    def evaluation(self, org, pid, eid):
        with self._conn() as c:
            self._project(c, org, pid)
            return self._evaluation(c, pid, eid)

    def evaluations(self, org, pid, dataset_id=None, limit=50, offset=0):
        limit, offset = min(max(int(limit), 1), 200), max(int(offset), 0)
        with self._conn() as c:
            self._project(c, org, pid)
            ids = c.execute(f"SELECT id FROM evaluations WHERE project_id=? {'AND dataset_id=?' if dataset_id else ''} "
                            "ORDER BY created DESC LIMIT ? OFFSET ?",
                            (pid, *([dataset_id] if dataset_id else []), limit + 1, offset)).fetchall()
            items = [self._evaluation(c, pid, r["id"], results=False) for r in ids[:limit]]
        return {"items": items, "next_offset": offset + limit if len(ids) > limit else None}

    # --- comparisons ----------------------------------------------------------
    def compare(self, org, pid, baseline_id, candidate_id, policy=None, actor=None):
        """Paired comparison of two evaluations of the same dataset version with the same scorers and trials."""
        overrides = validate_policy(policy or {})
        with self._conn(write=True) as c:
            p = self._project(c, org, pid)
            a, b = self._evaluation(c, pid, baseline_id), self._evaluation(c, pid, candidate_id)
            if (a["dataset_id"], a["dataset_version"]) != (b["dataset_id"], b["dataset_version"]):
                raise ValidationError("The evaluations used different datasets or dataset versions; run both on "
                                      "the same snapshot.")
            same = lambda k: a["config"].get(k) == b["config"].get(k)
            if not same("trials") or sorted(map(json.dumps, a["config"]["scorers"])) != sorted(
                    map(json.dumps, b["config"]["scorers"])):
                raise ValidationError("The evaluations used different scorers or trials, so they are not comparable.")
            old = {r["example_id"]: r for r in a["results"]}
            ids = [r["example_id"] for r in b["results"] if r["example_id"] in old]
            new = {r["example_id"]: r for r in b["results"]}
            eff = {**p["settings"]["policy"], **overrides}
            res = compare_scores([old[i]["score"] for i in ids], [new[i]["score"] for i in ids],
                                 [new[i]["critical"] for i in ids], eff, ids)
            cid = uuid.uuid4().hex
            c.execute("INSERT INTO comparisons VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (pid, cid, a["id"], b["id"], json.dumps(eff), int(eff == p["settings"]["policy"]),
                       res["verdict"], res["reason"], json.dumps(res), time.time()))
            self._audit(c, pid, actor, "comparison.create", {"comparison_id": cid, "verdict": res["verdict"]})
            return self._comparison(c, pid, cid)

    def _comparison(self, c, pid, cid):
        row = c.execute("SELECT * FROM comparisons WHERE project_id=? AND id=?", (pid, cid)).fetchone()
        if not row:
            raise NotFound("Comparison not found.")
        cmp = dict(row)
        cmp["policy"], cmp["project_policy"] = load(cmp["policy"]), bool(cmp["project_policy"])
        cmp.update(load(cmp.pop("stats")))
        for side in ("baseline", "candidate"):
            e = c.execute("SELECT version_name, version, prompt_sha, summary FROM evaluations WHERE project_id=? "
                          "AND id=?", (pid, cmp[f"{side}_eval"])).fetchone()
            cmp[side] = {"evaluation_id": cmp[f"{side}_eval"], "version_name": e["version_name"],
                         "version": e["version"], "prompt_sha": e["prompt_sha"], "score": load(e["summary"])["score"]}
        return cmp

    def comparison(self, org, pid, cid):
        with self._conn() as c:
            self._project(c, org, pid)
            return self._comparison(c, pid, cid)

    def comparisons(self, org, pid, limit=50):
        with self._conn() as c:
            self._project(c, org, pid)
            ids = c.execute("SELECT id FROM comparisons WHERE project_id=? ORDER BY created DESC LIMIT ?",
                            (pid, min(max(int(limit), 1), 200))).fetchall()
            return [self._comparison(c, pid, r["id"]) for r in ids]

    # --- versions (prompts, playbooks, policies) ------------------------------
    @staticmethod
    def _ver_out(row, active=None):
        v = dict(row)
        v["lessons"], v["patterns"] = load(v["lessons"]) or [], load(v["patterns"]) or []
        v["active"] = active == v["version"]
        return v

    def _version(self, c, pid, name, num):
        row = c.execute("SELECT * FROM versions WHERE project_id=? AND name=? AND version=?", (pid, name, num)).fetchone()
        if not row:
            raise NotFound(f"Version {name}@{num} not found.")
        return self._ver_out(row, self._active(c, pid, name))

    @staticmethod
    def _active(c, pid, name):
        row = c.execute("SELECT version FROM active_versions WHERE project_id=? AND name=?", (pid, name)).fetchone()
        return row["version"] if row else None

    def create_version(self, org, pid, content, name="system_prompt", parent_version=None, lessons=None,
                       patterns=None, note=None, actor=None):
        """Every new version starts as a *candidate*; it is never active until approved and activated."""
        if not NAME_RX.match(name or ""):
            raise ValidationError("Version name: 1-64 letters, digits, '_', '-' or '.'.")
        content = _text(content, "content", 50_000, required=True)
        if not isinstance(lessons or [], list) or not isinstance(patterns or [], list):
            raise ValidationError("lessons and patterns must be lists")
        with self._conn(write=True) as c:
            p = self._project(c, org, pid)
            missing = [t for t in p["settings"]["locked_text"] if t not in content]
            if missing:  # locked rules (e.g. safety instructions) can't be edited away by an improvement
                raise ValidationError(f"Candidate drops locked text required by project settings: {missing}")
            if parent_version is not None:
                self._version(c, pid, name, parent_version)
            num = c.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM versions WHERE project_id=? AND name=?",
                            (pid, name)).fetchone()[0]
            c.execute("""INSERT INTO versions (project_id, name, version, content, parent_version, lessons, patterns,
                note, created_by, created) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (pid, name, num, content, parent_version, dump(lessons or []), dump(patterns or []),
                       _text(note, "note", 2000), actor, time.time()))
            self._audit(c, pid, actor, "version.create", {"name": name, "version": num})
            return self._version(c, pid, name, num)

    def versions(self, org, pid, name=None):
        with self._conn() as c:
            self._project(c, org, pid)
            rows = c.execute(f"SELECT * FROM versions WHERE project_id=? {'AND name=?' if name else ''} "
                             "ORDER BY name, version DESC", (pid, *([name] if name else []))).fetchall()
            return [self._ver_out(r, self._active(c, pid, r["name"])) for r in rows]

    def version(self, org, pid, name, num):
        with self._conn() as c:
            self._project(c, org, pid)
            return self._version(c, pid, name, num)

    def active_version(self, org, pid, name="system_prompt"):
        with self._conn() as c:
            self._project(c, org, pid)
            num = self._active(c, pid, name)
            return None if num is None else self._version(c, pid, name, num)

    def approve(self, org, pid, name, num, comparison_id=None, override_reason=None, actor=None):
        """Approval needs evidence: a comparison, made with the project's own policy, in which this candidate beat
        the active version. Anything else needs a written override_reason, which is kept in the audit log."""
        with self._conn(write=True) as c:
            self._project(c, org, pid)
            v = self._version(c, pid, name, num)
            if v["status"] != "candidate":
                raise Conflict(f"Version {name}@{num} is already {v['status']}.")
            active = self._active(c, pid, name)
            override = _text(override_reason, "override_reason", 2000)
            if comparison_id:
                cmp = self._comparison(c, pid, comparison_id)
                if (cmp["candidate"]["version_name"], cmp["candidate"]["version"]) != (name, num):
                    raise ValidationError("The comparison's candidate evaluation is not this version.")
                if active is not None and cmp["baseline"]["version"] != active:
                    raise ValidationError(f"The comparison's baseline is not the active version ({name}@{active}).")
                if not cmp["project_policy"]:
                    raise ValidationError("The comparison used policy overrides; compare again with the project "
                                          "policy.")
                if cmp["verdict"] != "improved" and not override:
                    raise ValidationError(f"The comparison verdict is '{cmp['verdict']}' ({cmp['reason']}). "
                                          "Pass override_reason to approve anyway.")
                reason = override or cmp["reason"]
            elif active is None:
                reason = override or "initial version: nothing active to compare against"
            elif override:
                reason = override
            else:
                raise ValidationError("Approval needs a comparison against the active version, or an override_reason.")
            c.execute("UPDATE versions SET status='approved', decided_by=?, decision_reason=?, comparison_id=?, "
                      "decided=? WHERE project_id=? AND name=? AND version=?",
                      (actor, reason, comparison_id, time.time(), pid, name, num))
            self._audit(c, pid, actor, "version.approve", {"name": name, "version": num, "comparison_id": comparison_id,
                                                          "override": bool(override), "reason": reason})
            return self._version(c, pid, name, num)

    def reject(self, org, pid, name, num, reason=None, actor=None):
        with self._conn(write=True) as c:
            self._project(c, org, pid)
            v = self._version(c, pid, name, num)
            if v["status"] != "candidate":
                raise Conflict(f"Version {name}@{num} is already {v['status']}.")
            c.execute("UPDATE versions SET status='rejected', decided_by=?, decision_reason=?, decided=? "
                      "WHERE project_id=? AND name=? AND version=?",
                      (actor, _text(reason, "reason", 2000), time.time(), pid, name, num))
            self._audit(c, pid, actor, "version.reject", {"name": name, "version": num, "reason": reason})
            return self._version(c, pid, name, num)

    def activate(self, org, pid, name, num, actor=None):
        """Point production at an approved version. Activating an older approved version is the rollback."""
        with self._conn(write=True) as c:
            self._project(c, org, pid)
            v = self._version(c, pid, name, num)
            if v["status"] != "approved":
                raise ValidationError(f"Only approved versions can be activated ({name}@{num} is {v['status']}).")
            before = self._active(c, pid, name)
            c.execute("INSERT INTO active_versions VALUES (?,?,?,?,?) ON CONFLICT(project_id, name) DO UPDATE SET "
                      "version=excluded.version, activated=excluded.activated, activated_by=excluded.activated_by",
                      (pid, name, num, time.time(), actor))
            self._audit(c, pid, actor, "version.activate", {"name": name, "from": before, "to": num})
            return self._version(c, pid, name, num)

    def audit(self, org, pid, limit=100):
        with self._conn() as c:
            self._project(c, org, pid)
            return [{**dict(r), "detail": load(r["detail"])} for r in c.execute(
                "SELECT * FROM audit WHERE project_id=? ORDER BY id DESC LIMIT ?", (pid, min(max(int(limit), 1), 500)))]

    # --- overview -------------------------------------------------------------
    def overview(self, org, pid):
        """Headline numbers for the dashboard, all computed from stored rows."""
        with self._conn() as c:
            self._project(c, org, pid)
            t = dict(c.execute("""SELECT COUNT(*) runs, SUM(status='error') errors, AVG(duration_ms) avg_latency_ms,
                SUM(prompt_tokens) prompt_tokens, SUM(completion_tokens) completion_tokens, SUM(cost) cost
                FROM runs WHERE project_id=? AND parent_id IS NULL AND {NOT_EVAL}""".format(NOT_EVAL=NOT_EVAL),
                         (pid,)).fetchone())
            t["errors"] = t["errors"] or 0
            t["error_rate"] = round(t["errors"] / t["runs"], 4) if t["runs"] else None
            failures = [self._run_out(r) for r in c.execute(
                f"SELECT * FROM runs WHERE project_id=? AND status='error' AND {NOT_EVAL} ORDER BY started DESC "
                "LIMIT 5", (pid,))]
            by_version = [dict(r) for r in c.execute(f"""SELECT r.version_name, r.version, COUNT(*) runs,
                SUM(r.status='error') errors, AVG(r.duration_ms) avg_latency_ms,
                (SELECT AVG(f.rating) FROM feedback f JOIN runs r2 ON r2.project_id=f.project_id AND r2.id=f.run_id
                 WHERE f.project_id=r.project_id AND r2.version IS r.version) avg_rating
                FROM runs r WHERE r.project_id=? AND r.parent_id IS NULL AND {NOT_EVAL.replace("runs.", "r.")}
                GROUP BY r.version_name, r.version ORDER BY r.version""", (pid,))]
            fb = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) n FROM feedback WHERE project_id=? GROUP BY status", (pid,))}
            evals = [self._evaluation(c, pid, r["id"], results=False) for r in c.execute(
                "SELECT id FROM evaluations WHERE project_id=? ORDER BY created DESC LIMIT 10", (pid,))]
            last = c.execute("SELECT id FROM comparisons WHERE project_id=? ORDER BY created DESC LIMIT 1",
                             (pid,)).fetchone()
            active = [dict(r) for r in c.execute("SELECT name, version, activated FROM active_versions "
                                                 "WHERE project_id=?", (pid,))]
            return {**t, "recent_failures": failures, "by_version": by_version, "feedback": fb,
                    "evaluations": evals, "latest_comparison": self._comparison(c, pid, last["id"]) if last else None,
                    "active_versions": active}
