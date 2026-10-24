"""Deprecated: the managed-agents API (/v1/agents) as a thin wrapper over kind="agent" projects.

An agent is now a project with settings.kind = "agent" (a migrated agent keeps its id). Everything it learns goes
through the self-improving loop (sia_loop) and its gate:
  run       -> sia_loop.actor.perform with the active agent_config; reflection adds lessons in probation only
  feedback  -> pending feedback, and a pending example for a correction; nothing changes until it is verified
  verify    -> a verified example, and probation lessons from it
  study / evaluate / improve -> background jobs (`sia worker` runs them)
This module and /v1/agents are removed in a later release (SELF_IMPROVING_SPEC.md phase 7): use /v1/projects.
"""
import re
import uuid

from sia_devtools.errors import NotFound, ValidationError
from sia_loop import actor, config, jobs, memory, models

DATASET = "eval"


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48] or "agent"


def create(dev, org, name, domain, instructions="", rubric="", by=None, max_projects=None):
    """A new kind="agent" project with agent_config@1 (the instructions as its system prompt) approved and active."""
    taken = {p["name"] for p in dev.projects(org)}
    if max_projects is not None and len(taken) >= max_projects:
        from sia_devtools.errors import QuotaExceeded
        raise QuotaExceeded(f"This plan includes {max_projects} project(s); a hosted agent is a project.")
    pname = _slug(name)
    if pname in taken:
        pname = f"{pname}-{uuid.uuid4().hex[:6]}"
    p = dev.create_project(org, pname, {"kind": "agent", "agent": {"name": name, "domain": domain,
                                                                   "instructions": instructions, "rubric": rubric}},
                           actor=by)
    v = dev.create_version(org, p["id"], config.dumps(config.default_config(system_prompt=instructions)),
                           name=config.CONFIG_NAME, note="initial config", actor=by)
    dev.approve(org, p["id"], config.CONFIG_NAME, v["version"], actor=by)
    dev.activate(org, p["id"], config.CONFIG_NAME, v["version"], actor=by)
    return Agent(dev, org, p["id"]).shape()


def agents(dev, org):
    return [Agent(dev, org, p["id"], project=p).shape() for p in dev.projects(org) if p["settings"]["kind"] == "agent"]


