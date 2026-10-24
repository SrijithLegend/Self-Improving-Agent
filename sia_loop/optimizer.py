"""The optimizer: how the agent changes itself.

Each mutation operator takes the active config and the evidence and returns one candidate config (or None when it has
nothing to offer). LLM operators make at most one call; the others are deterministic edits. All evidence comes from
the dev split and live signals; nothing here ever sees a holdout example (G2). Whatever an operator returns still has
to pass config.validate, so locked text, allowlists and the dev-only exemplar rule hold whatever the model wrote.
"""
import copy
import json
import random
import re
from dataclasses import dataclass, field

from sia.llm import LLMError
from sia_devtools.scoring import as_text
from sia_devtools.store import regex_problem

from . import memory
from .tools.macro import validate_macro

K_OPTIONS = (0, 3, 5, 8)


@dataclass
class Evidence:
    clusters: list                        # memory.failure_clusters(...) on dev results and live signals
    settings: dict                        # project settings (locked_text, loop allowlists)
    llm: object = None                    # the optimizer's model (budget-metered)
    lessons: list = field(default_factory=list)       # the project's lessons that aren't retired
    utilities: dict = field(default_factory=dict)     # memory.utilities(...)
    dev_results: list = field(default_factory=list)   # the active config's results on the dev split
    transcripts: list = field(default_factory=list)   # recent production tool calls [{tool, args, result}]

    def kinds(self):
        out = {"quality": 0, "format": 0, "outcome": 0, "tools": 0}
        for c in self.clusters:
            out[c["kind"]] = out.get(c["kind"], 0) + c["count"]
        return out


def _evidence_text(ev, limit=8):
    lines = []
    for c in ev.clusters[:limit]:
        lines.append(f"- [{c['kind']}] {c['key']} ({c['count']}x)")
        for e in c["examples"][:2]:
            lines.append(f"    input: {e.get('input', '')[:300]} | expected: {e.get('expected', '')[:300]} | "
                         f"got: {e.get('got', '')[:300]}")
    return "\n".join(lines) or "(no failures recorded)"


def _locked_note(ev):
    locked = ev.settings["locked_text"]
    return ("These lines are locked and must appear verbatim in your output:\n" + "\n".join(f"- {t}" for t in locked)
            + "\n\n") if locked else ""


def _with(cfg, **changes):
    new = copy.deepcopy(cfg)
    new.update(copy.deepcopy(changes))
    return new


# --- LLM operators (one call each) ----------------------------------------------------------------------------
def rewrite_playbook(cfg, ev):
    lessons = [f"- {lesson['text']}" for lesson in ev.lessons][:25]
    text = ev.llm.text(
        "You are improving the operating playbook of an AI agent. You change only its strategy text.",
        f"CURRENT PLAYBOOK:\n{cfg['playbook'] or '(none yet)'}\n\nLESSONS (some unproven):\n"
        + ("\n".join(lessons) or "(none)") + f"\n\nFAILURE PATTERNS ON PRACTICE CASES:\n{_evidence_text(ev)}\n\n"
        + _locked_note(ev) + "Write the improved playbook: concise principles, procedures and checks as markdown "
        "bullets, under 350 words, that fix the failure patterns and keep what already works. Output only the "
        "playbook.", temperature=0.5).strip()
    return _with(cfg, playbook=text) if text and text != cfg["playbook"] else None


def edit_system_prompt(cfg, ev):
    if not ev.clusters:
        return None
    out = ev.llm.json(
        "You make minimal, targeted edits to an AI agent's instructions.",
        f"CURRENT INSTRUCTIONS:\n{cfg['system_prompt'] or '(none)'}\n\nTHE MOST COMMON FAILURE:\n"
        f"{_evidence_text(ev, 1)}\n\n" + _locked_note(ev) + "Change as little as possible so this failure stops "
        'happening, keeping everything else. Return {"system_prompt": "<the full edited instructions>"}',
        temperature=0.3)
    text = out.get("system_prompt") if isinstance(out, dict) else None
    return _with(cfg, system_prompt=text.strip()) if isinstance(text, str) and text.strip() and \
        text.strip() != cfg["system_prompt"] else None


