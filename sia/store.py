"""SQLite persistence: organizations (tenants) and their API keys, agents, knowledge (FTS5), lessons (FTS5),
playbook versions, episodes, eval examples, usage. Schema changes are applied as numbered migrations."""
import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager

from sia_devtools.store import migrate

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (id TEXT PRIMARY KEY, name TEXT, key_hash TEXT UNIQUE, created REAL);
CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, domain TEXT,
    instructions TEXT, rubric TEXT, created REAL);
CREATE TABLE IF NOT EXISTS playbooks (id INTEGER PRIMARY KEY, agent_id TEXT, version INTEGER, content TEXT,
    score REAL, active INTEGER, note TEXT, created REAL);
CREATE TABLE IF NOT EXISTS docs (id TEXT PRIMARY KEY, agent_id TEXT, title TEXT, chunks INTEGER, created REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(text, title UNINDEXED, agent_id UNINDEXED, doc_id UNINDEXED);
CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY, agent_id TEXT, text TEXT, source TEXT,
    uses INTEGER DEFAULT 0, total REAL DEFAULT 0, active INTEGER DEFAULT 1, created REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5(text, agent_id UNINDEXED, lesson_id UNINDEXED);
CREATE TABLE IF NOT EXISTS episodes (id TEXT PRIMARY KEY, agent_id TEXT, task TEXT, answer TEXT, score REAL,
    critique TEXT, attempts INTEGER, lesson_ids TEXT, rating INTEGER, correction TEXT, created REAL);
CREATE TABLE IF NOT EXISTS examples (id INTEGER PRIMARY KEY, agent_id TEXT, input TEXT, expected TEXT,
    source TEXT, created REAL);
CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, tenant_id TEXT, agent_id TEXT, action TEXT,
    llm_calls INTEGER, created REAL);
CREATE INDEX IF NOT EXISTS idx_usage_tenant ON usage(tenant_id, created);
CREATE INDEX IF NOT EXISTS idx_agents_tenant ON agents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_lessons_agent ON lessons(agent_id, active);
CREATE INDEX IF NOT EXISTS idx_episodes_agent ON episodes(agent_id, created);
CREATE INDEX IF NOT EXISTS idx_examples_agent ON examples(agent_id);
CREATE INDEX IF NOT EXISTS idx_playbooks_agent ON playbooks(agent_id, active)
"""

MIGRATIONS = [("legacy-000-base", SCHEMA), ("legacy-001-keys-plans-review", """
CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT, prefix TEXT,
    key_hash TEXT UNIQUE NOT NULL, scope TEXT NOT NULL DEFAULT 'full', created REAL, last_used REAL, revoked REAL);
CREATE INDEX IF NOT EXISTS idx_api_keys_org ON api_keys(org_id);
INSERT OR IGNORE INTO api_keys (id, org_id, name, prefix, key_hash, scope, created)
    SELECT lower(hex(randomblob(8))), id, 'default', '', key_hash, 'full', created FROM tenants WHERE key_hash IS NOT NULL;
ALTER TABLE tenants ADD COLUMN plan TEXT NOT NULL DEFAULT 'free';
ALTER TABLE examples ADD COLUMN status TEXT NOT NULL DEFAULT 'verified';
ALTER TABLE examples ADD COLUMN episode_id TEXT;
ALTER TABLE usage ADD COLUMN units INTEGER NOT NULL DEFAULT 1;
ALTER TABLE usage ADD COLUMN project_id TEXT
"""), ("legacy-002-payments", """
ALTER TABLE tenants ADD COLUMN paid_until REAL;
CREATE TABLE IF NOT EXISTS payments (order_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, plan TEXT NOT NULL,
    amount INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'created', payment_id TEXT,
    created REAL, paid REAL);
CREATE INDEX IF NOT EXISTS idx_payments_org ON payments(org_id, created)
"""), ("legacy-003-access-requests", """
CREATE TABLE IF NOT EXISTS access_requests (id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '', use_case TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
    org_id TEXT, created REAL NOT NULL, decided REAL);
