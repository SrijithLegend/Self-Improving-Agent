"""The self-improvement loop.

An agent improves the way a person does, at three timescales:
  1. Within a task   - draft, self-critique against a rubric, revise (run).
  2. Across tasks    - distil each episode into short reusable lessons; lessons that keep
                       coinciding with good outcomes rise, ones that coincide with failures are retired.
  3. Consolidation   - periodically rewrite the agent's playbook (its strategy prompt) from the
                       lessons and failures, and adopt it ONLY if it scores higher on a held eval set (improve).
Learning signals: self-critique, human feedback/corrections, and self-study on supplied documents.
"""
from statistics import mean, stdev

from .llm import LLMError
from .store import Store

PASS_SCORE = 0.75          # critique score (0..1) that counts as "good enough"
ADOPT_MARGIN = 0.02        # a new playbook must beat the current one by this much on the eval set...
                           # ...and by more than the noise (one standard error of the per-example gains)

DEFAULT_RUBRIC = ("Correctness and factual accuracy; completeness for what was asked; "
                  "grounded in the reference knowledge when relevant (no invented facts); "
                  "clear, actionable, appropriately concise.")


def build_system(agent, playbook, lessons, knowledge):
    parts = [f"You are an expert agent specialized in: {agent['domain']}."]
    if agent["instructions"]:
        parts.append(agent["instructions"])
    if playbook:
        parts.append("## Your playbook (strategy refined from experience)\n" + playbook)
    if lessons:
        parts.append("## Lessons learned from past tasks\n" + "\n".join(f"- {l['text']}" for l in lessons))
    if knowledge:
        parts.append("## Reference knowledge\n" + "\n\n".join(
            f"[{i}] ({k['title']}) {k['text']}" for i, k in enumerate(knowledge, 1)))
        parts.append("Use the reference knowledge when relevant.")
    parts.append("If information you need is missing, say so instead of inventing it.")
    return "\n\n".join(parts)


