"""Memory: how the agent learns, without letting anything it learns reach production unchecked.

Lessons: reflection after a task and lessons from verified corrections are stored in *probation*. A probation lesson is
never in a production prompt. The optimizer may pin it into a candidate config, and it reaches production only if
that config passes the promotion gate. Before a lesson is stored it is scrubbed: anything that reads like an
instruction override, contains a URL, an email or code, or mostly repeats one task's text is dropped. That last one is a
memorised fact, not a lesson.

Utility comes from trusted signals only: validator results, outcome signals and verified feedback on runs whose config
pinned the lesson. The actor's self-critique is never counted.

Failure clusters group recent failures by a cheap key. They are the optimizer's evidence, and they never include
anything from the holdout split (G2).
"""
import json
import re
from collections import Counter, defaultdict

from sia.llm import LLMError
from sia_devtools.scoring import as_text
from sia_devtools.store import tokens

INJECTION_RX = re.compile(r"\bignore\b|\bdisregard\b|system prompt|you must always|https?://|\bwww\.|"
                          r"[\w.+-]+@[\w-]+\.[\w.]+|```|<\s*/?\s*(?:script|system)\b", re.I)
MAX_OVERLAP = 0.4                     # share of a lesson's words that may come from the one task it was learned from
PRIOR, PRIOR_WEIGHT = 0.7, 2          # a lesson with no trusted record yet has a mildly optimistic utility
LOW_UTILITY, MIN_USES = 0.45, 5       # unpin lessons below this after this many trusted uses


def lesson_problem(text, task_text=""):
    """Why a proposed lesson must not be stored, or None."""
    if not isinstance(text, str) or not 8 <= len(text.strip()) <= 300:
        return "a lesson is one sentence of 8-300 characters"
    if INJECTION_RX.search(text):
        return "it reads like an instruction override, or contains a URL, an email or code"
    words, task = tokens(text), tokens(task_text or "")
    if words and len(words & task) / len(words) > MAX_OVERLAP:
        return "it repeats the task instead of generalising from it"
    return None


def save_lessons(dev, org, pid, raw, source, task_text=""):
    """Store up to 2 proposed lessons in probation. Returns [(text, id)] of those kept."""
    saved = []
    for text in (raw if isinstance(raw, list) else [])[:2]:
        if isinstance(text, str) and not lesson_problem(text, task_text):
            if lid := dev.add_lesson(org, pid, text.strip(), source):
                saved.append((text.strip(), lid))
    return saved


def reflect(dev, org, pid, llm, task, draft, answer, critique, attempts, domain=""):
    """After a production task: at most one call, and only probation lessons. Nothing to learn from a first draft the
    critic already liked."""
    if critique is None or (critique["score"] >= 0.95 and attempts == 1):
        return []
    task_text = as_text(task)
    try:
        out = llm.json(f"You are a coach extracting durable know-how for an expert in {domain or 'this domain'}.",
                       f"TASK:\n{task_text[:3000]}\n\nFIRST DRAFT:\n{as_text(draft)[:3000]}\n\n"
                       + (f"REVISED ANSWER:\n{as_text(answer)[:3000]}\n\n" if answer != draft else "")
                       + f"REVIEW (score {critique['score']:.2f}): {critique['issues']} {critique['suggestion']}\n\n"
                       "Extract at most 2 lessons that would make FUTURE answers in this domain better. Each lesson: "
                       "one imperative sentence under 30 words, general enough to apply to other tasks, not a "
                       'restatement of this task\'s facts. Return {"lessons": [..]} (empty list if nothing general).')
    except LLMError:
        return []  # learning is best effort; the user's task already succeeded
    return [t for t, _ in save_lessons(dev, org, pid, (out or {}).get("lessons") if isinstance(out, dict) else None,
                                       "reflection", task_text)]


def learn_from_correction(dev, org, pid, llm, task, answer, correct, domain=""):
    """A reviewer verified a correction: what should the agent do differently? Probation lessons only."""
    task_text = as_text(task)
    try:
        out = llm.json(f"You are a coach extracting durable know-how for an expert in {domain or 'this domain'}.",
                       f"TASK:\n{task_text[:3000]}\n\n" + (f"AGENT ANSWER:\n{as_text(answer)[:3000]}\n\n" if answer
                                                           else "")
                       + f"VERIFIED CORRECT ANSWER:\n{as_text(correct)[:3000]}\n\nWhat should the agent do differently "
                         'next time? Return {"lessons": [..]} with at most 2 general, imperative lessons under 30 '
                         "words each.")
    except LLMError:
        return []
    return [t for t, _ in save_lessons(dev, org, pid, (out or {}).get("lessons") if isinstance(out, dict) else None,
                                       "feedback", task_text)]


