"""Scorers, paired comparison and the CI gate. Pure functions, no I/O, so local mode and the server agree.

A scorer is `fn(output, expected, example) -> float in [0, 1]` (bools count as 0/1) with a name, a version and a
kind: "deterministic" (exact checks, assertions) or "model" (an LLM judge). An example's primary score is the mean
of its deterministic scores; model scores only decide when no deterministic scorer was given.
"""
import json
import re
from dataclasses import dataclass
from statistics import mean, stdev
from typing import Any, Callable

DEFAULT_POLICY = {
    "min_examples": 5,            # fewer paired examples than this -> inconclusive, never "improved"
    "min_gain": 0.02,             # the mean gain must beat this...
    "z": 1.0,                     # ...and z standard errors of the per-example gains (evaluation noise)
    "regression_tolerance": 0.05,  # an example whose score drops by more than this counts as a regression
    "max_regressions": None,      # None = no limit on ordinary regressions; critical examples may never regress
}
VERDICTS = ("improved", "no_improvement", "inconclusive", "regressed")


@dataclass(frozen=True)
class Scorer:
    name: str
    fn: Callable[[Any, Any, dict], float]
    version: str = "1"
    kind: str = "deterministic"

    def __call__(self, output, expected, example):
        return max(0.0, min(1.0, float(self.fn(output, expected, example))))

    def describe(self):
        return {"name": self.name, "version": self.version, "kind": self.kind}


def scorer(name=None, version="1", kind="deterministic"):
    """Decorator for custom scorers: @scorer("refund_amount", version="2")."""
    def wrap(fn):
        return Scorer(name or fn.__name__, fn, version, kind)
    return wrap


def as_text(x):
    """Comparable text for a value: a one-key dict like {"answer": ...} is its value, other objects are JSON."""
    if isinstance(x, dict) and len(x) == 1:
        x = next(iter(x.values()))
    if isinstance(x, str):
        return x
    return json.dumps(x, sort_keys=True, default=str)


def _norm(s):
    return " ".join(as_text(s).split()).casefold()


@scorer("exact_match")
def exact_match(output, expected, example):
    return _norm(output) == _norm(expected)


@scorer("contains")
def contains(output, expected, example):
    """Every expected snippet appears in the output. `expected` may be a string or a list of snippets."""
    want = expected if isinstance(expected, list) else [expected]
    out = _norm(output)
    return mean(_norm(w) in out for w in want) if want else 1.0


@scorer("json_match")
def json_match(output, expected, example):
    """Fraction of expected fields the output gets right (structured outputs). A string output is parsed as JSON."""
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            return 0.0
    if not isinstance(expected, dict) or not isinstance(output, dict):
        return float(output == expected)
    return mean(output.get(k) == v for k, v in expected.items()) if expected else 1.0


def regex(pattern, name=None):
    """Assertion: the output matches `pattern` (re.search, case-insensitive)."""
    rx = re.compile(pattern, re.I)
    return Scorer(name or f"regex:{pattern}", lambda o, e, ex: bool(rx.search(as_text(o))), "1")


def llm_judge(llm, rubric="Is the answer correct and complete compared with the reference?", name="llm_judge"):
    """Model-based grader. `llm` is anything with .json(system, prompt, temperature=...) like sia.llm.LLM.
    Graded at temperature 0, but still nondeterministic: treat it as a noisy signal, not ground truth."""
    def judge(output, expected, example):
        v = llm.json("You are a strict, fair grader.",
                     f"INPUT:\n{as_text(example.get('input'))}\n\nANSWER:\n{as_text(output)}\n\n"
                     f"REFERENCE:\n{as_text(expected)}\n\nRUBRIC:\n{rubric}\n\nReturn {{\"score\": <0-10>}}",
                     temperature=0)
        return float(v.get("score", 0)) / 10 if isinstance(v, dict) else 0.0
    return Scorer(name, judge, "1", "model")


BUILTIN = {s.name: s for s in (exact_match, contains, json_match)}


def resolve(s):
    """A Scorer, the name of a built-in, or a plain function (named after the function, version "1")."""
    if isinstance(s, Scorer):
        return s
    if isinstance(s, str):
        if s.startswith("regex:"):
            return regex(s[6:])
        if s not in BUILTIN:
            raise ValueError(f"Unknown scorer '{s}'. Built-ins: {', '.join(BUILTIN)}, regex:<pattern>")
        return BUILTIN[s]
    if callable(s):
        return Scorer(getattr(s, "__name__", "custom"), s, str(getattr(s, "version", "1")))
    raise ValueError(f"Not a scorer: {s!r}")


