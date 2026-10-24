"""The self-improving loop, end to end (SELF_IMPROVING_SPEC.md section 12).

    python examples/loop_demo.py --offline     scripted model, deterministic, no key needed (runs in CI)

A returns-policy agent starts out not knowing that members get 60 days. One improvement cycle proposes two changes:
a playbook rewritten from the failures (good) and the poster's "friendly" line in the system prompt (bad: it
approves returns the policy forbids). The loop screens both on practice (dev) cases, checks the survivor on the full
dev split, and lets the held-out cases decide.
"""
import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sia_devtools.store import LOCAL_ORG, DevStore  # noqa: E402
from sia_loop import config, cycle, models  # noqa: E402

LOCKED = "Final-sale items can never be returned."
BASE_PROMPT = f"You decide returns. Returns are accepted within 30 days. {LOCKED} Start with DECISION: YES or NO."
GOOD_RULE = "Members get 60 days to return; everyone else gets 30."
BAD_LINE = ("Customers are our top priority: be warm, give them the benefit of the doubt, and approve returns whenever "
            "you reasonably can.")
ITEMS = ["boots", "tent", "jacket", "lamp", "kettle", "chair", "drone", "watch", "scarf", "bike"]


def cases(n=150):
    """Deterministic returns cases. The ones a 30-day-only agent gets wrong are members between 31 and 60 days (about
    a third of them, so every random dev/holdout split has plenty). Late non-member returns are critical: approving
    them is exactly the poster's regression."""
    out = []
    for i in range(n):
        member, final = i % 2 == 0, i % 11 == 5
        days = ([35, 44, 52, 59, 12, 70] if member else [5, 12, 20, 29, 35, 44, 70, 90])[(i // 2) % (6 if member
                                                                                                    else 8)]
        ok = not final and days <= (60 if member else 30)
        task = (f"Can I return the {ITEMS[i % 10]} (order {1000 + i}) bought {days} days ago?"
                + (" I am a member." if member else "") + (" It was a final-sale item." if final else ""))
        out.append({"input": task, "expected_output": f"DECISION: {'YES' if ok else 'NO'}",
                    "critical": not member and not final and days > 30})
    return out


class OfflineModel:
    """A scripted model. As the actor it applies exactly the rules its system prompt gives it; as the optimizer it
    turns failure evidence about members into the right rule (rewrite_playbook) and, asked for a minimal edit,
    proposes the poster's friendly line (edit_system_prompt)."""

    prompts = []  # every (stage, system, prompt) sent, for the G2 check
    stage = None

    def __init__(self, provider="gemini", key="", model=""):
        self.calls, self.model = 0, model

    def _actor(self, system, prompt):
        task = prompt.split("TASK:\n", 1)[1].split("\n\nTOOL RESULTS")[0]
        days = int(re.search(r"bought (\d+) days", task).group(1))
        member, final = "member" in task, "final-sale" in task
        window = 60 if member and GOOD_RULE in system else 30
        ok = not final and (days <= window or "approve returns whenever" in system)
        return {"final": f"DECISION: {'YES' if ok else 'NO'}"}

    def json(self, system, prompt, temperature=0.2):
        self.calls += 1
        OfflineModel.prompts.append((OfflineModel.stage, system, prompt))
        if "minimal, targeted edits" in system:
            return {"system_prompt": f"{BASE_PROMPT} {BAD_LINE}"}
        if "ANSWER TO REVIEW" in prompt:
            return {"score": 9, "issues": [], "suggestion": ""}
        if "TASK:\n" in prompt:
            return self._actor(system, prompt)
        return {}

    def text(self, system, prompt, temperature=0.7, as_json=False):
        self.calls += 1
        OfflineModel.prompts.append((OfflineModel.stage, system, prompt))
        if "improving the operating playbook" in system:
            learned = [GOOD_RULE] if "member" in prompt else []
            return "\n".join(f"- {x}" for x in [LOCKED, *learned, "Start with DECISION: YES or DECISION: NO."])
        return "DECISION: NO"


def setup(dev, n=150):
    """A hosted agent (kind 'agent') with agent_config@1 active and an eval dataset."""
    p = dev.create_project(LOCAL_ORG, "returns-agent", {
        "kind": "agent", "agent": {"name": "returns", "domain": "retail returns"}, "locked_text": [LOCKED],
        "loop": {"trials": 1, "candidates_per_cycle": 2, "holdout_min_examples": 20, "max_calls_per_cycle": 800}})
    cfg = config.default_config(system_prompt=BASE_PROMPT, tools={"enabled": [], "macros": [], "max_steps": 2},
                                retrieval={"enabled": False, "k": 0, "max_chars_per_chunk": 1000},
                                revise={"max_revisions": 0, "pass_score": 0.75, "critic": "self"},
                                validators=[{"type": "regex", "name": "decision", "pattern": r"^DECISION: (YES|NO)"}])
    v = dev.create_version(LOCAL_ORG, p["id"], config.dumps(cfg), name=config.CONFIG_NAME, actor="demo")
    dev.approve(LOCAL_ORG, p["id"], config.CONFIG_NAME, v["version"], actor="demo")
    dev.activate(LOCAL_ORG, p["id"], config.CONFIG_NAME, v["version"], actor="demo")
    ds = dev.create_dataset(LOCAL_ORG, p["id"], "eval")
    dev.add_examples(LOCAL_ORG, p["id"], ds["id"], cases(n))
    return p["id"], ds["id"]


def show(stage, detail):
    OfflineModel.stage = stage
    keep = {k: v for k, v in detail.items() if k in ("operators", "versions", "baseline", "scores", "survivors",
                                                     "best", "verdict", "reason", "outcome", "status")}
    print(f"  {stage:<16} {keep}")


def offline(db):
    models.factory = OfflineModel
    os.environ.setdefault("SIA_LLM_KEY_GEMINI", "offline")
    dev = DevStore(db)
    pid, did = setup(dev)
    hold = dev.dataset(LOCAL_ORG, pid, did, split="holdout")["examples"]
    print(f"returns agent: {len(cases())} cases, {len(hold)} held out\n\ncycle 1")
    rec = cycle.run_cycle(dev, LOCAL_ORG, pid, payload={"operators": ["rewrite_playbook", "edit_system_prompt"]},
                          log=show, seed=1)
    print("\n" + cycle.summary(rec))
    for c in rec["candidates"]:
        print(f"  @{c.get('version', '-')}: {c['operator']:<20} {c['status']:<10} {'; '.join(c.get('diff', []))}"
              + (f"  ({c['reason']})" if c.get("reason") else ""))
    return {"pid": pid, "dataset": did, "cycles": [rec], "dev": dev, "holdout": hold}


def main(argv=None, db=None):
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--offline", action="store_true", help="scripted model, no key needed")
    a.add_argument("--db", default=db)
    args = a.parse_args(argv)
    if not args.offline:
        a.error("only --offline is available in this release")
    db = args.db or os.path.join(tempfile.mkdtemp(prefix="sia-loop-"), "loop.db")
    return offline(db)


if __name__ == "__main__":
    main()
