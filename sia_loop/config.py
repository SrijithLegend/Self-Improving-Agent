"""The agent config bundle: everything a self-improving agent may change about itself, as one versioned JSON document.

It is stored in the DevTools `versions` table with name="agent_config", so versioning, approval, activation,
rollback and the audit log come for free. `validate` runs on every write (DevStore.create_version calls it):
locked text, allowlisted tools and models, dev-only exemplars and size limits hold for every version, whoever wrote it.
"""
import copy
import json
import re

from sia.llm import PROVIDERS
from sia_devtools.errors import ValidationError
from sia_devtools.store import NAME_RX, regex_problem

AGENT_CONFIG_SCHEMA_VERSION = 1
CONFIG_NAME = "agent_config"
DEFAULT_CONFIG = {
    "schema": AGENT_CONFIG_SCHEMA_VERSION,
    "system_prompt": "",          # role + instructions (locked_text applies here)
    "playbook": "",               # strategy: principles, procedures, checks (<= 600 words)
    "lessons": [],                # lesson ids pinned into the prompt (max 12); their text lives in `lessons`
    "exemplars": [],              # dataset example ids used as few-shot demos (dev split only, max 6)
    "retrieval": {"enabled": True, "k": 5, "max_chars_per_chunk": 1000},
    "tools": {
        "enabled": ["search_knowledge", "calculator"],   # names from the registry
        "macros": [],             # agent-authored declarative tools (tools/macro.py)
        "max_steps": 6,           # tool-loop step limit per task
    },
    "validators": [],             # checks run on every answer: {"type": "regex"|"json_schema", "name", ...}
    "revise": {"max_revisions": 2, "pass_score": 0.75, "critic": "self"},  # inner loop; never used for promotion
    "model": {"provider": "gemini", "chain": ["gemini-flash-latest"], "temperature": 0.3, "thinking": None},
}
THINKING_LEVELS = (None, "minimal", "low", "medium", "high")
MAX_CHARS, MAX_LESSONS, MAX_EXEMPLARS, MAX_STEPS, MAX_MACROS, MAX_VALIDATORS = 50_000, 12, 6, 12, 10, 10
MAX_PLAYBOOK_WORDS, MAX_PROMPT_CHARS = 600, 20_000
BUILTIN_TOOLS = ("search_knowledge", "calculator", "lookup_table", "http_get")


def default_config(**fields):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(copy.deepcopy(fields))
    return cfg