def add_validator(cfg, ev):
    """A regex every good answer matches, proposed from format failures. Kept only if it matches at least 95% of
    the verified expected outputs on the dev split (so it can't reject correct answers)."""
    if not ev.kinds()["format"] and not ev.kinds()["quality"]:
        return None
    expected = [as_text(r["expected_output"]) for r in ev.dev_results if r["expected_output"] is not None]
    if len(expected) < 5:
        return None
    out = ev.llm.json("You write strict but fair output-format checks for an AI agent.",
                      f"FAILURES:\n{_evidence_text(ev)}\n\nCORRECT ANSWERS LOOK LIKE:\n"
                      + "\n".join(f"- {e[:200]}" for e in expected[:10])
                      + '\n\nPropose one Python regex (re.search) that every correct answer matches. Return '
                        '{"name": "<short_snake_case>", "pattern": "<regex>"}', temperature=0.2)
    if not isinstance(out, dict) or not isinstance(out.get("pattern"), str):
        return None
    name = re.sub(r"[^a-z0-9_]", "_", str(out.get("name") or "format").lower())[:40] or "format"
    if regex_problem(out["pattern"], 200) or name in {v["name"] for v in cfg["validators"]}:
        return None
    rx = re.compile(out["pattern"])
    if sum(bool(rx.search(e)) for e in expected) < 0.95 * len(expected):
        return None
    return _with(cfg, validators=[*cfg["validators"], {"type": "regex", "name": name, "pattern": out["pattern"]}])


def create_macro_tool(cfg, ev):
    if not ev.settings["loop"]["allow_macros"] or not ev.transcripts or len(cfg["tools"]["macros"]) >= 10:
        return None
    calls = "\n".join(f"- {t['tool']}({json.dumps(t['args'])[:200]}) -> {str(t['result'])[:200]}"
                      for t in ev.transcripts[:20])
    out = ev.llm.json("You design small, declarative helper tools for an AI agent. You never write code.",
                      f"RECENT TOOL CALLS:\n{calls}\n\nFAILURES:\n{_evidence_text(ev, 3)}\n\n" + _locked_note(ev)
                      + 'If a sub-task recurs, describe ONE helper as JSON: either {"name", "description", "kind": '
                        '"prompt", "input_schema", "template" (with {field} placeholders), "output_schema"} or '
                        '{"name", "description", "kind": "pipeline", "steps": [{"tool", "args"}]} using only the '
                        f'enabled tools {cfg["tools"]["enabled"]}. Return {{"macro": <helper or null>}}.',
                      temperature=0.3)
    m = out.get("macro") if isinstance(out, dict) else None
    if not isinstance(m, dict):
        return None
    try:
        m = validate_macro(m, ev.settings["locked_text"], set(cfg["tools"]["enabled"]))
    except Exception:  # noqa: BLE001 - an invalid proposal is simply not a candidate
        return None
    if m["name"] in {x["name"] for x in cfg["tools"]["macros"]}:
        return None
    return _with(cfg, tools={**cfg["tools"], "macros": [*cfg["tools"]["macros"], m]})


# --- deterministic operators (no model calls) -------------------------------------------------------------------
def pin_lessons(cfg, ev, k=3):
    pinned = set(cfg["lessons"])
    pool = [lesson for lesson in ev.lessons if lesson["state"] == "probation" and lesson["id"] not in pinned]
    # lessons from verified corrections first, then by trusted utility, then the newest
    pool.sort(key=lambda lesson: (lesson["source"] == "feedback",
                                  ev.utilities.get(lesson["id"], {}).get("utility", 0), lesson["id"]), reverse=True)
    room = 12 - len(pinned)
    add = [lesson["id"] for lesson in pool[:min(k, room)]]
    return _with(cfg, lessons=[*cfg["lessons"], *add]) if add else None


def unpin_lessons(cfg, ev):
    low = {lid for lid in cfg["lessons"] if ev.utilities.get(lid, {}).get("uses", 0) >= memory.MIN_USES
           and ev.utilities[lid]["utility"] < memory.LOW_UTILITY}
    return _with(cfg, lessons=[x for x in cfg["lessons"] if x not in low]) if low else None


