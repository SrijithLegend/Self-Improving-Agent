"""The live monitor: live traffic is the final judge (G6).

While a canary runs, the canary and the active version serve the same live traffic in the same window. Every check
compares them on error rate, validator failures, negative outcomes and (after 30 ratings each) negative feedback,
with a two-proportion z-test. A canary that is worse on any of them (z > 2) is stopped and its approval revoked.
Production never left the active version, so nothing else needs undoing. A canary that holds up for
loop.canary_min_runs runs and loop.canary_min_hours is promoted at autonomy 3; at autonomy 2 a person is told it's
ready. For 48 hours after a promotion the same tests compare the new version with the previous one, and a failure
re-activates the previous version.
"""
import math
import time

from . import jobs, notify

ACTOR = "sia-loop"
Z_MAX, MIN_RATINGS, WATCH_HOURS, EVERY = 2.0, 30, 48, 600
METRICS = (("error rate", "errors", "runs"), ("validator failures", "validator_fails", "validator_runs"),
           ("negative outcomes", "outcome_fails", "outcome_runs"), ("negative feedback", "negative_ratings", "ratings"))


def z_worse(fail_a, n_a, fail_b, n_b):
    """Two-proportion z statistic; positive when b fails more often than a."""
    if not n_a or not n_b:
        return 0.0
    p = (fail_a + fail_b) / (n_a + n_b)
    se = math.sqrt(p * (1 - p) * (1 / n_a + 1 / n_b))
    return 0.0 if se == 0 else (fail_b / n_b - fail_a / n_a) / se


def regressions(base, new):
    """[(metric, detail)] on which `new` is significantly worse than `base`."""
    out = []
    for label, fails, total in METRICS:
        if label == "negative feedback" and min(base[total], new[total]) < MIN_RATINGS:
            continue
        z = z_worse(base[fails], base[total], new[fails], new[total])
        if z > Z_MAX:
            out.append((label, f"{label}: {new[fails]}/{new[total]} vs {base[fails]}/{base[total]} (z = {z:.2f})"))
    return out


def check(dev, org, pid, now=None):
    """One monitoring pass over the project's canaries and recent promotions. Returns what it did."""
    now = now or time.time()
    p = dev.project(org, pid)
    loop, actions = p["settings"]["loop"], []
    if loop["paused"]:
        return [{"action": "paused"}]
    for c in dev.canaries(org, pid):
        name, v, active = c["name"], c["version"], c["active"]
        base = dev.variant_stats(org, pid, name, active, c["started"])
        new = dev.variant_stats(org, pid, name, v, c["started"])
        entry = {"name": name, "canary": v, "active": active, "canary_runs": new["runs"], "active_runs": base["runs"]}
        if min(base["runs"], new["runs"]) < loop["canary_min_runs"]:
            actions.append({**entry, "action": "waiting", "need": loop["canary_min_runs"]})
            continue
        bad = regressions(base, new)
        if bad:
            reason = "live regression: " + "; ".join(d for _, d in bad)
            dev.stop_canary(org, pid, name, reason, actor=ACTOR)
            dev.revoke(org, pid, name, v, reason, actor=ACTOR)
            actions.append({**entry, "action": "rolled_back", "reason": reason})
            notify.emit(dev, org, pid, "canary.rolled_back", {**entry, "reason": reason})
        elif now - c["started"] >= loop["canary_min_hours"] * 3600:
            if loop["autonomy"] >= 3:
                dev.promote_canary(org, pid, name, actor=ACTOR)
                actions.append({**entry, "action": "promoted"})
                notify.emit(dev, org, pid, "canary.promoted", entry)
            else:
                if not any(a["action"] == "canary.ready" and (a["detail"] or {}).get("version") == v
                           for a in dev.audit(org, pid, 200)):
                    dev._audit_now(pid, ACTOR, "canary.ready", {"name": name, "version": v, "canary": new,
                                                                "active": base})
                actions.append({**entry, "action": "ready"})
        else:
            actions.append({**entry, "action": "waiting", "hours": loop["canary_min_hours"]})
    actions += _watch(dev, org, pid, loop, now)
    return actions


def _watch(dev, org, pid, loop, now):
    """After a promotion: the new version against the previous one's last week. A failure rolls back."""
    out, seen = [], set()
    for a in dev.audit(org, pid, 500):
        d = a["detail"] or {}
        if a["action"] != "canary.promoted" or d.get("name") in seen or now - a["created"] > WATCH_HOURS * 3600:
            continue
        seen.add(d["name"])
        name, new_v, prev = d["name"], d["to"], d.get("from")
        current = dev.active_version(org, pid, name)
        if prev is None or not current or current["version"] != new_v:
            continue  # something else was activated since: nothing to watch
        base = dev.variant_stats(org, pid, name, prev, a["created"] - 7 * 86400, a["created"])
        new = dev.variant_stats(org, pid, name, new_v, a["created"])
        if min(base["runs"], new["runs"]) < loop["canary_min_runs"]:
            out.append({"name": name, "action": "watching", "version": new_v, "runs": new["runs"]})
            continue
        if bad := regressions(base, new):
            reason = "live regression after promotion: " + "; ".join(x for _, x in bad)
            dev.activate(org, pid, name, prev, actor=ACTOR)
            dev.revoke(org, pid, name, new_v, reason, actor=ACTOR)
            out.append({"name": name, "action": "rolled_back_after_promotion", "version": new_v, "to": prev,
                        "reason": reason})
            notify.emit(dev, org, pid, "canary.rolled_back", out[-1])
    return out


def live(dev, org, pid, now=None):
    """Whether there's anything left to monitor (a canary, or a promotion still inside its watch window)."""
    now = now or time.time()
    return bool(dev.canaries(org, pid)) or any(
        a["action"] == "canary.promoted" and now - a["created"] <= WATCH_HOURS * 3600 for a in dev.audit(org, pid, 200))


def handler(dev, job):
    """The monitor job: one check, then the next one in 10 minutes while there's something to watch."""
    pid = job["project_id"]
    org = jobs.org_of(dev, pid)
    actions = check(dev, org, pid)
    if live(dev, org, pid) and not dev.project(org, pid)["settings"]["loop"]["paused"]:
        jobs.enqueue(dev, org, pid, "monitor", run_after=time.time() + EVERY, unique="queued")
    return {"actions": actions}
