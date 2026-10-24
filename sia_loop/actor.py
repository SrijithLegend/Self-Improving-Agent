"""The actor: runs one task with one agent_config.

A provider-neutral JSON action loop. Each turn the model returns {"tool": ..., "args": ...} or {"final": ...}, so it
works on every provider sia.llm supports, without native function calling. After the loop, the config's
draft-critique-revise step may polish the answer (a runtime feature: its self-critique score is recorded, but the
evaluator and the promoter never use it, G1), then the config's validators check the answer deterministically.
"""
import json
import re
import uuid
from dataclasses import dataclass, field

from sia.llm import BadJSON, LLMError
from sia_devtools.scoring import as_text

from .tools.macro import schema_errors
from .tools.registry import Registry

DEFAULT_RUBRIC = ("Correctness and factual accuracy; completeness for what was asked; grounded in the reference "
                  "knowledge when relevant (no invented facts); clear, actionable, appropriately concise.")
ACTION_PROTOCOL = ('Each turn, reply with exactly one JSON object: {"tool": "<name>", "args": {...}} to call a tool, '
                   'or {"final": "<your complete answer>"} when you are done.')
MAX_RESULT = 4000


@dataclass
class ActorContext:
    llm: object                   # .json(system, prompt, temperature=...), .text(...), .calls
    tools: Registry
    domain: str = ""
    rubric: str = ""
    lessons: dict = field(default_factory=dict)    # lesson id -> text, for the ids the config pins
    exemplars: list = field(default_factory=list)  # [{"input", "expected_output"}] for the config's exemplar ids
    revise: bool = True                            # run the config's self-critique loop


@dataclass
class ActorResult:
    answer: str
    transcript: list
    checks: dict
    calls: int
    attempts: int = 1
    self_critique: float | None = None
    critique: dict | None = None
    error: str | None = None
    sources: list = field(default_factory=list)
    draft: str | None = None      # the answer before any revision


def build_system(cfg, ctx, knowledge=()):
    parts = [f"You are an expert agent specialized in: {ctx.domain}."] if ctx.domain else []
    if cfg["system_prompt"]:
        parts.append(cfg["system_prompt"])
    if cfg["playbook"]:
        parts.append("## Your playbook (strategy refined from experience)\n" + cfg["playbook"])
    lessons = [ctx.lessons[i] for i in cfg["lessons"] if i in ctx.lessons]
    if lessons:
        parts.append("## Lessons learned from past tasks\n" + "\n".join(f"- {t}" for t in lessons))
    if ctx.exemplars:
        parts.append("## Worked examples\n" + "\n\n".join(
            f"Task: {as_text(e['input'])}\nAnswer: {as_text(e['expected_output'])}" for e in ctx.exemplars))
    if knowledge:
        parts.append("## Reference knowledge\n" + "\n\n".join(f"[{i}] ({k['title']}) {k['text']}"
                                                              for i, k in enumerate(knowledge, 1)))
        parts.append("Use the reference knowledge when relevant.")
    specs = ctx.tools.specs(cfg)
    if specs:
        parts.append("## Tools\n" + "\n".join(json.dumps(s, ensure_ascii=False) for s in specs))
    parts.append(ACTION_PROTOCOL if specs else 'Reply with one JSON object: {"final": "<your complete answer>"}.')
    parts.append("If information you need is missing, say so instead of inventing it.")
    return "\n\n".join(parts)


def render(task, transcript):
    out = f"TASK:\n{task}"
    if transcript:
        out += "\n\nTOOL RESULTS SO FAR:\n" + "\n".join(
            f"{i}. {s['tool']}({json.dumps(s['args'], ensure_ascii=False)}) -> {s['result']}"
            for i, s in enumerate(transcript, 1))
    return out


def run_validators(validators, answer):
    """{"validator:<name>": 1 or 0} for each validator. Deterministic and cheap: it runs on every answer."""
    text = as_text(answer)[:64_000]
    out = {}
    for v in validators:
        if v["type"] == "regex":
            ok = re.search(v["pattern"], text) is not None
        else:
            try:
                ok = not schema_errors(json.loads(text), v["schema"])
            except json.JSONDecodeError:
                ok = False
        out[f"validator:{v['name']}"] = int(ok)
    return out


