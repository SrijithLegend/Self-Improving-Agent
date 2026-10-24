"""One improvement cycle, end to end: the "main" of the loop (spec §7.2).

    evidence    failure clusters from the active config's DEV results and live signals (never holdout, G2)
    propose     K operators -> candidate configs; each must pass config.validate and is stored as a candidate
    stage 1     cheap screen on a dev subset (1 trial); the top 2 that beat the active config survive
    stage 2     full dev split; the best must be "improved" over the active config, or the cycle stops
    stage 3     the HOLDOUT decides: active vs best on examples no step above has seen
    promote     the promoter applies the autonomy level and its rules (promoter.py)

Losing candidates are rejected with the reason, so the history shows what was tried. Changes stack one verified step
per cycle: the next cycle starts from whatever is active then. Every model call goes through one budget (G5).
"""
import hashlib
import random
import time

from sia_devtools.errors import NotFound, ValidationError

from . import budget as budgets
from . import config, memory, models, notify, optimizer
from .evaluator import Evaluator

ACTOR = "sia-loop"
SCREEN_N, SURVIVORS = 20, 2


def actor_calls_per_task(dev, org, pid, cfg, version=None):
    """The average LLM calls per production task of this config version recently (recorded on each run), or a guess
    from the config."""
    runs = dev.runs(org, pid, limit=50, root_only=True, name="agent.run", version=version)["items"]
    seen = [r["metadata"].get("llm_calls") for r in runs if isinstance(r.get("metadata"), dict)]
    seen = [x for x in seen if isinstance(x, (int, float)) and x > 0]
    if seen:
        return sum(seen) / len(seen)
    return 1 + (1 + cfg["revise"]["max_revisions"] if cfg["revise"]["max_revisions"] else 0)


def _live_evidence(dev, org, pid):
    runs = [r for r in dev.runs(org, pid, limit=200, root_only=True)["items"] if "eval" not in (r["tags"] or [])]
    tool_runs = [r for r in dev.runs(org, pid, limit=200)["items"] if (r["name"] or "").startswith("tool:")]
    errors = [r for r in tool_runs if r["status"] == "error"]
    if errors:
        runs.append({"input": errors[0]["input"], "scores": {}, "output": errors[0]["output"],
                     "tool_errors": f"{len(errors)} failing tool call(s), e.g. {errors[0]['name']}"})
    transcripts = [{"tool": r["name"][5:], "args": r["input"], "result": r["output"]} for r in tool_runs[:30]]
    corrections = dev.feedback_list(org, pid, "verified", limit=100)["items"]
    return runs, corrections, transcripts