CREATE INDEX IF NOT EXISTS idx_access_requests ON access_requests(status, email)
""")]
PAID_PERIOD_DAYS = 30  # one payment buys this many days of a paid plan

# Starting hypotheses for a freemium plan, not validated prices. Enforced server-side in sia/api.py.
PLANS = {
    "free": {"projects": 1, "runs_per_month": 10_000, "examples_per_dataset": 200, "retention_days": 14,
             "overage": False},
    "pro": {"projects": 10, "runs_per_month": 250_000, "examples_per_dataset": 5_000, "retention_days": 90,
            "overage": True, "hard_cap": 2.0},
    "team": {"projects": 100, "runs_per_month": 2_000_000, "examples_per_dataset": 50_000, "retention_days": 365,
             "overage": True, "hard_cap": 2.0},
}
# hard_cap: paid plans may go past their included runs up to this multiple. Overage isn't billed during the beta,
# so without a cap a paid plan would be unlimited.
KEY_SCOPES = ("full", "ingest")  # ingest: send runs and feedback, read the active prompt; nothing else

PRIOR, PRIOR_WEIGHT = 0.7, 2  # new lessons start with a mildly optimistic utility
PRUNE_AFTER_USES, PRUNE_BELOW = 5, 0.45


def fts_query(text, limit=32):
    """Turn free text into a safe FTS5 OR-query (quoted tokens, so user text can't inject FTS syntax)."""
    words = list(dict.fromkeys(w for w in re.findall(r"\w+", text.lower()) if len(w) > 2))[:limit]
    return " OR ".join(f'"{w}"' for w in words)


def tokens(text):
    return set(re.findall(r"\w+", text.lower()))


def chunk_text(content, size=1000):
    """Split on paragraphs, packing them into ~size-char chunks; hard-split oversized paragraphs."""
    out, buf = [], ""
    for para in re.split(r"\n\s*\n", content):
        para = para.strip()
        while len(para) > size:
            out.append(para[:size])
            para = para[size:]
        if buf and len(buf) + len(para) > size:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n\n{para}".strip()
    if buf:
        out.append(buf)
    return out


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


class Store:
    def __init__(self, path="sia.db"):
        self.path = path
        migrate(path, MIGRATIONS)

    @contextmanager
    def _conn(self):
        """One connection per call: commit on success, roll back on error, always close."""
        with closing(sqlite3.connect(self.path, timeout=30)) as c:
            c.row_factory = sqlite3.Row
            with c:
                yield c

    def q(self, sql, *args):
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def x(self, sql, *args):
        with self._conn() as c:
            return c.execute(sql, args).lastrowid

    def n(self, sql, *args):
        """Execute and return the number of rows changed."""
        with self._conn() as c:
            return c.execute(sql, args).rowcount

    # --- tenants (organizations) and API keys --------------------------------
    def create_tenant(self, name, plan="free"):
        with self._conn() as c:
            return self._new_tenant(c, name, plan)

    def _new_tenant(self, c, name, plan="free"):
        """The organization and its first full-scope key, on connection c (one transaction)."""
        tid = uuid.uuid4().hex
        c.execute("INSERT INTO tenants (id, name, created, plan) VALUES (?,?,?,?)", (tid, name, time.time(), plan))
        return {"tenant_id": tid, "name": name, "api_key": self._new_key(c, tid, "default")["api_key"]}

    # a paid plan whose period ran out falls back to free
    PLAN_SQL = "CASE WHEN t.paid_until IS NOT NULL AND t.paid_until < ? THEN 'free' ELSE t.plan END"

    def tenant(self, tenant_id):
        rows = self.q(f"SELECT t.id, t.name, {self.PLAN_SQL} plan, t.paid_until, t.created FROM tenants t WHERE id=?",
                      time.time(), tenant_id)
        return rows[0] if rows else None

    def set_plan(self, tenant_id, plan):
        """Admin override: set a plan with no expiry."""
        if plan not in PLANS:
            raise ValueError(f"plan must be one of {', '.join(PLANS)}")
        return bool(self.n("UPDATE tenants SET plan=?, paid_until=NULL WHERE id=?", plan, tenant_id))

    # --- payments ------------------------------------------------------------
    def add_payment(self, order_id, tenant_id, plan, amount, currency):
        self.x("INSERT INTO payments (order_id, org_id, plan, amount, currency, created) VALUES (?,?,?,?,?,?)",
               order_id, tenant_id, plan, amount, currency, time.time())

    def payment(self, order_id):
        rows = self.q("SELECT * FROM payments WHERE order_id=?", order_id)
        return rows[0] if rows else None

    def payments(self, tenant_id, limit=50):
        return self.q("SELECT * FROM payments WHERE org_id=? ORDER BY created DESC LIMIT ?", tenant_id, limit)

    def mark_paid(self, order_id, payment_id):
        """Record a confirmed payment and extend the plan by PAID_PERIOD_DAYS. Idempotent: an order that is already
        paid changes nothing, so a webhook and the checkout callback can both report the same payment."""
        now = time.time()
        with self._conn() as c:
            if not c.execute("UPDATE payments SET status='paid', payment_id=?, paid=? WHERE order_id=? AND status!='paid'",
                             (payment_id, now, order_id)).rowcount:
                return False
            pay = c.execute("SELECT org_id, plan FROM payments WHERE order_id=?", (order_id,)).fetchone()
            t = c.execute("SELECT plan, paid_until FROM tenants WHERE id=?", (pay["org_id"],)).fetchone()
            # same plan still running: add to it; otherwise the new period starts now
            start = t["paid_until"] if t["plan"] == pay["plan"] and (t["paid_until"] or 0) > now else now
            c.execute("UPDATE tenants SET plan=?, paid_until=? WHERE id=?",
                      (pay["plan"], start + PAID_PERIOD_DAYS * 86400, pay["org_id"]))
        return True

    def create_key(self, tenant_id, name="key", scope="full"):
        """Keys are stored only as SHA-256 hashes; the plaintext is returned once, here."""
        with self._conn() as c:
            return self._new_key(c, tenant_id, name, scope)

    @staticmethod
    def _new_key(c, tenant_id, name="key", scope="full"):
        if scope not in KEY_SCOPES:
            raise ValueError(f"scope must be one of {KEY_SCOPES}")
        key, kid = "sia_" + secrets.token_urlsafe(32), uuid.uuid4().hex[:16]
        c.execute("INSERT INTO api_keys (id, org_id, name, prefix, key_hash, scope, created) VALUES (?,?,?,?,?,?,?)",
                  (kid, tenant_id, name, key[:8], hash_key(key), scope, time.time()))
        return {"id": kid, "name": name, "scope": scope, "prefix": key[:8], "api_key": key}

    def keys(self, tenant_id):
        return self.q("SELECT id, name, prefix, scope, created, last_used, revoked FROM api_keys WHERE org_id=? "
                      "ORDER BY created", tenant_id)

    def revoke_key(self, tenant_id, key_id):
        return bool(self.n("UPDATE api_keys SET revoked=? WHERE id=? AND org_id=? AND revoked IS NULL",
                           time.time(), key_id, tenant_id))

    def rotate_key(self, tenant_id, key_id=None):
        """Issue a replacement key (same name and scope); the old one stops working immediately. Data is kept."""
        old = self.q("SELECT * FROM api_keys WHERE org_id=? AND revoked IS NULL" + (" AND id=?" if key_id else ""),
                     tenant_id, *([key_id] if key_id else []))
        for k in old:
            self.revoke_key(tenant_id, k["id"])
        new = self.create_key(tenant_id, old[0]["name"] if old else "default", old[0]["scope"] if old else "full")
        return {"tenant_id": tenant_id, **new}

    def tenant_by_key(self, key):
        rows = self.q(f"""SELECT t.id, t.name, {self.PLAN_SQL} plan, t.paid_until, k.id key_id, k.scope
                          FROM api_keys k JOIN tenants t ON t.id=k.org_id WHERE k.key_hash=? AND k.revoked IS NULL""",
                      time.time(), hash_key(key))
        if not rows:
            return None
        now = time.time()  # last_used is informational; write it at most once a minute per key
        self.x("UPDATE api_keys SET last_used=? WHERE id=? AND (last_used IS NULL OR last_used<?)",
               now, rows[0]["key_id"], now - 60)
        return rows[0]

    # --- access requests (manual onboarding) ---------------------------------
    def add_access_request(self, email, name, company="", use_case=""):
        """Store a request unless the same email already has one pending. Returns True if a row was added."""
        email = email.strip().lower()
        with self._conn() as c:
            if c.execute("SELECT 1 FROM access_requests WHERE email=? AND status='pending'", (email,)).fetchone():
                return False
            c.execute("INSERT INTO access_requests (id, email, name, company, use_case, created) VALUES (?,?,?,?,?,?)",
                      (uuid.uuid4().hex, email, name.strip(), company.strip(), use_case.strip(), time.time()))
        return True

    def access_requests(self, status=None, limit=500):
        return self.q("SELECT * FROM access_requests" + (" WHERE status=?" if status else "") +
                      " ORDER BY created LIMIT ?", *([status] if status else []), limit)

    def decide_access_request(self, request_id, approve):
        """pending -> approved (creates the organization and its first full-scope key) | rejected. Returns None if
        the request is missing or was already decided, so an approval can never create two organizations. One
        transaction: if creating the organization fails, the request stays pending."""
        with self._conn() as c:
            if not c.execute("UPDATE access_requests SET status=?, decided=? WHERE id=? AND status='pending'",
                             ("approved" if approve else "rejected", time.time(), request_id)).rowcount:
                return None
            if not approve:
                return {"id": request_id, "status": "rejected"}
            r = c.execute("SELECT * FROM access_requests WHERE id=?", (request_id,)).fetchone()
            org = self._new_tenant(c, r["company"] or r["name"])
            c.execute("UPDATE access_requests SET org_id=? WHERE id=?", (org["tenant_id"], request_id))
        return {"id": request_id, "status": "approved", "email": r["email"], "org_id": org["tenant_id"],
                "org_name": org["name"], "api_key": org["api_key"]}  # the key is shown only once

    # --- agents --------------------------------------------------------------
    def create_agent(self, tenant_id, name, domain, instructions="", rubric=""):
        aid = uuid.uuid4().hex
        self.x("INSERT INTO agents VALUES (?,?,?,?,?,?,?)", aid, tenant_id, name, domain, instructions, rubric, time.time())
        return self.agent(aid)

    def agent(self, agent_id, tenant_id=None):
        rows = self.q("SELECT * FROM agents WHERE id=?", agent_id)
        if not rows or (tenant_id is not None and rows[0]["tenant_id"] != tenant_id):
            return None
        return rows[0]

    def agents(self, tenant_id):
        return self.q("SELECT * FROM agents WHERE tenant_id=? ORDER BY created", tenant_id)

    def update_agent(self, agent_id, **fields):
        for k, v in fields.items():
            if k in ("name", "domain", "instructions", "rubric") and v is not None:
                self.x(f"UPDATE agents SET {k}=? WHERE id=?", v, agent_id)

    def delete_agent(self, agent_id):
        with self._conn() as c:
            for table in ("agents", "playbooks", "docs", "chunks", "lessons", "lessons_fts", "episodes", "examples"):
                c.execute(f"DELETE FROM {table} WHERE {'id' if table == 'agents' else 'agent_id'}=?", (agent_id,))

    # --- knowledge -----------------------------------------------------------
    def add_doc(self, agent_id, title, content):
        did, parts = uuid.uuid4().hex, chunk_text(content)
        with self._conn() as c:
            c.execute("INSERT INTO docs VALUES (?,?,?,?,?)", (did, agent_id, title, len(parts), time.time()))
            c.executemany("INSERT INTO chunks VALUES (?,?,?,?)", [(p, title, agent_id, did) for p in parts])
        return {"doc_id": did, "title": title, "chunks": len(parts)}

    def docs(self, agent_id):
        return self.q("SELECT id, title, chunks, created FROM docs WHERE agent_id=? ORDER BY created", agent_id)

    def delete_doc(self, agent_id, doc_id):
        with self._conn() as c:
            found = c.execute("DELETE FROM docs WHERE id=? AND agent_id=?", (doc_id, agent_id)).rowcount
            c.execute("DELETE FROM chunks WHERE doc_id=? AND agent_id=?", (doc_id, agent_id))
        return bool(found)

    def search_chunks(self, agent_id, text, k=5):
        query = fts_query(text)
        if not query:
            return []
        return self.q("SELECT text, title FROM chunks WHERE chunks MATCH ? AND agent_id=? ORDER BY rank LIMIT ?",
                      query, agent_id, k)

    def random_chunks(self, agent_id, n):
        return self.q("SELECT text, title FROM chunks WHERE agent_id=? ORDER BY random() LIMIT ?", agent_id, n)

    # --- lessons -------------------------------------------------------------
    def add_lesson(self, agent_id, text, source):
        """Store a lesson unless a near-duplicate already exists. Returns the new id or None."""
        text = text.strip()
        if not text:
            return None
        new = tokens(text)
        for row in self._match_lessons(agent_id, text, 5):
            old = tokens(row["text"])
            if len(new & old) / max(len(new | old), 1) > 0.6:
                return None
        with self._conn() as c:
            lid = c.execute("INSERT INTO lessons (agent_id, text, source, created) VALUES (?,?,?,?)",
                            (agent_id, text, source, time.time())).lastrowid
            c.execute("INSERT INTO lessons_fts VALUES (?,?,?)", (text, agent_id, lid))
        return lid

    def _match_lessons(self, agent_id, text, k):
        query = fts_query(text)
        if not query:
            return []
        return self.q("""SELECT l.* FROM lessons_fts f JOIN lessons l ON l.id = f.lesson_id
                         WHERE lessons_fts MATCH ? AND f.agent_id=? AND l.active=1 ORDER BY f.rank LIMIT ?""",
                      query, agent_id, k)

    @staticmethod
    def utility(lesson):
        return (lesson["total"] + PRIOR * PRIOR_WEIGHT) / (lesson["uses"] + PRIOR_WEIGHT)

    def relevant_lessons(self, agent_id, text, k=6):
        """Lessons relevant to this task, plus the most proven general lessons, ranked by utility."""
        picked = {r["id"]: r for r in self._match_lessons(agent_id, text, k * 2)}
        for r in self.top_lessons(agent_id, 3):
            picked.setdefault(r["id"], r)
        return sorted(picked.values(), key=self.utility, reverse=True)[:k]

    def top_lessons(self, agent_id, k=20):
        # ponytail: utility sorted in Python over all active lessons; move into SQL if agents reach ~100k lessons.
        rows = self.q("SELECT * FROM lessons WHERE agent_id=? AND active=1", agent_id)
        return sorted(rows, key=self.utility, reverse=True)[:k]

    def credit_lessons(self, lesson_ids, score):
        """Credit lessons used in an episode with its outcome; retire lessons that keep coinciding with failure."""
        with self._conn() as c:
            for lid in lesson_ids:
                c.execute("UPDATE lessons SET uses=uses+1, total=total+? WHERE id=?", (score, lid))
            c.execute(f"""UPDATE lessons SET active=0 WHERE uses>=? AND
                          (total + ? * ?) / (uses + ?) < ? AND id IN ({','.join('?' * len(lesson_ids)) or 'NULL'})""",
                      (PRUNE_AFTER_USES, PRIOR, PRIOR_WEIGHT, PRIOR_WEIGHT, PRUNE_BELOW, *lesson_ids))

    def lessons(self, agent_id, include_inactive=False):
        rows = self.q(f"SELECT * FROM lessons WHERE agent_id=? {'' if include_inactive else 'AND active=1'}", agent_id)
        for r in rows:
            r["utility"] = round(self.utility(r), 3)
        return sorted(rows, key=lambda r: r["utility"], reverse=True)

    def set_lesson_active(self, agent_id, lesson_id, active):
        return bool(self.n("UPDATE lessons SET active=? WHERE id=? AND agent_id=?", int(active), lesson_id, agent_id))

    # --- playbooks -----------------------------------------------------------
    def active_playbook(self, agent_id):
        rows = self.q("SELECT * FROM playbooks WHERE agent_id=? AND active=1", agent_id)
        return rows[0] if rows else {"version": 0, "content": "", "score": None}

    def add_playbook(self, agent_id, content, score, activate, note=""):
        with self._conn() as c:
            version = c.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM playbooks WHERE agent_id=?",
                                (agent_id,)).fetchone()[0]
            if activate:
                c.execute("UPDATE playbooks SET active=0 WHERE agent_id=?", (agent_id,))
            c.execute("INSERT INTO playbooks (agent_id, version, content, score, active, note, created) "
                      "VALUES (?,?,?,?,?,?,?)", (agent_id, version, content, score, int(activate), note, time.time()))
        return version

    def playbooks(self, agent_id):
        return self.q("SELECT version, content, score, active, note, created FROM playbooks "
                      "WHERE agent_id=? ORDER BY version DESC", agent_id)

    def activate_playbook(self, agent_id, version):
        """Roll back/forward to a specific playbook version (0 = no playbook)."""
        with self._conn() as c:
            if version and not c.execute("SELECT 1 FROM playbooks WHERE agent_id=? AND version=?",
                                         (agent_id, version)).fetchone():
                return False
            c.execute("UPDATE playbooks SET active=0 WHERE agent_id=?", (agent_id,))
            c.execute("UPDATE playbooks SET active=1 WHERE agent_id=? AND version=?", (agent_id, version))
            return True

    # --- episodes ------------------------------------------------------------
    def add_episode(self, agent_id, task, answer, score, critique, attempts, lesson_ids):
        eid = uuid.uuid4().hex
        self.x("INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?,?)", eid, agent_id, task, answer, score,
               json.dumps(critique), attempts, json.dumps(lesson_ids), None, None, time.time())
        return eid

    def episode(self, agent_id, episode_id):
        rows = self.q("SELECT * FROM episodes WHERE id=? AND agent_id=?", episode_id, agent_id)
        if not rows:
            return None
        e = rows[0]
        e["critique"], e["lesson_ids"] = json.loads(e["critique"] or "null"), json.loads(e["lesson_ids"] or "[]")
        return e

    def episodes(self, agent_id, limit=50):
        rows = self.q("SELECT * FROM episodes WHERE agent_id=? ORDER BY created DESC LIMIT ?", agent_id, limit)
        for e in rows:
            e["critique"], e["lesson_ids"] = json.loads(e["critique"] or "null"), json.loads(e["lesson_ids"] or "[]")
        return rows

    def set_feedback(self, episode_id, rating, correction):
        self.x("UPDATE episodes SET rating=?, correction=? WHERE id=?", rating, correction, episode_id)

    # --- eval examples -------------------------------------------------------
    def add_example(self, agent_id, input, expected, source, status="verified", episode_id=None):
        return self.x("INSERT INTO examples (agent_id, input, expected, source, created, status, episode_id) "
                      "VALUES (?,?,?,?,?,?,?)", agent_id, input, expected, source, time.time(), status, episode_id)

    def examples(self, agent_id, limit=None, include_unverified=False):
        """Eval set, real data first: user-provided, then verified feedback corrections, then synthetic study
        questions. User corrections stay 'pending' (out of the eval set) until someone verifies them."""
        sql = f"""SELECT * FROM examples WHERE agent_id=? {'' if include_unverified else "AND status='verified'"}
                 ORDER BY CASE source WHEN 'user' THEN 0 WHEN 'feedback' THEN 1 ELSE 2 END, random()"""
        return self.q(sql + (" LIMIT ?" if limit else ""), agent_id, *([limit] if limit else []))

    def example(self, agent_id, example_id):
        rows = self.q("SELECT * FROM examples WHERE id=? AND agent_id=?", example_id, agent_id)
        return rows[0] if rows else None

    def review_example(self, agent_id, example_id, status):
        """pending -> verified | rejected. Returns False if the example is missing or was already reviewed."""
        return bool(self.n("UPDATE examples SET status=? WHERE id=? AND agent_id=? AND status='pending'",
                           status, example_id, agent_id))

    def delete_example(self, agent_id, example_id):
        return bool(self.n("DELETE FROM examples WHERE id=? AND agent_id=?", example_id, agent_id))

    # --- usage (metering) ----------------------------------------------------
    def record_usage(self, tenant_id, agent_id, action, llm_calls, units=1, project_id=None):
        self.x("INSERT INTO usage (tenant_id, agent_id, action, llm_calls, created, units, project_id) "
               "VALUES (?,?,?,?,?,?,?)", tenant_id, agent_id, action, llm_calls, time.time(), units, project_id)

    def usage(self, tenant_id, since=0):
        """Requests, LLM calls and units (e.g. runs ingested) per action since a timestamp; kept after agents and
        projects are deleted, for billing."""
        rows = self.q("""SELECT action, COUNT(*) requests, SUM(llm_calls) llm_calls, SUM(units) units FROM usage
                         WHERE tenant_id=? AND created>=? GROUP BY action ORDER BY action""", tenant_id, since)
        return {"requests": sum(r["requests"] for r in rows), "llm_calls": sum(r["llm_calls"] for r in rows),
                "by_action": {r["action"]: {"requests": r["requests"], "llm_calls": r["llm_calls"],
                                            "units": r["units"]} for r in rows}}

    def units_since(self, tenant_id, action, since):
        return self.q("SELECT COALESCE(SUM(units), 0) n FROM usage WHERE tenant_id=? AND action=? AND created>=?",
                      tenant_id, action, since)[0]["n"]

    # --- stats ---------------------------------------------------------------
    def stats(self, agent_id):
        scores = [r["score"] for r in self.q(
            "SELECT score FROM episodes WHERE agent_id=? AND score IS NOT NULL ORDER BY created", agent_id)]
        pb = self.active_playbook(agent_id)
        last_improve = self.q("SELECT MAX(created) t FROM playbooks WHERE agent_id=?", agent_id)[0]["t"] or 0
        since = self.q("SELECT COUNT(*) n FROM episodes WHERE agent_id=? AND created>?", agent_id, last_improve)[0]["n"]
        avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
        half = min(20, len(scores) // 2)  # first/last windows never overlap
        count = lambda t: self.q(f"SELECT COUNT(*) n FROM {t} WHERE agent_id=?", agent_id)[0]["n"]
        return {
            "episodes": len(scores),
            "avg_score_first_20": avg(scores[:half]),
            "avg_score_last_20": avg(scores[len(scores) - half:] if half else []),
            "active_lessons": self.q("SELECT COUNT(*) n FROM lessons WHERE agent_id=? AND active=1", agent_id)[0]["n"],
            "documents": count("docs"),
            "eval_examples": self.q("SELECT COUNT(*) n FROM examples WHERE agent_id=? AND status='verified'",
                                    agent_id)[0]["n"],
            "pending_examples": self.q("SELECT COUNT(*) n FROM examples WHERE agent_id=? AND status='pending'",
                                       agent_id)[0]["n"],
            "playbook_version": pb["version"],
            "playbook_score": pb["score"],
            "episodes_since_last_improve": since,
            "improve_recommended": since >= 10,
        }
