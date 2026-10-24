"""The promoter: the only part of the loop that can approve anything, and only when every rule holds (spec §8.2).

  1. the holdout comparison is "improved" under the project's own policy (DevStore.approve checks it again)
  2. no critical example regressed, in either split
  3. the candidate config is valid and keeps every locked_text line
  4. the holdout has at least loop.holdout_min_examples verified examples
  5. fewer than loop.max_promotions_per_day promotions in the last 24 hours
  6. the judge was deterministic, or a different model from the actor's (G1)
  7. the loop isn't paused (G7)

What happens when they hold depends on the autonomy level: at 0 and 1 a human decides; at 2 the loop approves and
starts a canary; at 3 the monitor may also promote the canary to everyone (monitor.py).
"""
import time

from sia_devtools.errors import ValidationError

from . import config
from .evaluator import check_judge

ACTOR = "sia-loop"


def recent_promotions(dev, org, pid, now=None):
    now = now or time.time()
    return sum(1 for a in dev.audit(org, pid, 500) if a["actor"] == ACTOR and a["action"] == "version.approve"
               and a["created"] > now - 86400)


def rules(dev, org, pid, version, cmp_holdout, cmp_dev):
    """[(rule, ok, detail)] for promoting agent_config@version on this evidence."""
    p = dev.project(org, pid)
    s, loop = p["settings"], p["settings"]["loop"]
    cfg = config.parse(dev.version(org, pid, config.CONFIG_NAME, version)["content"])
    out = [("holdout improved", cmp_holdout["verdict"] == "improved" and cmp_holdout["project_policy"],
            f"{cmp_holdout['verdict']}: {cmp_holdout['reason']}")]
    crit = list(cmp_holdout["critical_regressions"]) + list((cmp_dev or {}).get("critical_regressions") or [])
    out.append(("no critical regressions", not crit, f"critical examples that got worse: {crit}" if crit else "none"))
    try:
        config.validate(cfg, s, *dev.config_refs(org, pid))
        out.append(("valid config and locked text", True, "ok"))
    except ValidationError as e:
        out.append(("valid config and locked text", False, str(e)))
    n = cmp_holdout["n"]
    out.append(("enough holdout examples", n >= loop["holdout_min_examples"],
                f"{n} paired holdout examples; the minimum is {loop['holdout_min_examples']}"))
    done = recent_promotions(dev, org, pid)
    out.append(("promotions per day", done < loop["max_promotions_per_day"],
                f"{done} in the last 24 h; the limit is {loop['max_promotions_per_day']}"))
    try:
        judge = check_judge(s, cfg)
        out.append(("independent judge", True, "deterministic scorers" if not judge else f"judge {judge['model']}"))
    except ValidationError as e:
        out.append(("independent judge", False, str(e)))
    out.append(("loop not paused", not loop["paused"], "paused" if loop["paused"] else "running"))
    return out


def decide(dev, org, pid, version, cmp_holdout, cmp_dev=None, payload=None, actor=ACTOR):
    """Apply the rules and the autonomy level to the cycle's best candidate. Returns {"status", "outcome", ...}."""
    payload = payload or {}
    checks = rules(dev, org, pid, version, cmp_holdout, cmp_dev)
    failed = [(name, detail) for name, ok, detail in checks if not ok]
    report = [{"rule": name, "ok": ok, "detail": detail} for name, ok, detail in checks]
    if cmp_holdout["verdict"] != "improved":
        dev.reject(org, pid, config.CONFIG_NAME, version, f"holdout: {cmp_holdout['verdict']} "
                                                          f"({cmp_holdout['reason']})", actor=actor)
        return {"status": "rejected", "outcome": "rejected_on_holdout", "rules": report}
    autonomy = dev.project(org, pid)["settings"]["loop"]["autonomy"]
    if failed or (autonomy < 2 and not payload.get("activate")):
        why = "; ".join(f"{n}: {d}" for n, d in failed) or f"autonomy {autonomy}: a human approves"
        return {"status": "candidate", "outcome": "awaiting_approval", "reason": why, "rules": report,
                "comparison_id": cmp_holdout["id"]}
    if payload.get("activate"):  # a human asked this cycle to release a winner, so no canary is involved
        who = payload.get("requested_by") or actor
        dev.approve(org, pid, config.CONFIG_NAME, version, comparison_id=cmp_holdout["id"], actor=who)
        dev.activate(org, pid, config.CONFIG_NAME, version, actor=who)
        return {"status": "approved", "outcome": "activated", "rules": report, "comparison_id": cmp_holdout["id"]}
    return {"status": "candidate", "outcome": "awaiting_approval", "reason": "canary promotion arrives in phase 5",
            "rules": report, "comparison_id": cmp_holdout["id"]}