def run_cycle(dev, org, pid, *, payload=None, llm_for=None, judge_llm=None, quota=None, on_usage=None, log=None,
              seed=None):
    """Run one cycle for a kind="agent" project. Returns the cycle's record (stored as the cycle job's result, which
    is the loop history). quota(org) -> calls left this month or None; on_usage(calls) meters what was spent."""
    from . import promoter
    payload, events, t0 = payload or {}, [], time.time()

    def say(stage, **detail):
        events.append({"stage": stage, "t": round(time.time() - t0, 3), **detail})
        if log:
            log(stage, detail)

    p = dev.project(org, pid)
    settings, loop = p["settings"], p["settings"]["loop"]
    if settings["kind"] != "agent":
        raise ValidationError("Server-side cycles run on hosted agents (kind='agent'). For your own app, run "
                              "`sia loop run --target module:function` where your code can run.")
    if loop["paused"]:
        say("paused")
        return {"outcome": "paused", "events": events, "calls": 0}
    active = dev.active_version(org, pid, config.CONFIG_NAME)
    if active is None:
        raise ValidationError("This agent has no active agent_config to improve on.")
    active_cfg = config.parse(active["content"])
    try:
        ds = dev.datasets(org, pid, payload.get("dataset", "eval"))[0]
    except IndexError:
        raise NotFound(f"No dataset named {payload.get('dataset', 'eval')!r} to learn from.")
    dev_ex = dev.dataset(org, pid, ds["id"], split="dev")["examples"]
    hold_ex = dev.dataset(org, pid, ds["id"], split="holdout")["examples"]
    record = {"outcome": None, "active_version": active["version"], "dataset_id": ds["id"],
              "dataset_version": ds["version"], "dev_examples": len(dev_ex), "holdout_examples": len(hold_ex),
              "candidates": [], "events": events, "calls": 0}
    if len(dev_ex) < 2:
        say("not_enough_data", dev_examples=len(dev_ex))
        return {**record, "outcome": "not_enough_data"}

    k, trials, screen_n = loop["candidates_per_cycle"], loop["trials"], min(SCREEN_N, len(dev_ex))
    per_task = actor_calls_per_task(dev, org, pid, active_cfg, active["version"])
    est = budgets.estimate(candidates=k, screen_n=screen_n, dev_n=len(dev_ex), holdout_n=len(hold_ex),
                           trials=trials, actor_calls=per_task, judge_calls=1 if "judge" in loop["scorers"] else 0)
    # ponytail: the estimate still counts the dev baseline even when a recent one is reused (it errs on the safe side)
    left = quota(org) if quota else None
    record.update(estimate=est, cap=loop["max_calls_per_cycle"], quota_left=left)
    if est > loop["max_calls_per_cycle"] or (left is not None and est > left):
        say("skipped_budget", estimate=est, cap=loop["max_calls_per_cycle"], quota_left=left)
        dev._audit_now(pid, ACTOR, "loop.skipped_budget", {"estimate": est, "cap": loop["max_calls_per_cycle"],
                                                           "quota_left": left})
        return {**record, "outcome": "skipped_budget", "_status": "skipped_budget"}

    bud = budgets.Budget(min(loop["max_calls_per_cycle"], left) if left is not None else loop["max_calls_per_cycle"])
    make = llm_for or models.for_config
    metered = lambda cfg: bud.wrap(make(cfg))  # noqa: E731
    judge = bud.wrap(judge_llm) if judge_llm is not None else None
    ev_runner = Evaluator(dev, org, pid, llm_for=metered, judge_llm=judge)
    rng = random.Random(seed if seed is not None else int(hashlib.sha256(f"{pid}:{t0}".encode()).hexdigest(), 16))
    cands = {}  # version -> {"operator", "cfg", "diff", ...}

    def reject_all(reason, keep=()):
        for v, c in cands.items():
            if v not in keep and c.get("status") == "candidate":
                dev.reject(org, pid, config.CONFIG_NAME, v, reason, actor=ACTOR)
                c["status"], c["reason"] = "rejected", reason

    try:
        reused = ev_runner.reuse(active["version"], ds["id"], "dev", trials)
        base_dev, base_dev_res = reused or ev_runner.evaluate(active["version"], ds["id"], "dev", trials,
                                                              "loop:baseline:dev")
        say("baseline", version=active["version"], reused=bool(reused))
        record["baseline_dev"] = {"evaluation_id": base_dev["id"], "score": base_dev["summary"]["score"],
                                  "reused": bool(reused)}

        runs, corrections, transcripts = _live_evidence(dev, org, pid)
        clusters = memory.failure_clusters(base_dev_res, runs, corrections, dev.holdout_inputs(org, pid))
        evidence = optimizer.Evidence(clusters=clusters, settings=settings, llm=metered(active_cfg),
                                      lessons=dev.lessons(org, pid, "probation") + dev.lessons(org, pid, "pinned"),
                                      utilities=memory.utilities(dev, org, pid), dev_results=base_dev_res,
                                      transcripts=transcripts)
        record["evidence"] = [{"key": c["key"], "kind": c["kind"], "count": c["count"]} for c in clusters]

        names = payload.get("operators") or optimizer.pick(evidence, k, rng)
        say("propose", operators=names)
        lessons, splits = dev.config_refs(org, pid)
        seen = {config.dumps(config.validate(active_cfg, settings, lessons, splits))}
        for name, cand, err in optimizer.propose(active_cfg, evidence, names):
            entry = {"operator": name, "status": "skipped"}
            try:
                if cand is None:
                    raise ValidationError(err or "nothing to propose")
                cand = config.validate(cand, settings, lessons, splits)
                if config.dumps(cand) in seen:
                    raise ValidationError("same as the active config or another candidate")
            except ValidationError as e:
                record["candidates"].append({**entry, "reason": str(e)[:300]})
                continue
            seen.add(config.dumps(cand))
            diff = config.diff(active_cfg, cand)
            v = dev.create_version(org, pid, config.dumps(cand), name=config.CONFIG_NAME,
                                   parent_version=active["version"], note=f"{name}: " + "; ".join(diff),
                                   actor=ACTOR)["version"]
            cands[v] = {"version": v, "operator": name, "diff": diff, "status": "candidate", "cfg": cand}
            notify.emit(dev, org, pid, "candidate.created", {"version": v, "operator": name, "diff": diff})
        say("candidates", versions=list(cands))
        if not cands:
            record.update(outcome="no_candidates")
            return record

        # stage 1: cheap screen on a dev subset, one trial
        subset = sorted(dev_ex, key=lambda e: e["id"])[:screen_n]
        base_sub = {r["example_id"]: r["score"] for r in base_dev_res}
        base_mean = sum(base_sub[e["id"]] for e in subset) / len(subset)
        for v, c in cands.items():
            res = ev_runner.score(c["cfg"], subset, trials=1)
            c["screen"] = round(sum(r["score"] for r in res) / len(res), 4)
        survivors = sorted((v for v, c in cands.items() if c["screen"] > base_mean),
                           key=lambda v: -cands[v]["screen"])[:SURVIVORS]
        say("screen", baseline=round(base_mean, 4), scores={v: c["screen"] for v, c in cands.items()},
            survivors=survivors)
        for v, c in cands.items():
            if v not in survivors:
                why = "not among the best two" if c["screen"] > base_mean else "no better than the active config"
                dev.reject(org, pid, config.CONFIG_NAME, v, f"screen: {c['screen']:.3f} vs the active config's "
                           f"{base_mean:.3f} on {len(subset)} dev examples ({why})", actor=ACTOR)
                c["status"], c["reason"] = "rejected", f"screen {c['screen']:.3f} vs {base_mean:.3f}: {why}"
        if not survivors:
            record.update(outcome="no_improvement")
            return record

        # stage 2: the full dev split
        for v in survivors:
            ev, _ = ev_runner.evaluate(v, ds["id"], "dev", trials, "loop:candidate:dev")
            cands[v]["dev_evaluation"], cands[v]["dev_score"] = ev["id"], ev["summary"]["score"]
        best = max(survivors, key=lambda v: cands[v]["dev_score"])
        cmp_dev = dev.compare(org, pid, base_dev["id"], cands[best]["dev_evaluation"], actor=ACTOR)
        cands[best]["dev_verdict"] = cmp_dev["verdict"]
        say("dev", best=best, verdict=cmp_dev["verdict"], reason=cmp_dev["reason"])
        if cmp_dev["verdict"] != "improved":
            reject_all(f"dev: {cmp_dev['verdict']} ({cmp_dev['reason']})")
            record.update(outcome="no_improvement", dev_comparison=cmp_dev["id"])
            return record
        reject_all("dev: another candidate scored higher on the dev split", keep=[best])

        # stage 3: the holdout decides (and every critical example, in both splits, must hold)
        if not hold_ex:
            reject_all("holdout: the dataset has no holdout examples, so nothing can be promoted")
            record.update(outcome="no_holdout")
            return record
        say("holdout", best=best)
        base_hold, _ = ev_runner.evaluate(active["version"], ds["id"], "holdout", trials, "loop:baseline:holdout")
        cand_hold, _ = ev_runner.evaluate(best, ds["id"], "holdout", trials, "loop:candidate:holdout")
        cmp = dev.compare(org, pid, base_hold["id"], cand_hold["id"], actor=ACTOR)
        cands[best].update(holdout_verdict=cmp["verdict"], holdout_comparison=cmp["id"])
        record.update(best=best, dev_comparison=cmp_dev["id"], holdout_comparison=cmp["id"],
                      holdout={"baseline": cmp["baseline"]["score"], "candidate": cmp["candidate"]["score"],
                               "verdict": cmp["verdict"], "reason": cmp["reason"]})
        say("holdout_verdict", verdict=cmp["verdict"], reason=cmp["reason"])
        decision = promoter.decide(dev, org, pid, best, cmp, cmp_dev, payload=payload, actor=ACTOR)
        cands[best]["status"] = decision["status"]
        record.update(outcome=decision["outcome"], promotion=decision)
        say("promote", **{k: v for k, v in decision.items() if k != "rules"})
        return record
    except budgets.BudgetExhausted as e:
        reject_all(f"budget: {e}")
        say("budget_exhausted", used=bud.used)
        record.update(outcome="budget_exhausted")
        return record
    except Exception as e:  # a model outage or a bug: nothing half-measured may be decided on
        reject_all(f"cycle stopped before a decision: {type(e).__name__}: {str(e)[:200]}")
        say("failed", error=f"{type(e).__name__}: {str(e)[:300]}")
        record.update(outcome="failed")
        raise
    finally:
        record["calls"] = bud.used
        record["candidates"] = [*record["candidates"], *({k: v for k, v in c.items() if k != "cfg"}
                                                         for c in cands.values())]
        if on_usage and bud.used:
            on_usage(bud.used)
        for c in cands.values():
            if c.get("status") == "rejected":
                notify.emit(dev, org, pid, "candidate.rejected", {"version": c["version"], "reason": c.get("reason")})
        if record.get("outcome") == "budget_exhausted":
            notify.emit(dev, org, pid, "budget.exhausted", {"calls": bud.used, "cap": bud.cap})
        dev._audit_now(pid, ACTOR, "loop.cycle", {"outcome": record.get("outcome"), "calls": bud.used,
                                                  "candidates": list(cands), "best": record.get("best")})


def summary(record):
    """One line for a cycle record, for logs and the CLI."""
    h = record.get("holdout") or {}
    return (f"cycle: {record['outcome']}; {len(record['candidates'])} candidate(s); "
            + (f"holdout {h['baseline']:.3f} -> {h['candidate']:.3f} ({h['verdict']}); " if h else "")
            + f"{record['calls']} LLM call(s)")