# --- utility from trusted signals ---------------------------------------------------------------------------------
def trusted_score(run, rating=None):
    """The mean of a run's trusted signals (validators, outcome, verified rating), or None if it has none."""
    s = run.get("scores") or {}
    vals = [float(v) for k, v in s.items() if k.startswith("validator:")]
    if "outcome" in s:
        vals.append(float(s["outcome"]))
    if rating is not None:
        vals.append(1.0 if rating > 0 else 0.0)
    return sum(vals) / len(vals) if vals else None


def utilities(dev, org, pid, limit=2000):
    """{lesson_id: {"utility", "uses"}} from trusted signals on recent production runs that pinned the lesson."""
    import sqlite3
    from contextlib import closing
    configs, credit = {}, defaultdict(lambda: [0.0, 0])
    verified = {}
    with closing(sqlite3.connect(dev.path, timeout=30)) as c:
        c.row_factory = sqlite3.Row
        for f in c.execute("SELECT run_id, rating FROM feedback WHERE project_id=? AND status='verified'", (pid,)):
            verified[f["run_id"]] = f["rating"]
        for v in c.execute("SELECT version, content FROM versions WHERE project_id=? AND name='agent_config'",
                           (pid,)):
            try:
                configs[v["version"]] = json.loads(v["content"]).get("lessons", [])
            except (ValueError, AttributeError):
                configs[v["version"]] = []
        rows = c.execute(f"""SELECT id, scores, version FROM runs WHERE project_id=? AND parent_id IS NULL AND
            version_name='agent_config' AND NOT EXISTS (SELECT 1 FROM json_each(runs.tags) WHERE value='eval')
            ORDER BY started DESC LIMIT {int(limit)}""", (pid,)).fetchall()
    for r in rows:
        score = trusted_score({"scores": json.loads(r["scores"] or "{}")}, verified.get(r["id"]))
        if score is None:
            continue
        for lid in configs.get(r["version"], []):
            credit[lid][0] += score
            credit[lid][1] += 1
    out = {}
    for lesson in dev.lessons(org, pid):
        total, uses = credit.get(lesson["id"], (0.0, 0))
        out[lesson["id"]] = {"utility": (total + PRIOR * PRIOR_WEIGHT) / (uses + PRIOR_WEIGHT), "uses": uses}
    return out


# --- failure clusters -----------------------------------------------------------------------------------------------
def _words(x, n=6):
    return " ".join(re.findall(r"\w+", as_text(x).casefold())[:n])


def failure_clusters(dev_results, live_runs=(), corrections=(), holdout_texts=frozenset(), top=10):
    """Group failures by scorer + the first 6 words of the expected output + validator name.

    dev_results: eval results on the DEV split, [{input, expected_output, output, scores, passed}]
    live_runs:   recent production runs with scores (validator failures, negative outcomes)
    corrections: verified feedback, [{run_input, correction, category}]
    holdout_texts: normalised inputs of every holdout example. Any evidence about one of them is dropped (G2), even
                   when it arrives through production traffic or feedback.
    Returns the top clusters: {"key", "kind", "count", "examples": up to 3 of {input, expected, got}}.
    """
    norm = lambda x: " ".join(as_text(x).split()).casefold()  # noqa: E731
    clusters, examples = Counter(), defaultdict(list)

    def add(key, kind, item):
        if norm(item["input"]) in holdout_texts:
            return
        clusters[(key, kind)] += 1
        if len(examples[(key, kind)]) < 3:
            examples[(key, kind)].append({k: as_text(v)[:600] for k, v in item.items()})

    for r in dev_results:
        if r.get("split", "dev") != "dev":
            raise ValueError("failure_clusters only takes dev-split results")
        if not r["passed"]:
            failed = sorted(k for k, v in (r.get("scores") or {}).items() if v < 1) or ["error"]
            add(f"{'+'.join(failed)}: {_words(r['expected_output'])}", "quality",
                {"input": r["input"], "expected": r["expected_output"], "got": r.get("output")})
    for run in live_runs:
        s = run.get("scores") or {}
        for name, v in s.items():
            if name.startswith("validator:") and v == 0:
                add(name, "format", {"input": run.get("input"), "expected": f"passes {name}", "got": run.get("output")})
        if s.get("outcome") == 0:
            add("outcome: negative", "outcome", {"input": run.get("input"), "expected": "a good outcome",
                                                 "got": run.get("output")})
        if run.get("tool_errors"):
            add("tool errors", "tools", {"input": run.get("input"), "expected": "tool calls that work",
                                         "got": run["tool_errors"]})
    for f in corrections:
        add(f"correction: {f.get('category') or _words(f.get('correction'))}", "quality",
            {"input": f.get("run_input"), "expected": f.get("correction"), "got": f.get("run_output")})
    return [{"key": k, "kind": kind, "count": n, "examples": examples[(k, kind)]}
            for (k, kind), n in clusters.most_common(top)]
