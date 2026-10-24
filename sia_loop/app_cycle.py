"""One improvement cycle for your own app (kind="app"), run where your code runs: `sia loop run --target mod:fn`.

Your target is the actor, called as target(input, prompt) with each candidate prompt. Everything goes through
SIAClient, so it works the same in local mode and against a hosted server (the evaluations are computed here and
recorded there, like `sia improve`). The same discipline as a hosted cycle holds:

    candidates are drafted from DEV failures and verified feedback only (holdout inputs are filtered out, G2)
    the best on the full dev split must be "improved" over the active prompt, then the HOLDOUT decides
    autonomy 0-1: the winner waits for a person; autonomy 2: it's approved and starts a canary (get_prompt(unit=))
"""
from sia_devtools.errors import ValidationError
from sia_devtools.scoring import as_text

ACTOR = "sia-loop"
FOCUS = ["the most common failure", "the second most common failure", "output format and structure",
         "edge cases the instructions don't cover", "keeping the instructions short and unambiguous"]


def _norm(x):
    return " ".join(as_text(x).split()).casefold()


def propose(llm, current, evidence, locked, focus):
    from sia.llm import parse_json
    raw = llm.text("You improve the instructions of an AI application. You only change instruction text, never code.",
                   f"CURRENT INSTRUCTIONS:\n{current or '(none yet)'}\n\nFAILURES ON PRACTICE CASES:\n{evidence}\n\n"
                   + ("These lines are locked and must appear verbatim in your output:\n"
                      + "\n".join(f"- {t}" for t in locked) + "\n\n" if locked else "")
                   + f"Focus on {focus}. Change as little as possible and keep what already works. Return JSON: "
                     '{"content": "<the full improved instructions>"}', temperature=0.5)
    out = parse_json(raw)
    content = out.get("content") if isinstance(out, dict) else None
    return content.strip() if isinstance(content, str) and content.strip() else None