class Agent:
    def __init__(self, store: Store, llm, agent: dict):
        self.store, self.llm, self.a = store, llm, agent

    # --- 1. within a task ----------------------------------------------------
    def _context(self, task, playbook=None):
        knowledge = self.store.search_chunks(self.a["id"], task)
        lessons = self.store.relevant_lessons(self.a["id"], task)
        if playbook is None:
            playbook = self.store.active_playbook(self.a["id"])["content"]
        return build_system(self.a, playbook, lessons, knowledge), lessons, knowledge

    def critique(self, task, answer, knowledge=(), expected=None):
        ref = "\n".join(f"- {k['text'][:600]}" for k in knowledge)
        prompt = (f"TASK:\n{task}\n\nANSWER TO REVIEW:\n{answer}\n\n"
                  + (f"REFERENCE ANSWER (treat as ground truth):\n{expected}\n\n" if expected else "")
                  + (f"REFERENCE KNOWLEDGE:\n{ref}\n\n" if ref else "")
                  + f"RUBRIC:\n{self.a['rubric'] or DEFAULT_RUBRIC}\n\n"
                  'Return {"score": <0-10>, "issues": [<specific problems>], "suggestion": "<how to fix>"}')
        v = self.llm.json(f"You are a strict, fair senior reviewer in {self.a['domain']}. "
                          "Score harshly: 10 means nothing could be improved.", prompt, temperature=0)
        if not isinstance(v, dict):
            v = {}
        try:
            score = max(0.0, min(1.0, float(v.get("score", 0)) / 10))
        except (TypeError, ValueError):
            score = 0.0
        return {"score": score, "issues": list(v.get("issues") or [])[:8], "suggestion": str(v.get("suggestion", ""))}

    def run(self, task, learn=True, max_revisions=2, expected=None):
        system, lessons, knowledge = self._context(task)
        answer = self.llm.text(system, task)
        if not learn:
            return {"answer": answer, "score": None, "episode_id": None, "attempts": 1, "critique": None,
                    "lessons_used": [l["text"] for l in lessons], "new_lessons": []}

        draft = answer
        verdict = self.critique(task, answer, knowledge, expected)
        best, attempts = (verdict["score"], answer, verdict), 1
        while best[0] < PASS_SCORE and attempts <= max_revisions:
            answer = self.llm.text(system, (
                f"{task}\n\n---\nYour previous answer:\n{best[1]}\n\nReviewer feedback:\n"
                + "\n".join(f"- {i}" for i in best[2]["issues"]) + f"\nSuggestion: {best[2]['suggestion']}\n\n"
                "Write an improved, complete answer to the original task. Output only the answer."))
            verdict = self.critique(task, answer, knowledge, expected)
            attempts += 1
            if verdict["score"] > best[0]:
                best = (verdict["score"], answer, verdict)
        score, answer, verdict = best

        new = self.reflect(task, draft, answer, verdict, attempts)
        ids = [l["id"] for l in lessons]
        self.store.credit_lessons(ids, score)
        eid = self.store.add_episode(self.a["id"], task, answer, score, verdict, attempts, ids)
        return {"answer": answer, "score": round(score, 3), "episode_id": eid, "attempts": attempts,
                "critique": verdict, "lessons_used": [l["text"] for l in lessons], "new_lessons": new}

    # --- 2. across tasks -----------------------------------------------------
    def reflect(self, task, draft, final, verdict, attempts):
        """Distil the episode into reusable lessons. Nothing to learn from a perfect first draft."""
        if verdict["score"] >= 0.95 and attempts == 1:
            return []
        prompt = (f"TASK:\n{task}\n\nFIRST DRAFT:\n{draft[:3000]}\n\n"
                  + (f"REVISED ANSWER:\n{final[:3000]}\n\n" if final != draft else "")
                  + f"REVIEW (score {verdict['score']:.2f}): {verdict['issues']} {verdict['suggestion']}\n\n"
                  "Extract at most 2 lessons that would make FUTURE answers in this domain better. Each lesson: "
                  "one imperative sentence under 30 words, general enough to apply to other tasks, not a "
                  'restatement of this task\'s facts. Return {"lessons": [..]} (empty list if nothing general).')
        return self._save_lessons(prompt, "reflection")

    def _save_lessons(self, prompt, source):
        try:
            out = self.llm.json(f"You are a coach extracting durable know-how for an expert in {self.a['domain']}.",
                                prompt)
        except LLMError:
            return []  # learning is best-effort; never fail the user's task because reflection misfired
        lessons = out.get("lessons") if isinstance(out, dict) else None
        saved = []
        for text in (lessons if isinstance(lessons, list) else [])[:2]:
            if isinstance(text, str) and self.store.add_lesson(self.a["id"], text, source):
                saved.append(text.strip())
        return saved

    def feedback(self, episode_id, rating, correction=None, comment=None):
        """Human feedback is the strongest signal: re-credit lessons, learn from corrections, grow the eval set."""
        ep = self.store.episode(self.a["id"], episode_id)
        if not ep:
            raise KeyError("episode not found")
        self.store.set_feedback(episode_id, rating, correction or comment)
        self.store.credit_lessons(ep["lesson_ids"], (rating - 1) / 4)
        new = []
        if correction:
            self.store.add_example(self.a["id"], ep["task"], correction, "feedback")
        if correction or comment or rating <= 2:
            new = self._save_lessons(
                f"TASK:\n{ep['task']}\n\nAGENT ANSWER:\n{ep['answer'][:3000]}\n\nUSER RATING: {rating}/5\n"
                + (f"USER'S CORRECT ANSWER:\n{correction[:3000]}\n" if correction else "")
                + (f"USER COMMENT: {comment}\n" if comment else "")
                + '\nWhat should the agent do differently next time? Return {"lessons": [..]} with at most 2 '
                  "general, imperative lessons under 30 words each.", "feedback")
        return {"episode_id": episode_id, "new_lessons": new}

    def study(self, rounds=3):
        """Self-study: quiz itself on its own documents, grade against the source, learn from mistakes.
        Each quiz question is also kept as an eval example so improve() has something to measure against."""
        chunks = self.store.random_chunks(self.a["id"], rounds)
        if not chunks:
            raise ValueError("No knowledge to study. Add documents first.")
        results = []
        for c in chunks:
            qa = self.llm.json(
                f"You write exam questions for practitioners of {self.a['domain']}.",
                f"SOURCE ({c['title']}):\n{c['text']}\n\nWrite one realistic task or question a user in this domain "
                "might ask that this source answers, plus the ideal answer based strictly on the source. "
                'Return {"question": "...", "answer": "..."}', temperature=0.8)
            if not isinstance(qa, dict):
                continue
            q, expected = str(qa.get("question", "")).strip(), str(qa.get("answer", "")).strip()
            if not q or not expected:
                continue
            self.store.add_example(self.a["id"], q, expected, "synthetic")
            r = self.run(q, expected=expected)
            results.append({"question": q, "score": r["score"], "new_lessons": r["new_lessons"]})
        return {"studied": len(results), "results": results}

    # --- 3. consolidation ----------------------------------------------------
    def scores(self, playbook, examples, trials=1):
        """Per-example score with this playbook, averaged over `trials` answers (grading is at temperature 0)."""
        out = []
        for ex in examples:
            system, _, knowledge = self._context(ex["input"], playbook=playbook)
            runs = []
            for _ in range(trials):
                answer = self.llm.text(system, ex["input"], temperature=0.3)
                runs.append(self.critique(ex["input"], answer, knowledge, ex["expected"])["score"])
            out.append(mean(runs))
        return out

    def evaluate(self, playbook, examples, trials=1):
        return mean(self.scores(playbook, examples, trials))

    def improve(self, max_examples=8, trials=1):
        examples = self.store.examples(self.a["id"], max_examples)
        if not examples:
            raise ValueError("No eval examples. Add examples, give feedback with corrections, or run study first.")
        current = self.store.active_playbook(self.a["id"])
        lessons = self.store.top_lessons(self.a["id"], 25)
        failures = [e for e in self.store.episodes(self.a["id"], 40) if (e["score"] or 0) < PASS_SCORE or
                    (e["rating"] or 5) <= 2][:10]
        evidence = "\n".join(
            f"- Task: {e['task'][:200]} | issues: {(e['critique'] or {}).get('issues')} | user: {e['correction'] or ''}"
            for e in failures)
        candidate = self.llm.text(
            f"You are improving the operating playbook of an AI expert in {self.a['domain']}.",
            f"CURRENT PLAYBOOK:\n{current['content'] or '(none yet)'}\n\n"
            "LESSONS LEARNED (most useful first):\n" + "\n".join(f"- {l['text']}" for l in lessons)
            + f"\n\nRECENT FAILURES:\n{evidence or '(none)'}\n\n"
            "Write the improved playbook: a concise set of principles, procedures and checks (markdown bullets, "
            "under 350 words) that consolidates the lessons, fixes the failure patterns and keeps what already "
            "works. Output only the playbook.", temperature=0.5).strip()

        old = self.scores(current["content"], examples, trials)
        new = self.scores(candidate, examples, trials)
        old_score, new_score = mean(old), mean(new)
        adopted = significant_gain(old, new)
        version = self.store.add_playbook(self.a["id"], candidate, round(new_score, 3), adopted,
                                          note=f"baseline {old_score:.3f} on {len(examples)} examples")
        if not adopted and current["version"]:
            # keep the active version's recorded score current against today's eval set
            self.store.x("UPDATE playbooks SET score=? WHERE agent_id=? AND active=1", round(old_score, 3), self.a["id"])
        return {"adopted": adopted, "old_score": round(old_score, 3), "new_score": round(new_score, 3),
                "candidate_version": version, "active_version": version if adopted else current["version"],
                "eval_examples": len(examples), "trials": trials, "playbook": candidate}


def significant_gain(old, new):
    """Paired comparison on the same examples: the mean gain must clear ADOPT_MARGIN and one standard error."""
    gains = [n - o for o, n in zip(old, new)]
    noise = stdev(gains) / len(gains) ** 0.5 if len(gains) > 1 else 0.0
    return mean(gains) > max(ADOPT_MARGIN, noise)
