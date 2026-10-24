"""Server-side evaluation of an agent_config on one split of a dataset.

Scoring follows DevTools: deterministic scorers (settings.loop.scorers, default "contains") decide; an LLM judge
("judge" in loop.scorers) decides only when there is no deterministic scorer. The judge must run on a different
model from the actor's (G1), unless settings.loop.allow_same_model_judge says otherwise. The actor's self-critique
is never a score here.
"""
import hashlib
import time
import uuid
from statistics import mean

from sia.llm import LLMError
from sia_devtools.errors import ValidationError
from sia_devtools.scoring import BUILTIN, llm_judge, primary_score, regex
from sia_devtools.store import regex_problem

from . import actor, config, models

PASS_THRESHOLD = 0.75


def scorers_for(settings, judge_llm=None):
    """Scorer objects for settings.loop.scorers. "judge" needs judge_llm."""
    out = []
    for s in settings["loop"]["scorers"]:
        if s == "judge":
            if judge_llm is None:
                raise ValidationError("loop.scorers includes 'judge' but no judge model is configured")
            out.append(llm_judge(judge_llm, (settings.get("agent") or {}).get("rubric") or
                                 "Is the answer correct and complete compared with the reference?"))
        elif s.startswith("regex:"):
            if problem := regex_problem(s[6:], 200):
                raise ValidationError(f"scorer {s}: {problem}")
            out.append(regex(s[6:]))
        else:
            out.append(BUILTIN[s])
    return out


def check_judge(settings, cfg):
    """G1: refuse a judge that runs on one of the actor's own models."""
    judge = settings["loop"]["judge"]
    if "judge" not in settings["loop"]["scorers"]:
        return None
    if not judge:
        raise ValidationError("loop.scorers includes 'judge' but loop.judge is not set")
    if judge["model"] in cfg["model"]["chain"] and not settings["loop"]["allow_same_model_judge"]:
        raise ValidationError(f"G1: the judge ({judge['model']}) is the actor's own model. An agent may not grade "
                              "itself for promotion; pick another judge model (or set allow_same_model_judge).")
    return judge


class Evaluator:
    """Runs configs on examples. llm_for(cfg) gives the actor's model for a config; judge_llm grades if needed. Both
    are expected to be budget-metered by the caller."""

    def __init__(self, dev, org, pid, llm_for=models.for_config, judge_llm=None):
        self.dev, self.org, self.pid = dev, org, pid
        self.settings = dev.project(org, pid)["settings"]
        self.llm_for, self.judge_llm = llm_for, judge_llm
        self.scorers = None

    def _scorers(self, cfg):
        judge = check_judge(self.settings, cfg)
        if judge and self.judge_llm is None:
            self.judge_llm = models.make(judge["provider"], [judge["model"]])
        if self.scorers is None:
            self.scorers = scorers_for(self.settings, self.judge_llm)
        return self.scorers

    def score(self, cfg, examples, trials=1):
        """Per-example results [{example_id, output, scores, score, passed, error, duration_ms}] (not stored)."""
        scorers = self._scorers(cfg)
        kinds = {s.name: s.kind for s in scorers}
        llm = self.llm_for(cfg)
        ctx = actor.context_for(self.dev, self.org, self.pid, cfg, llm)
        out = []
        for ex in examples:
            per, errors, took, last = {s.name: [] for s in scorers}, [], [], None
            for _ in range(trials):
                t0 = time.perf_counter()
                try:
                    last, err = actor.run_task(cfg, ex["input"], ctx).answer, None
                except LLMError as e:
                    last, err = None, f"LLMError: {e}"[:500]
                took.append((time.perf_counter() - t0) * 1000)
                for s in scorers:
                    if err:
                        per[s.name].append(0.0)
                        continue
                    try:
                        per[s.name].append(s(last, ex["expected_output"], ex))
                    except Exception as e:  # noqa: BLE001 - a scorer failure scores 0, like the SDK
                        per[s.name].append(0.0)
                        err = f"scorer {s.name} failed: {type(e).__name__}: {e}"[:500]
                if err:
                    errors.append(err)
            sc = {k: mean(v) for k, v in per.items()}
            score = primary_score(sc, kinds)
            out.append({"example_id": ex["id"], "input": ex["input"], "expected_output": ex["expected_output"],
                        "critical": ex["critical"], "split": ex["split"], "output": last, "scores": sc,
                        "score": score, "passed": score >= PASS_THRESHOLD, "error": errors[0] if errors else None,
                        "duration_ms": mean(took)})
        return out

    def evaluate(self, version, dataset_id, split, trials=1, name=None):
        """Evaluate a stored agent_config version on a full split and record it as a DevTools evaluation (so it can
        be compared and used to approve). Returns (evaluation, per-example results)."""
        v = self.dev.version(self.org, self.pid, config.CONFIG_NAME, version)
        cfg = config.parse(v["content"])
        snap = self.dev.dataset(self.org, self.pid, dataset_id, split=split)
        if not snap["examples"]:
            raise ValidationError(f"the {split} split of the dataset has no verified examples")
        results = self.score(cfg, snap["examples"], trials)
        scorers = self._scorers(cfg)
        ev = {"id": uuid.uuid4().hex, "name": name or f"loop:{split}", "dataset_id": dataset_id,
              "dataset_version": snap["snapshot_version"], "version_name": config.CONFIG_NAME, "version": version,
              "prompt_sha": hashlib.sha256(v["content"].encode()).hexdigest(),
              "config": {"scorers": [s.describe() for s in scorers], "trials": trials,
                         "pass_threshold": PASS_THRESHOLD, "split": split, "metadata": {"by": "sia-loop"}},
              "results": [{k: r[k] for k in ("example_id", "output", "scores", "error", "duration_ms")}
                          for r in results]}
        return self.dev.record_evaluation(self.org, self.pid, ev, actor="sia-loop"), results