def swap_exemplars(cfg, ev):
    """Few-shot demos from the dev cases the active config gets wrong (with their verified answers), plus the
    current demos it still gets right. Dev split only: config.validate refuses anything else."""
    failing = [r for r in ev.dev_results if not r["passed"] and r["expected_output"] is not None]
    if not failing:
        return None
    ok = {r["example_id"] for r in ev.dev_results if r["passed"]}
    keep = [e for e in cfg["exemplars"] if e in ok]
    new = (keep + [r["example_id"] for r in failing if r["example_id"] not in keep][:3])[:6]
    return _with(cfg, exemplars=new) if new != cfg["exemplars"] else None


def tune_retrieval(cfg, ev):
    k = cfg["retrieval"]["k"] if cfg["retrieval"]["enabled"] else 0
    nxt = K_OPTIONS[(K_OPTIONS.index(k) + 1) % len(K_OPTIONS)] if k in K_OPTIONS else 5
    return _with(cfg, retrieval={**cfg["retrieval"], "k": nxt, "enabled": nxt > 0})


def toggle_tools(cfg, ev):
    enabled, allowed = cfg["tools"]["enabled"], ev.settings["loop"]["allowed_tools"]
    failing = {t["tool"] for t in ev.transcripts if str(t["result"]).startswith("error:")}
    if failing & set(enabled):
        drop = sorted(failing & set(enabled))[0]
        return _with(cfg, tools={**cfg["tools"], "enabled": [t for t in enabled if t != drop]})
    for t in allowed:
        if t not in enabled and (t != "http_get" or ev.settings["loop"]["http_allowlist"]):
            return _with(cfg, tools={**cfg["tools"], "enabled": [*enabled, t]})
    return None


def switch_model(cfg, ev):
    """allowed_models is ordered from the most capable to the cheapest. Move to a cheaper one, unless the failures
    are about quality: then try a more capable one."""
    models, current = ev.settings["loop"]["allowed_models"], cfg["model"]["chain"][0]
    i = models.index(current) if current in models else 0
    j = i - 1 if ev.kinds()["quality"] else i + 1
    if not 0 <= j < len(models) or models[j] == current:
        return None
    return _with(cfg, model={**cfg["model"], "chain": [models[j]]})


OPERATORS = {"rewrite_playbook": rewrite_playbook, "edit_system_prompt": edit_system_prompt,
             "pin_lessons": pin_lessons, "unpin_lessons": unpin_lessons, "swap_exemplars": swap_exemplars,
             "tune_retrieval": tune_retrieval, "add_validator": add_validator,
             "create_macro_tool": create_macro_tool, "toggle_tools": toggle_tools, "switch_model": switch_model}
LLM_OPERATORS = {"rewrite_playbook", "edit_system_prompt", "add_validator", "create_macro_tool"}


def weights(ev):
    """How likely each operator is to be tried, from the kind of evidence there is."""
    k = {name: n > 0 for name, n in ev.kinds().items()}
    probation = any(lesson["state"] == "probation" for lesson in ev.lessons)
    return {"rewrite_playbook": 1 + 3 * k["quality"] + k["outcome"], "edit_system_prompt": 1 + 2 * k["quality"]
            + 3 * k["format"], "pin_lessons": 3 if probation else 0, "unpin_lessons": 1, "swap_exemplars":
            2 * k["quality"], "tune_retrieval": 1, "add_validator": 4 * k["format"], "create_macro_tool":
            (3 * k["tools"]) if ev.settings["loop"]["allow_macros"] else 0, "toggle_tools": 0.5 + 2 * k["tools"],
            "switch_model": 1 if len(ev.settings["loop"]["allowed_models"]) > 1 else 0}


def pick(ev, n, rng=None):
    """n distinct operators, sampled by weight (deterministic for a given rng)."""
    rng = rng or random.Random(0)
    w = {name: x for name, x in weights(ev).items() if x > 0}
    chosen = []
    while w and len(chosen) < n:
        total = sum(w.values())
        r, acc = rng.uniform(0, total), 0.0
        for name, x in w.items():
            acc += x
            if r <= acc:
                chosen.append(name)
                del w[name]
                break
    return chosen


def propose(active_cfg, ev, names):
    """[(operator name, candidate config or None, error or None)] for the given operators."""
    out = []
    for name in names:
        try:
            out.append((name, OPERATORS[name](active_cfg, ev), None))
        except (LLMError, re.error, ValueError, TypeError, KeyError) as e:
            out.append((name, None, f"{type(e).__name__}: {e}"[:300]))
    return out