def critique(ctx, task, answer, knowledge=()):
    """The actor's own reviewer (the lowest-trust signal: used only to decide whether to revise)."""
    ref = "\n".join(f"- {k['text'][:600]}" for k in knowledge)
    v = ctx.llm.json(f"You are a strict, fair senior reviewer in {ctx.domain or 'this domain'}. Score harshly: 10 "
                     "means nothing could be improved.",
                     f"TASK:\n{task}\n\nANSWER TO REVIEW:\n{answer}\n\n"
                     + (f"REFERENCE KNOWLEDGE:\n{ref}\n\n" if ref else "")
                     + f"RUBRIC:\n{ctx.rubric or DEFAULT_RUBRIC}\n\n"
                     'Return {"score": <0-10>, "issues": [<specific problems>], "suggestion": "<how to fix>"}',
                     temperature=0)
    v = v if isinstance(v, dict) else {}
    try:
        score = max(0.0, min(1.0, float(v.get("score", 0)) / 10))
    except (TypeError, ValueError):
        score = 0.0
    return {"score": score, "issues": [str(i) for i in list(v.get("issues") or [])[:8]],
            "suggestion": str(v.get("suggestion", ""))}


def revise_if_needed(cfg, ctx, system, task, answer, knowledge):
    """Draft, critique, revise while the critic scores the answer below pass_score. Best effort: if the critic
    fails, the answer we have is kept. Returns (answer, attempts, best critique or None, error or None)."""
    rv = cfg["revise"]
    if not ctx.revise or rv["max_revisions"] < 1:
        return answer, 1, None, None
    attempts, best = 1, None
    try:
        verdict = critique(ctx, task, answer, knowledge)
        best = (verdict["score"], answer, verdict)
        while best[0] < rv["pass_score"] and attempts <= rv["max_revisions"]:
            draft = ctx.llm.text(system, f"{task}\n\n---\nYour previous answer:\n{best[1]}\n\nReviewer feedback:\n"
                                 + "\n".join(f"- {i}" for i in best[2]["issues"])
                                 + f"\nSuggestion: {best[2]['suggestion']}\n\nWrite an improved, complete answer to "
                                   "the original task. Output only the answer.",
                                 temperature=cfg["model"]["temperature"])
            attempts += 1
            verdict = critique(ctx, task, draft, knowledge)
            if verdict["score"] > best[0]:
                best = (verdict["score"], draft, verdict)
    except LLMError as e:
        return (best[1] if best else answer), attempts, best[2] if best else None, str(e)[:300]
    return best[1], attempts, best[2], None


def run_task(cfg, task, ctx):
    """Answer one task with a validated config. Raises LLMError only if the model can't be reached at all."""
    task_text = as_text(task)
    calls0 = getattr(ctx.llm, "calls", 0)
    r = cfg["retrieval"]
    knowledge = ctx.tools.search(task_text, r["k"]) if r["enabled"] and r["k"] > 0 else []
    knowledge = [{**k, "text": k["text"][:r["max_chars_per_chunk"]]} for k in knowledge]
    system = build_system(cfg, ctx, knowledge)
    temperature = cfg["model"]["temperature"]
    transcript, answer, error = [], None, None
    for _ in range(cfg["tools"]["max_steps"]):
        try:
            act = ctx.llm.json(system, render(task_text, transcript), temperature=temperature)
        except BadJSON as e:  # the model answered in prose: ask once more for a plain final answer below
            error = str(e)[:300]
            break
        if not isinstance(act, dict):
            break
        if "final" in act:
            answer = act["final"] if isinstance(act["final"], str) else json.dumps(act["final"], ensure_ascii=False)
            break
        tool, args = str(act.get("tool"))[:64], act.get("args") or {}
        result = ctx.tools.call(tool, args, cfg)
        transcript.append({"tool": tool, "args": args if isinstance(args, dict) else {"_": str(args)[:200]},
                           "result": str(result)[:MAX_RESULT]})
    if answer is None:
        answer = ctx.llm.text(system, render(task_text, transcript) + "\n\nGive your final answer now, as plain "
                                                                     "text.", temperature=temperature)
    draft = answer
    answer, attempts, verdict, rev_error = revise_if_needed(cfg, ctx, system, task_text, answer, knowledge)
    return ActorResult(answer=answer, draft=draft, transcript=transcript, checks=run_validators(cfg["validators"], answer),
                       calls=getattr(ctx.llm, "calls", 0) - calls0, attempts=attempts,
                       self_critique=round(verdict["score"], 3) if verdict else None, critique=verdict,
                       error=rev_error or error, sources=[k["title"] for k in knowledge])