class Agent:
    def __init__(self, dev, org, agent_id, key=None, project=None):
        """key: the caller's model key for the agent's provider (None: the server-side key)."""
        self.dev, self.org, self.id, self.key = dev, org, agent_id, key
        try:
            self.p = project or dev.project(org, agent_id)
        except (NotFound, ValidationError):
            raise NotFound("Agent not found.")
        if self.p["settings"]["kind"] != "agent":
            raise NotFound("Agent not found.")
        self.info = self.p["settings"]["agent"] or {}

    def shape(self):
        return {"id": self.id, "tenant_id": self.org, "name": self.info.get("name") or self.p["name"],
                "domain": self.info.get("domain", ""), "instructions": self.info.get("instructions", ""),
                "rubric": self.info.get("rubric", ""), "created": self.p["created"], "project": self.p["name"]}

    def _active(self):
        v = self.dev.active_version(self.org, self.id, config.CONFIG_NAME)
        if v is None:
            raise ValidationError("This agent has no active agent_config.")
        return v

    def _llm(self):
        return models.for_config(config.parse(self._active()["content"]), self.key)

    def _dataset(self):
        return self.dev.create_dataset(self.org, self.id, DATASET, actor="sia-loop")

    # --- profile -------------------------------------------------------------------------------------------------
    def update(self, by=None, **fields):
        """Edit the profile. New instructions become the system prompt of a new agent_config version, approved and
        activated by this human edit (with the reason in the audit log)."""
        agent = {**self.info, **{k: v for k, v in fields.items() if v is not None}}
        self.dev.update_project(self.org, self.id, {"agent": agent}, actor=by)
        if fields.get("instructions") is not None and fields["instructions"] != self.info.get("instructions"):
            active = self._active()
            cfg = {**config.parse(active["content"]), "system_prompt": fields["instructions"]}
            v = self.dev.create_version(self.org, self.id, config.dumps(cfg), name=config.CONFIG_NAME,
                                        parent_version=active["version"], note="instructions edited", actor=by)
            self.dev.approve(self.org, self.id, config.CONFIG_NAME, v["version"],
                             override_reason="instructions edited by a person through PATCH /v1/agents", actor=by)
            self.dev.activate(self.org, self.id, config.CONFIG_NAME, v["version"], actor=by)
        self.__init__(self.dev, self.org, self.id, self.key)
        return self.shape()

    def stats(self):
        runs = self.dev.export_runs(self.org, self.id, name="agent.run", root_only=True)
        scores = [r["scores"]["self_critique"] for r in runs if "self_critique" in (r["scores"] or {})]
        avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
        half = min(20, len(scores) // 2)
        active = self.dev.active_version(self.org, self.id, config.CONFIG_NAME)
        ds = self.dev.datasets(self.org, self.id, DATASET)
        examples = self.dev.dataset(self.org, self.id, ds[0]["id"], include_pending=True)["examples"] if ds else []
        since = [r for r in runs if active and r["started"] > (active.get("decided") or 0)]
        return {"episodes": len(scores), "avg_score_first_20": avg(scores[:half]),
                "avg_score_last_20": avg(scores[len(scores) - half:] if half else []),
                "active_lessons": len(self.dev.lessons(self.org, self.id, "pinned")),
                "probation_lessons": len(self.dev.lessons(self.org, self.id, "probation")),
                "documents": len(self.dev.knowledge_docs(self.org, self.id)),
                "eval_examples": sum(e["status"] == "verified" for e in examples),
                "pending_examples": sum(e["status"] == "pending" for e in examples),
                "playbook_version": active["version"] if active else 0, "playbook_score": None,
                "episodes_since_last_improve": len(since), "improve_recommended": len(since) >= 10}

    def playbook(self):
        return config.parse(self._active()["content"])["playbook"]

    # --- work and feedback -----------------------------------------------------------------------------------------
    def run(self, task, learn=True):
        out = actor.perform(self.dev, self.org, self.id, task, key=self.key, revise=learn, learn=learn)
        return {"answer": out["answer"], "score": out["self_critique"], "episode_id": out["run_id"],
                "attempts": out["attempts"], "critique": out["critique"], "lessons_used": out["lessons_used"],
                "new_lessons": out["new_lessons"], "llm_calls": out["llm_calls"],
                **({"learning_error": out["error"]} if out.get("error") else {})}

    def feedback(self, episode_id, rating, correction=None, comment=None):
        """A 1-5 rating becomes good (4-5) or bad feedback, stored as pending. A correction also becomes a pending
        example: user text can be wrong or malicious, so it is never tested or learned from until verified."""
        run = self.dev.run(self.org, self.id, episode_id)
        self.dev.add_feedback(self.org, self.id, episode_id, 1 if rating >= 4 else -1, correction=correction,
                              category=f"rating:{rating}", explanation=comment)
        pending = None
        if correction:
            task = (run["input"] or {}).get("task") if isinstance(run["input"], dict) else run["input"]
            pending = self.dev.add_examples(self.org, self.id, self._dataset()["id"], [
                {"input": task, "expected_output": correction}], status="pending", source="feedback",
                source_run_id=episode_id)["ids"][0]
        return {"episode_id": episode_id, "pending_example_id": pending, "new_lessons": [], "llm_calls": 0}

    # --- examples --------------------------------------------------------------------------------------------------
    def add_example(self, input, expected):
        return self.dev.add_examples(self.org, self.id, self._dataset()["id"], [
            {"input": input, "expected_output": expected}])["ids"][0]

    def examples(self, include_unverified=False):
        ds = self.dev.datasets(self.org, self.id, DATASET)
        if not ds:
            return []
        rows = self.dev.dataset(self.org, self.id, ds[0]["id"], include_pending=include_unverified)["examples"]
        return [{"id": e["id"], "input": e["input"], "expected": e["expected_output"], "source": e["source"],
                 "status": e["status"], "split": e["split"], "created": e["created"]} for e in rows]

    def verify_example(self, example_id):
        """Accept a pending example: it joins the tests (in its fixed split) and the agent takes lessons from it, in
        probation."""
        did = self._dataset()["id"]
        ex = self.dev.example_row(self.org, self.id, did, example_id)
        new_id = self.dev.review_example(self.org, self.id, did, example_id, "verified")
        answer = None
        if ex.get("source_run_id"):
            try:
                answer = self.dev.run(self.org, self.id, ex["source_run_id"])["output"]
            except NotFound:
                pass
        llm = self._llm()
        new = memory.learn_from_correction(self.dev, self.org, self.id, llm, ex["input"], answer,
                                           ex["expected_output"], self.info.get("domain", ""))
        return {"example_id": new_id, "status": "verified", "new_lessons": new, "llm_calls": llm.calls}

    def reject_example(self, example_id):
        self.dev.review_example(self.org, self.id, self._dataset()["id"], example_id, "rejected")
        return {"id": example_id, "status": "rejected"}

    def delete_example(self, example_id):
        self.dev.delete_example(self.org, self.id, self._dataset()["id"], example_id)

    # --- background work -------------------------------------------------------------------------------------------
    def study(self, rounds=3):
        return self._job("study", {"rounds": rounds})

    def evaluate(self, trials=1):
        return self._job("evaluate", {"trials": trials})

    def improve(self, trials=None, activate=False, by=None):
        """One improvement cycle. activate=True releases the winner at once, but only if it passes the holdout and
        every promotion rule; otherwise it stays a candidate for a person to decide."""
        payload = {"activate": bool(activate), "requested_by": by}
        return self._job("cycle", payload)

    def _job(self, kind, payload):
        j = jobs.enqueue(self.dev, self.org, self.id, kind, payload, unique=kind == "cycle")
        return {"job_id": j["id"], "status": j["status"], "kind": kind, "llm_calls": 0}

    # --- memory and history ----------------------------------------------------------------------------------------
    def lessons(self, include_inactive=False):
        util = memory.utilities(self.dev, self.org, self.id)
        rows = [lesson for lesson in self.dev.lessons(self.org, self.id)
                if include_inactive or lesson["state"] != "retired"]
        return [{**lesson, "active": lesson["state"] != "retired",
                 "utility": round(util.get(lesson["id"], {}).get("utility", memory.PRIOR), 3)} for lesson in rows]

    def set_lesson(self, lesson_id, enable, by=None):
        self.dev.set_lesson_state(self.org, self.id, lesson_id, "probation" if enable else "retired", by)
        return {"id": lesson_id, "active": bool(enable)}

    def episodes(self, limit=50):
        runs = self.dev.runs(self.org, self.id, limit=limit, root_only=True, name="agent.run")["items"]
        fb = {}
        for f in self.dev.feedback_list(self.org, self.id, limit=200)["items"]:
            fb.setdefault(f["run_id"], f)
        return [{"id": r["id"], "task": (r["input"] or {}).get("task") if isinstance(r["input"], dict) else r["input"],
                 "answer": r["output"], "score": (r["scores"] or {}).get("self_critique"),
                 "attempts": (r["metadata"] or {}).get("attempts"), "version": r["version"],
                 "rating": (fb.get(r["id"]) or {}).get("category"), "created": r["started"]} for r in runs]

    def playbooks(self):
        return [{"version": v["version"], "content": config.parse(v["content"])["playbook"], "score": None,
                 "active": v["active"], "status": v["status"], "note": v["note"], "created": v["created"]}
                for v in self.dev.versions(self.org, self.id, config.CONFIG_NAME)]

    def activate_playbook(self, version, by=None):
        """Roll back (or forward) to an approved agent_config version."""
        self.dev.activate(self.org, self.id, config.CONFIG_NAME, version, actor=by)
        return {"active_version": version}