def run(client, target, scorers, llm, *, dataset="eval", name="system_prompt", k=None, log=print):
    """One cycle. Returns a record like a hosted cycle's: outcome, candidates, comparisons."""
    s = client.project_settings()
    loop = s["loop"]
    if loop["paused"]:
        return {"outcome": "paused"}
    ds = client.dataset(dataset)
    dev_ex, hold_ex = ds.examples(split="dev"), ds.examples(split="holdout")
    if len(dev_ex) < 2:
        return {"outcome": "not_enough_data", "dev_examples": len(dev_ex)}
    active = client.active_version(name)
    trials = loop["trials"]
    ev = lambda version, split, label: client.evaluate(ds, target, scorers, version=version, version_name=name,  # noqa
                                                        trials=trials, split=split, name=label)
    if active:
        base_dev = ev("active", "dev", "loop:baseline:dev")
    else:
        base_dev = client.evaluate(ds, target, scorers, prompt="", trials=trials, split="dev", name="loop:baseline:dev")
    log(f"baseline  dev {base_dev['summary']['score']:.3f} on {base_dev['summary']['n']} examples")

    holdout = {_norm(e["input"]) for e in hold_ex}
    fails = [r for r in base_dev["results"] if not r["passed"]]
    verified = [f for f in client.list_feedback("verified", limit=50) if _norm(f["run_input"]) not in holdout]
    evidence = "\n".join(
        [f"- input={as_text(r['input'])[:400]} | expected={as_text(r['expected_output'])[:400]} | "
         f"got={as_text(r['output'])[:400]}" for r in fails[:15]]
        + [f"- input={as_text(f['run_input'])[:300]} | correct={as_text(f['correction'])[:300]}" for f in verified[:10]])
    if not fails and not verified:
        return {"outcome": "nothing_to_fix"}

    record = {"candidates": [], "baseline_dev": base_dev["id"]}
    for focus in FOCUS[:k or loop["candidates_per_cycle"]]:
        try:
            content = propose(llm, active["content"] if active else "", evidence, s["locked_text"], focus)
            if not content or (active and content == active["content"]):
                raise ValidationError("no change")
            v = client.create_version(content, name=name, parent_version=active["version"] if active else None,
                                      note=f"loop: {focus}")
            record["candidates"].append({"version": v["version"], "focus": focus, "status": "candidate"})
        except Exception as e:  # noqa: BLE001 - one bad proposal (or a dropped locked line) isn't fatal
            record["candidates"].append({"focus": focus, "status": "skipped", "reason": str(e)[:200]})
    cands = [c for c in record["candidates"] if c["status"] == "candidate"]
    if not cands:
        return {**record, "outcome": "no_candidates"}

    for c in cands:
        e = ev(c["version"], "dev", "loop:candidate:dev")
        c["dev_score"], c["dev_eval"] = e["summary"]["score"], e["id"]
        log(f"candidate @{c['version']} dev {c['dev_score']:.3f}  ({c['focus']})")
    best = max(cands, key=lambda c: c["dev_score"])
    cmp_dev = client.compare(base_dev, best["dev_eval"])
    for c in cands:
        if c is not best:
            client.reject(c["version"], name=name, reason="dev: another candidate scored higher", reviewer=ACTOR)
            c["status"] = "rejected"
    if cmp_dev["verdict"] != "improved":
        client.reject(best["version"], name=name, reason=f"dev: {cmp_dev['verdict']} ({cmp_dev['reason']})",
                      reviewer=ACTOR)
        best["status"] = "rejected"
        return {**record, "outcome": "no_improvement", "dev_comparison": cmp_dev["id"]}
    if not hold_ex:
        return {**record, "outcome": "no_holdout"}

    base_hold = ev("active", "holdout", "loop:baseline:holdout") if active else client.evaluate(
        ds, target, scorers, prompt="", trials=trials, split="holdout", name="loop:baseline:holdout")
    cand_hold = ev(best["version"], "holdout", "loop:candidate:holdout")
    cmp = client.compare(base_hold, cand_hold)
    log(f"holdout   {cmp['baseline']['score']:.3f} -> {cmp['candidate']['score']:.3f}: {cmp['verdict']} "
        f"({cmp['reason']})")
    record.update(best=best["version"], dev_comparison=cmp_dev["id"], holdout_comparison=cmp["id"],
                  holdout={"baseline": cmp["baseline"]["score"], "candidate": cmp["candidate"]["score"],
                           "verdict": cmp["verdict"]})
    if cmp["verdict"] != "improved":
        client.reject(best["version"], name=name, reason=f"holdout: {cmp['verdict']} ({cmp['reason']})",
                      reviewer=ACTOR)
        best["status"] = "rejected"
        return {**record, "outcome": "rejected_on_holdout"}

    problems = []
    if cmp["critical_regressions"] or cmp_dev["critical_regressions"]:
        problems.append("a critical example regressed")
    if cmp["n"] < loop["holdout_min_examples"]:
        problems.append(f"only {cmp['n']} holdout examples (holdout_min_examples is {loop['holdout_min_examples']})")
    if loop["autonomy"] < 2 or problems or not active:
        why = "; ".join(problems) or (f"autonomy {loop['autonomy']}: a person approves" if active else
                                      "the first version is approved by a person")
        log(f"waiting   @{best['version']} stays a candidate: {why}\n"
            f"          sia versions approve {best['version']} --comparison {cmp['id']}")
        return {**record, "outcome": "awaiting_approval", "reason": why}
    client.approve(best["version"], name=name, comparison=cmp["id"], reviewer=ACTOR)
    canary = client.start_canary(best["version"], loop["canary_pct"], name=name, reviewer=ACTOR)
    best["status"] = "approved"
    log(f"canary    @{best['version']} serves {canary['pct']}% of units; get_prompt(unit=...) picks it up")
    return {**record, "outcome": "canary_started", "canary": canary}