def dumps(cfg):
    """Canonical JSON text: the same config always gives the same version content."""
    return json.dumps(cfg, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def parse(content):
    try:
        cfg = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError as e:
        raise ValidationError(f"agent_config must be JSON: {e}")
    if not isinstance(cfg, dict):
        raise ValidationError("agent_config must be a JSON object")
    return cfg


def _int(v, what, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValidationError(f"{what} must be an integer from {lo} to {hi}")
    return v


def _num(v, what, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ValidationError(f"{what} must be a number from {lo} to {hi}")
    return v


def _obj(v, what, keys):
    if not isinstance(v, dict) or set(v) - set(keys):
        raise ValidationError(f"{what} must be an object with keys among: {', '.join(keys)}")
    return {**keys, **v}


def _ids(v, what, limit):
    if not isinstance(v, list) or len(v) > limit or len(set(map(str, v))) != len(v) or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in v):
        raise ValidationError(f"{what} must be a list of up to {limit} distinct ids")
    return v


def missing_locked(text, locked):
    return [t for t in locked if t not in text]


def validate(cfg, settings, lesson_ids=(), example_splits=None):
    """The config, normalised (missing keys filled from DEFAULT_CONFIG), or ValidationError.

    settings: the project's settings (locked_text, loop.allowed_tools, loop.allowed_models, loop.allow_macros).
    lesson_ids: ids of the project's lessons that are not retired.
    example_splits: {example_id: split} for the project's live, verified dataset examples.
    """
    from .tools.macro import validate_macro  # macros are validated by the module that runs them

    cfg = parse(cfg)
    if set(cfg) - set(DEFAULT_CONFIG):
        raise ValidationError(f"agent_config keys must be among: {', '.join(DEFAULT_CONFIG)}")
    out = {**copy.deepcopy(DEFAULT_CONFIG), **copy.deepcopy(cfg)}
    if out["schema"] != AGENT_CONFIG_SCHEMA_VERSION:
        raise ValidationError(f"agent_config schema must be {AGENT_CONFIG_SCHEMA_VERSION}")
    loop, locked = settings["loop"], settings["locked_text"]

    for k in ("system_prompt", "playbook"):
        if not isinstance(out[k], str) or len(out[k]) > MAX_PROMPT_CHARS:
            raise ValidationError(f"{k} must be a string of at most {MAX_PROMPT_CHARS:,} characters")
    if len(out["playbook"].split()) > MAX_PLAYBOOK_WORDS:
        raise ValidationError(f"playbook must be at most {MAX_PLAYBOOK_WORDS} words")
    # G4: locked text must survive every change. It is required in the system prompt, and in the playbook and
    # every prompt macro template too whenever those have any text.
    for k in ("system_prompt", "playbook"):
        if (out[k] or k == "system_prompt") and (miss := missing_locked(out[k], locked)):
            raise ValidationError(f"{k} drops locked text required by project settings: {miss}")

    unknown = set(_ids(out["lessons"], "lessons", MAX_LESSONS)) - set(lesson_ids)
    if unknown:
        raise ValidationError(f"lessons {sorted(unknown)} don't exist in this project (or are retired)")
    splits = example_splits or {}
    for eid in _ids(out["exemplars"], "exemplars", MAX_EXEMPLARS):
        if eid not in splits:
            raise ValidationError(f"exemplar {eid} is not a live, verified example of this project")
        if splits[eid] != "dev":  # G2: the held-out exam is never shown to the agent
            raise ValidationError(f"exemplar {eid} is in the holdout split; only dev examples can be exemplars")

    r = out["retrieval"] = _obj(out["retrieval"], "retrieval", DEFAULT_CONFIG["retrieval"])
    if not isinstance(r["enabled"], bool):
        raise ValidationError("retrieval.enabled must be true or false")
    _int(r["k"], "retrieval.k", 0, 10)
    _int(r["max_chars_per_chunk"], "retrieval.max_chars_per_chunk", 100, 4000)

    t = out["tools"] = _obj(out["tools"], "tools", DEFAULT_CONFIG["tools"])
    if not isinstance(t["enabled"], list) or len(set(t["enabled"])) != len(t["enabled"]):
        raise ValidationError("tools.enabled must be a list of distinct tool names")
    if bad := [x for x in t["enabled"] if x not in BUILTIN_TOOLS or x not in loop["allowed_tools"]]:
        raise ValidationError(f"tools {bad} are not allowed for this project (settings.loop.allowed_tools)")
    _int(t["max_steps"], "tools.max_steps", 1, MAX_STEPS)
    if not isinstance(t["macros"], list) or len(t["macros"]) > MAX_MACROS:
        raise ValidationError(f"tools.macros must be a list of at most {MAX_MACROS}")
    if t["macros"] and not loop["allow_macros"]:
        raise ValidationError("this project doesn't allow agent-made tools (settings.loop.allow_macros)")
    t["macros"] = [validate_macro(m, locked, set(t["enabled"])) for m in t["macros"]]
    names = [m["name"] for m in t["macros"]]
    if len(set(names)) != len(names) or set(names) & set(BUILTIN_TOOLS):
        raise ValidationError("macro names must be unique and differ from the built-in tools")

    if not isinstance(out["validators"], list) or len(out["validators"]) > MAX_VALIDATORS:
        raise ValidationError(f"validators must be a list of at most {MAX_VALIDATORS}")
    out["validators"] = [validate_validator(v) for v in out["validators"]]
    if len({v["name"] for v in out["validators"]}) != len(out["validators"]):
        raise ValidationError("validator names must be unique")

    rv = out["revise"] = _obj(out["revise"], "revise", DEFAULT_CONFIG["revise"])
    _int(rv["max_revisions"], "revise.max_revisions", 0, 5)
    _num(rv["pass_score"], "revise.pass_score", 0, 1)
    if rv["critic"] != "self":
        raise ValidationError("revise.critic must be 'self'")

    m = out["model"] = _obj(out["model"], "model", DEFAULT_CONFIG["model"])
    if m["provider"] not in PROVIDERS:
        raise ValidationError(f"model.provider must be one of {', '.join(sorted(PROVIDERS))}")
    if not isinstance(m["chain"], list) or not 1 <= len(m["chain"]) <= 4 or not all(isinstance(x, str)
                                                                                   for x in m["chain"]):
        raise ValidationError("model.chain must list 1-4 model names")
    if bad := [x for x in m["chain"] if x not in loop["allowed_models"]]:
        raise ValidationError(f"models {bad} are not allowed for this project (settings.loop.allowed_models)")
    _num(m["temperature"], "model.temperature", 0, 2)
    if m["thinking"] not in THINKING_LEVELS:
        raise ValidationError("model.thinking must be null or one of minimal, low, medium, high (Gemini only)")

    if len(dumps(out)) > MAX_CHARS:
        raise ValidationError(f"agent_config must be at most {MAX_CHARS:,} characters as JSON")
    return out


def validate_validator(v):
    """{"type": "regex", "name", "pattern"} or {"type": "json_schema", "name", "schema"}."""
    if not isinstance(v, dict) or not isinstance(v.get("name"), str) or not NAME_RX.match(v["name"]):
        raise ValidationError("each validator needs a name (letters, digits, '_', '-', '.')")
    if v.get("type") == "regex" and set(v) == {"type", "name", "pattern"}:
        if problem := regex_problem(v["pattern"], max_len=200):
            raise ValidationError(f"validator {v['name']}: pattern {problem}")
        return dict(v)
    if v.get("type") == "json_schema" and set(v) == {"type", "name", "schema"}:
        if not isinstance(v["schema"], dict) or len(json.dumps(v["schema"])) > 5000:
            raise ValidationError(f"validator {v['name']}: schema must be an object of at most 5,000 characters")
        return dict(v)
    raise ValidationError('a validator is {"type": "regex", "name", "pattern"} or {"type": "json_schema", "name", '
                          '"schema"}')


def bootstrap(dev, org, pid, actor=None):
    """A new hosted agent's first config: its instructions as the system prompt, approved and active (there is
    nothing to compare it with yet). Does nothing if the project already has an active agent_config."""
    if dev.active_version(org, pid, CONFIG_NAME):
        return None
    agent = dev.project(org, pid)["settings"]["agent"] or {}
    v = dev.create_version(org, pid, dumps(default_config(system_prompt=agent.get("instructions", ""))),
                           name=CONFIG_NAME, note="initial config", actor=actor)["version"]
    dev.approve(org, pid, CONFIG_NAME, v, actor=actor)
    return dev.activate(org, pid, CONFIG_NAME, v, actor=actor)


# --- diff --------------------------------------------------------------------------------------------------------
def _bullets(text):
    return [line.strip() for line in text.splitlines() if re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", line)]


def _set_diff(label, old, new, fmt=str):
    plus = [fmt(x) for x in new if x not in old]
    minus = [fmt(x) for x in old if x not in new]
    return [f"{label}: " + " ".join([f"+{x}" for x in plus] + [f"−{x}" for x in minus])] if plus or minus else []


def diff(a, b):
    """Human-readable changes from config a to config b, e.g. ["playbook: +2 bullets", "lessons: +#41 −#12",
    "retrieval.k: 5 → 3"]. Stored as the candidate version's note and shown in the dashboard."""
    a, b = {**DEFAULT_CONFIG, **parse(a)}, {**DEFAULT_CONFIG, **parse(b)}
    out = []
    if a["system_prompt"] != b["system_prompt"]:
        d = len(b["system_prompt"]) - len(a["system_prompt"])
        out.append(f"system_prompt: edited ({d:+d} chars)")
    if a["playbook"] != b["playbook"]:
        n = len(_bullets(b["playbook"])) - len(_bullets(a["playbook"]))
        out.append(f"playbook: {n:+d} bullet{'s' if abs(n) != 1 else ''}" if n else "playbook: rewritten")
    out += _set_diff("lessons", a["lessons"], b["lessons"], lambda x: f"#{x}")
    out += _set_diff("exemplars", a["exemplars"], b["exemplars"], lambda x: f"#{x}")
    for group in ("retrieval", "revise", "model"):
        for k in sorted(set(a[group]) | set(b[group])):
            x, y = a[group].get(k), b[group].get(k)
            if x != y:
                show = lambda v: ",".join(v) if isinstance(v, list) else json.dumps(v)  # noqa: E731
                out.append(f"{group}.{k}: {show(x)} → {show(y)}")
    out += _set_diff("tools", a["tools"]["enabled"], b["tools"]["enabled"])
    out += _set_diff("macros", [m["name"] for m in a["tools"]["macros"]], [m["name"] for m in b["tools"]["macros"]])
    if a["tools"]["max_steps"] != b["tools"]["max_steps"]:
        out.append(f"tools.max_steps: {a['tools']['max_steps']} → {b['tools']['max_steps']}")
    out += _set_diff("validators", [v["name"] for v in a["validators"]], [v["name"] for v in b["validators"]])
    return out or ["no change"]
