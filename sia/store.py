"""SQLite persistence: tenants, agents, knowledge (FTS5), lessons (FTS5), playbook versions, episodes, eval examples."""
import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager

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
CREATE INDEX IF NOT EXISTS idx_agents_tenant ON agents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_lessons_agent ON lessons(agent_id, active);
CREATE INDEX IF NOT EXISTS idx_episodes_agent ON episodes(agent_id, created);
CREATE INDEX IF NOT EXISTS idx_examples_agent ON examples(agent_id);
CREATE INDEX IF NOT EXISTS idx_playbooks_agent ON playbooks(agent_id, active);
"""

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
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

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

    # --- tenants -------------------------------------------------------------
    def create_tenant(self, name):
        key = "sia_" + secrets.token_urlsafe(32)
        tid = uuid.uuid4().hex
        self.x("INSERT INTO tenants VALUES (?,?,?,?)", tid, name, hash_key(key), time.time())
        return {"tenant_id": tid, "name": name, "api_key": key}

    def tenant_by_key(self, key):
        rows = self.q("SELECT id, name FROM tenants WHERE key_hash=?", hash_key(key))
        return rows[0] if rows else None

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
    def add_example(self, agent_id, input, expected, source):
        return self.x("INSERT INTO examples (agent_id, input, expected, source, created) VALUES (?,?,?,?,?)",
                      agent_id, input, expected, source, time.time())

    def examples(self, agent_id, limit=None):
        """Eval set, real data first: user-provided, then feedback corrections, then synthetic study questions."""
        sql = """SELECT * FROM examples WHERE agent_id=? ORDER BY
                 CASE source WHEN 'user' THEN 0 WHEN 'feedback' THEN 1 ELSE 2 END, random()"""
        return self.q(sql + (" LIMIT ?" if limit else ""), agent_id, *([limit] if limit else []))

    def delete_example(self, agent_id, example_id):
        return bool(self.n("DELETE FROM examples WHERE id=? AND agent_id=?", example_id, agent_id))

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
            "eval_examples": count("examples"),
            "playbook_version": pb["version"],
            "playbook_score": pb["score"],
            "episodes_since_last_improve": since,
            "improve_recommended": since >= 10,
        }
