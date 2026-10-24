"""SQLite persistence for the hosted server: organizations (tenants) and their API keys, payments, access requests,
usage. Everything a project holds lives in sia_devtools/store.py (DevStore) in the same file. Schema changes are
numbered migrations; the first ones created the managed-agent tables that legacy-006 drops once they're migrated."""
import hashlib
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
"""), ("legacy-004-unique-playbook-versions", """
UPDATE playbooks SET version = (SELECT COUNT(*) FROM playbooks p2 WHERE p2.agent_id=playbooks.agent_id
    AND p2.id<=playbooks.id) WHERE agent_id IN (SELECT agent_id FROM playbooks GROUP BY agent_id, version
    HAVING COUNT(*) > 1);
CREATE UNIQUE INDEX IF NOT EXISTS idx_playbooks_version ON playbooks(agent_id, version)
""")]
PAID_PERIOD_DAYS = 30  # one payment buys this many days of a paid plan

# Starting hypotheses for a freemium plan, not validated prices. Enforced server-side in sia/api.py.
PLANS = {
    "free": {"projects": 1, "runs_per_month": 10_000, "examples_per_dataset": 200, "retention_days": 14,
             "overage": False, "llm_calls_per_month": 500},
    "pro": {"projects": 10, "runs_per_month": 250_000, "examples_per_dataset": 5_000, "retention_days": 90,
            "overage": True, "hard_cap": 2.0, "llm_calls_per_month": 20_000},
    "team": {"projects": 100, "runs_per_month": 2_000_000, "examples_per_dataset": 50_000, "retention_days": 365,
             "overage": True, "hard_cap": 2.0, "llm_calls_per_month": 200_000},
}
# hard_cap: paid plans may go past their included runs up to this multiple. Overage isn't billed during the beta,
# so without a cap a paid plan would be unlimited.
KEY_SCOPES = ("full", "ingest")  # ingest: send runs and feedback, read the active prompt; nothing else



def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


class Store:
    def __init__(self, path="sia.db"):
        self.path = path
        migrate(path, MIGRATIONS)

    @contextmanager
    def _conn(self, write=True):
        """One connection per call: commit on success, roll back on error, always close. Writes take the lock up
        front (BEGIN IMMEDIATE, like DevStore), so read-then-write sequences such as 'next version' can't race."""
        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as c:
            c.row_factory = sqlite3.Row
            c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def q(self, sql, *args):
        with self._conn(write=False) as c:
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

    def delete_org(self, tenant_id):
        """Delete an organization and everything it owns, in one transaction. Usage and payment rows are kept for
        the books but anonymised: they no longer point at the organization or its projects."""
        from sia_devtools.store import DevStore
        with self._conn() as c:
            if not c.execute("SELECT 1 FROM tenants WHERE id=?", (tenant_id,)).fetchone():
                return False
            for (pid,) in c.execute("SELECT id FROM projects WHERE org_id=?", (tenant_id,)).fetchall():
                DevStore.purge_project(c, pid)
            c.execute("UPDATE usage SET tenant_id='deleted', agent_id=NULL, project_id=NULL WHERE tenant_id=?",
                      (tenant_id,))
            c.execute("UPDATE payments SET org_id='deleted' WHERE org_id=?", (tenant_id,))
            for table in ("api_keys", "access_requests"):
                c.execute(f"DELETE FROM {table} WHERE org_id=?", (tenant_id,))
            c.execute("DELETE FROM tenants WHERE id=?", (tenant_id,))
        return True

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

    def llm_calls_since(self, tenant_id, since):
        return self.q("SELECT COALESCE(SUM(llm_calls), 0) n FROM usage WHERE tenant_id=? AND created>=?",
                      tenant_id, since)[0]["n"]

    def units_since(self, tenant_id, action, since):
        return self.q("SELECT COALESCE(SUM(units), 0) n FROM usage WHERE tenant_id=? AND action=? AND created>=?",
                      tenant_id, action, since)[0]["n"]