def primary_score(scores, kinds):
    """Deterministic scores decide when there are any; model-graded ones only otherwise."""
    det = [v for k, v in scores.items() if kinds.get(k) == "deterministic"]
    vals = det or list(scores.values())
    return mean(vals) if vals else 0.0


def compare_scores(old, new, critical=None, policy=None, ids=None):
    """Paired comparison of baseline vs candidate on the same examples. Returns a verdict with its reason."""
    p = {**DEFAULT_POLICY, **(policy or {})}
    n = len(old)
    if n != len(new):
        raise ValueError("Baseline and candidate must be scored on the same examples.")
    ids = list(ids) if ids is not None else list(range(n))
    critical = list(critical) if critical is not None else [False] * n
    gains = [b - a for a, b in zip(old, new)]
    gain = mean(gains) if gains else 0.0
    se = stdev(gains) / n ** 0.5 if n > 1 else 0.0
    threshold = max(p["min_gain"], p["z"] * se)
    # one improved example out of n gives gain == SE exactly, so without a tolerance a tie with z*SE was decided
    # by floating-point rounding ("improved" at n=10, "inconclusive" at n=7). A tie is not an improvement.
    tie = 1e-9
    tol = p["regression_tolerance"]
    regressions = [i for i, g in zip(ids, gains) if g < -tol]
    crit = [i for i, g, c in zip(ids, gains, critical) if c and g < -tol]
    improvements = [i for i, g in zip(ids, gains) if g > tol]

    if crit:  # critical examples must hold whatever the sample size or the average says
        verdict, reason = "regressed", f"{len(crit)} critical example(s) got worse: {crit}"
    elif n < p["min_examples"]:
        verdict, reason = "inconclusive", f"only {n} paired example(s); the policy needs at least {p['min_examples']}"
    elif p["max_regressions"] is not None and len(regressions) > p["max_regressions"]:
        verdict, reason = "regressed", (f"{len(regressions)} example(s) regressed, more than the "
                                        f"{p['max_regressions']} allowed")
    elif gain > threshold + tie:
        verdict, reason = "improved", f"mean gain {gain:+.3f} beats the threshold {threshold:.3f} (max of min_gain, z*SE)"
    elif gain < -threshold - tie:
        verdict, reason = "regressed", f"mean change {gain:+.3f} is below -{threshold:.3f}"
    elif p["z"] * se > p["min_gain"]:
        verdict, reason = "inconclusive", f"mean change {gain:+.3f} is within evaluation noise (z*SE = {p['z'] * se:.3f})"
    else:
        verdict, reason = "no_improvement", f"mean change {gain:+.3f} does not reach min_gain {p['min_gain']}"
    return {
        "verdict": verdict, "reason": reason, "n": n, "policy": p,
        "baseline_mean": round(mean(old), 4) if old else None, "candidate_mean": round(mean(new), 4) if new else None,
        "mean_gain": round(gain, 4), "std_error": round(se, 4), "threshold": round(threshold, 4),
        "regressions": regressions, "critical_regressions": crit, "improvements": improvements,
        "per_example": [{"example_id": i, "baseline": round(a, 4), "candidate": round(b, 4), "delta": round(b - a, 4),
                         "critical": bool(c)} for i, a, b, c in zip(ids, old, new, critical)],
    }


EXIT = {"pass": 0, "fail": 1, "inconclusive": 3}  # 2 is argparse's usage error, 4 is a runtime error


def gate(candidate_score, comparison=None, min_score=None, max_regressions=None, require_improvement=False):
    """CI decision. Returns (status, reasons): status is pass, fail or inconclusive (see EXIT for exit codes)."""
    fails, unsure = [], []
    if min_score is not None and candidate_score < min_score:
        fails.append(f"score {candidate_score:.3f} is below --min-score {min_score}")
    if comparison:
        v = comparison["verdict"]
        if max_regressions is not None and len(comparison["regressions"]) > max_regressions:
            fails.append(f"{len(comparison['regressions'])} regression(s), --max-regressions is {max_regressions}")
        if comparison["critical_regressions"]:
            fails.append(f"critical example(s) regressed: {comparison['critical_regressions']}")
        if v == "regressed":
            fails.append(f"candidate regressed: {comparison['reason']}")
        elif v == "inconclusive":
            unsure.append(f"inconclusive: {comparison['reason']}")
        elif v == "no_improvement" and require_improvement:
            fails.append(f"no improvement: {comparison['reason']}")
    elif require_improvement:
        unsure.append("no baseline to compare against")
    if fails:
        return "fail", fails + unsure
    if unsure:
        return "inconclusive", unsure
    return "pass", ["all checks passed"]