# --- wiring to a project's stored data ----------------------------------------------------------------------------
def perform(dev, org, pid, task, *, key=None, revise=True, learn=True, tags=(), run_id=None):
    """Run a production task on a kind="agent" project with its active agent_config, and record it as a run. With
    learn, a reflection afterwards may add lessons, in probation only (they never change this config).
    key: the caller's model key for the config's provider (None: the server-side key)."""
    from sia_devtools.errors import ValidationError

    from . import config, memory, models
    p = dev.project(org, pid)
    if p["settings"]["kind"] != "agent":
        raise ValidationError("Tasks run only on hosted agents (projects with settings.kind = 'agent').")
    rid = run_id or uuid.uuid4().hex
    active = dev.active_version(org, pid, config.CONFIG_NAME, unit=rid)  # a running canary gets its share by run id
    if active is None:
        raise ValidationError("This agent has no active agent_config version yet.")
    tags = [*tags, "canary"] if active.get("variant") == "canary" else list(tags)
    cfg = config.parse(active["content"])
    llm = models.for_config(cfg, key)
    ctx = context_for(dev, org, pid, cfg, llm, revise=revise)
    result = run_task(cfg, task, ctx)
    record(dev, org, pid, task, result, active["version"], tags=tags, run_id=rid)
    new = memory.reflect(dev, org, pid, llm, task, result.draft, result.answer, result.critique, result.attempts,
                         ctx.domain) if learn and revise else []
    return {"run_id": rid, "answer": result.answer, "version": active["version"], "variant": active["variant"],
            "checks": result.checks,
            "attempts": result.attempts, "self_critique": result.self_critique, "critique": result.critique,
            "tools_used": [s["tool"] for s in result.transcript], "sources": result.sources,
            "lessons_used": [ctx.lessons[i] for i in cfg["lessons"] if i in ctx.lessons], "new_lessons": new,
            "llm_calls": llm.calls, **({"error": result.error} if result.error else {})}
def context_for(dev, org, pid, cfg, llm, revise=True):
    """The actor context for a project: its lessons, exemplars, documents and tables, all scoped to it."""
    p = dev.project(org, pid)
    s, agent = p["settings"], p["settings"]["agent"] or {}
    pinned = set(cfg["lessons"])
    lessons = {lesson["id"]: lesson["text"] for lesson in dev.lessons(org, pid) if lesson["id"] in pinned}
    tools = Registry(search=lambda q, k: dev.search_knowledge(org, pid, q, k),
                     get_table=lambda name: dev.lookup_table(org, pid, name), http_allowlist=s["loop"]["http_allowlist"],
                     llm=llm)
    return ActorContext(llm=llm, tools=tools, domain=agent.get("domain", ""), rubric=agent.get("rubric", ""),
                        lessons=lessons, exemplars=dev.examples_by_id(org, pid, cfg["exemplars"]), revise=revise)


def record(dev, org, pid, task, result, version, *, tags=(), run_id=None, metadata=None):
    """Log the task as a DevTools run (with one child run per tool call). Returns the run id."""
    rid = run_id or uuid.uuid4().hex
    scores = dict(result.checks)
    if result.self_critique is not None:
        scores["self_critique"] = result.self_critique
    runs = [{"id": rid, "name": "agent.run", "input": {"task": task}, "output": result.answer, "status": "success",
             "scores": scores, "version_name": "agent_config", "version": version, "tags": list(tags),
             "metadata": {"attempts": result.attempts, "tool_calls": len(result.transcript), "llm_calls": result.calls,
                          "sources": result.sources, **({"error": result.error} if result.error else {}),
                          **(metadata or {})}}]
    runs += [{"id": f"{rid[:48]}.t{i}", "parent_id": rid, "name": f"tool:{s['tool']}", "input": s["args"],
              "output": s["result"], "status": "error" if str(s["result"]).startswith("error:") else "success",
              "tags": list(tags), "version_name": "agent_config", "version": version}
             for i, s in enumerate(result.transcript)]
    dev.log_runs(org, pid, runs)
    return rid
