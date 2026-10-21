"""The `sia` command: evaluate locally or in CI, gate releases, manage versions, inspect runs, run the server.

Exit codes: 0 pass / ok, 1 gate failed, 2 bad command line, 3 gate inconclusive, 4 error.
Connection: --api-key / SIA_API_KEY (+ --base-url / SIA_BASE_URL) for the hosted API; otherwise local mode with
--local / SIA_LOCAL_DB (default sia_local.db). Project: --project / SIA_PROJECT (default "default").
"""
import argparse
import importlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import httpx

from .client import SIAClient
from .errors import SIAError
from .scoring import EXIT, Scorer, gate, resolve


def load(spec):
    """'package.module:attr' or 'path/to/file.py:attr' -> the object."""
    mod, _, attr = spec.rpartition(":")
    if not mod or not attr:
        raise SystemExit(f"error: expected module:function, got {spec!r}")
    sys.path.insert(0, os.getcwd())
    if mod.endswith(".py"):
        s = importlib.util.spec_from_file_location(Path(mod).stem, mod)
        m = importlib.util.module_from_spec(s)
        s.loader.exec_module(m)
    else:
        m = importlib.import_module(mod)
    obj = m
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def scorers(specs):
    out = []
    for s in specs or ["exact_match"]:
        if ":" in s and not s.startswith("regex:"):
            obj = load(s)
            out.append(obj if isinstance(obj, Scorer) else resolve(obj))
        else:
            out.append(resolve(s))
    return out


def client(a):
    return SIAClient(a.api_key, a.project, base_url=a.base_url, local=a.local)


def say(*lines):
    print("\n".join(lines))


def write_json(path, data):
    if path == "-":
        print(json.dumps(data, indent=2, default=str))
    elif path:
        Path(path).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def summary_line(label, ev):
    s = ev["summary"]
    who = f"{ev['version_name']}@{ev['version']}" if ev["version"] is not None else (
        f"prompt {ev['prompt_sha'][:10]}" if ev["prompt_sha"] else "no prompt")
    return (f"  {label:<10} {who:<22} score {s['score']:.3f}   pass {s['pass_rate']:.0%}   errors {s['errors']}"
            f"   eval {ev['id'][:12]}")


def comparison_lines(cmp):
    return [f"  verdict    {cmp['verdict']}: {cmp['reason']}",
            f"  paired     n={cmp['n']}  mean gain {cmp['mean_gain']:+.3f}  SE {cmp['std_error']:.3f}",
            f"  regressed  {cmp['regressions'] or 'none'}" + (f"  CRITICAL {cmp['critical_regressions']}"
                                                             if cmp["critical_regressions"] else "")]


def _evaluate(c, a, *, prompt_file=None, version=None, label=None):
    prompt = Path(prompt_file).read_text(encoding="utf-8") if prompt_file else None
    return c.evaluate(a.dataset, load(a.target), scorers(a.scorer), prompt=prompt, version=version,
                      version_name=a.name, trials=a.trials, pass_threshold=a.threshold, name=label)


# --- commands ------------------------------------------------------------------
def cmd_eval_run(a):
    c = client(a)
    ev = _evaluate(c, a, prompt_file=a.prompt_file, version=a.version, label="cli")
    say(f"dataset {a.dataset} v{ev['dataset_version']} ({ev['summary']['n']} examples)", summary_line("result", ev))
    for r in ev["results"]:
        if not r["passed"]:
            say(f"    failed example {r['example_id']}: score {r['score']:.2f}" + (f"  {r['error']}" if r["error"]
                                                                                  else ""))
    write_json(a.json, ev)
    return 0


def cmd_eval_compare(a):
    cmp = client(a).compare(a.baseline, a.candidate)
    say(*comparison_lines(cmp))
    write_json(a.json, cmp)
    return 0


def cmd_eval_gate(a):
    c = client(a)
    cand = _evaluate(c, a, prompt_file=a.candidate_file, version=a.candidate_version, label="gate:candidate")
    base = None
    if a.baseline_file:
        base = _evaluate(c, a, prompt_file=a.baseline_file, label="gate:baseline")
    elif a.baseline_version not in (None, "none"):
        if a.baseline_version != "active" or c.active_version(a.name):
            base = _evaluate(c, a, version=a.baseline_version, label="gate:baseline")
    elif a.candidate_file or a.candidate_version:
        if c.active_version(a.name):  # default baseline for a prompt change: what production runs now
            base = _evaluate(c, a, version="active", label="gate:baseline")
    cmp = c.compare(base, cand) if base else None
    status, reasons = gate(cand["summary"]["score"], cmp, a.min_score, a.max_regressions, a.require_improvement)
    say(f"SIA gate: dataset {a.dataset} v{cand['dataset_version']} ({cand['summary']['n']} examples)")
    if base:
        say(summary_line("baseline", base))
    say(summary_line("candidate", cand))
    if cmp:
        say(*comparison_lines(cmp))
    say(*[f"  - {r}" for r in reasons], f"RESULT: {status.upper()}")
    write_json(a.json, {"status": status, "reasons": reasons, "exit_code": EXIT[status],
                        "candidate": {k: cand[k] for k in ("id", "dataset_id", "dataset_version", "version",
                                                           "prompt_sha", "summary", "config")},
                        "baseline": {k: base[k] for k in ("id", "version", "prompt_sha", "summary")} if base else None,
                        "comparison": {k: cmp[k] for k in ("id", "verdict", "reason", "n", "mean_gain", "std_error",
                                                           "threshold", "regressions", "critical_regressions",
                                                           "policy")} if cmp else None})
    return EXIT[status]


def cmd_improve(a):
    from sia.llm import LLM

    c = client(a)
    key = os.getenv("SIA_LLM_KEY") or os.getenv("GENAI_API_KEY") or ""
    llm = LLM(a.provider, key, a.model)
    active = c.active_version(a.name)
    base = _evaluate(c, a, version="active" if active else None, label="improve:baseline")
    say(summary_line("baseline", base))
    cand = c.propose_improvement(llm, name=a.name, evaluation=base)
    say(f"  candidate  {a.name}@{cand['version']} drafted ({len(cand['lessons'])} lessons). Evaluating...")
    cev = _evaluate(c, a, version=cand["version"], label="improve:candidate")
    cmp = c.compare(base, cev)
    say(summary_line("candidate", cev), *comparison_lines(cmp), "",
        "Nothing was approved or activated. Next:",
        f"  sia versions approve {cand['version']} --comparison {cmp['id']}" if cmp["verdict"] == "improved" else
        f"  sia versions reject {cand['version']} --reason \"{cmp['verdict']}\"   (or approve with --override)")
    write_json(a.json, {"candidate": cand, "comparison": cmp})
    return 0


def cmd_versions(a):
    c = client(a)
    if a.action == "list":
        for v in c.versions(a.name):
            say(f"{v['name']}@{v['version']:<4} {v['status']:<10}{' ACTIVE' if v['active'] else '':<8}"
                f"{(v['decision_reason'] or v['note'] or '')[:80]}")
        return 0
    if a.action == "create":
        v = c.create_version(Path(a.file).read_text(encoding="utf-8"), name=a.name, note=a.reason)
    elif a.action == "show":
        v = c.get_version(a.version, a.name)
    elif a.action == "approve":
        v = c.approve(a.version, name=a.name, comparison=a.comparison, override_reason=a.override, reviewer=a.reviewer)
    elif a.action == "reject":
        v = c.reject(a.version, name=a.name, reason=a.reason, reviewer=a.reviewer)
    else:
        v = c.activate(a.version, name=a.name, reviewer=a.reviewer)
    say(f"{v['name']}@{v['version']}: {v['status']}{' (active)' if v['active'] else ''}")
    if a.action == "show":
        say("", v["content"])
    return 0


def cmd_runs(a):
    c = client(a)
    if a.action == "show":
        say(json.dumps(c.get_run(a.run_id), indent=2, default=str))
    elif a.action == "export":
        rows = c.export_runs(a.out, status=a.status, tag=a.tag, q=a.q)
        say(f"exported {len(rows)} run(s)" + (f" to {a.out}" if a.out else ""))
    else:
        for r in c.runs(status=a.status, tag=a.tag, q=a.q, limit=a.limit, root_only=True):
            say(f"{r['id'][:12]}  {r['status']:<8} {(r['duration_ms'] or 0):>8.0f}ms  {r['name']:<30} "
                f"{(r['error'] or '')[:60]}")
    return 0


def cmd_feedback(a):
    c = client(a)
    if a.action == "list":
        for f in c.list_feedback(a.status):
            say(f"{f['id'][:12]}  {f['status']:<9} rating {f['rating']:+d}  run {f['run_id'][:12]}  "
                f"{f['category'] or ''}  {(f['explanation'] or '')[:60]}")
        return 0
    f = c.review_feedback(a.feedback_id, "verified" if a.verify else "rejected", dataset=a.dataset, note=a.note,
                          reviewer=a.reviewer)
    say(f"feedback {f['id']}: {f['status']}" + (f", added as example {f['example_id']}" if f["example_id"] else ""))
    return 0


def cmd_serve(a, local_db=None):
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("error: the server needs extra packages: pip install 'sia-devtools[server]'")
    if local_db:
        os.environ.update(SIA_LOCAL_MODE="1", SIA_DB=local_db)
        a.host = "127.0.0.1"  # local mode has no keys, so never expose it beyond this machine
        print(f"SIA dashboard (local mode, {local_db}): http://127.0.0.1:{a.port}/dashboard")
    # X-Forwarded-For/-Proto are believed only from these proxies (rate limits and HSTS rely on the real client
    # address and scheme); the default trusts a proxy on this machine only
    uvicorn.run("sia.api:app", host=a.host, port=a.port, proxy_headers=True, server_header=False,
                forwarded_allow_ips=os.getenv("SIA_TRUSTED_PROXIES", "127.0.0.1"))
    return 0


def cmd_create_tenant(a):
    from sia.store import Store
    say(json.dumps(Store(os.getenv("SIA_DB", "sia.db")).create_tenant(a.org_name), indent=2))
    return 0


def cmd_set_plan(a):
    from sia.store import Store
    ok = Store(os.getenv("SIA_DB", "sia.db")).set_plan(a.org_id, a.plan)
    say(f"{a.org_id}: plan {a.plan}" if ok else "organization not found")
    return 0 if ok else 4


def admin_request(method, path):
    """Call the hosted server's admin API with SIA_ADMIN_KEY. Returns the JSON body; exits with code 4 on errors."""
    key = os.getenv("SIA_ADMIN_KEY", "")
    if not key:
        raise SIAError("set SIA_ADMIN_KEY (and SIA_BASE_URL for a remote server)")
    url = os.getenv("SIA_BASE_URL", "http://127.0.0.1:8000").rstrip("/") + path
    try:
        r = httpx.request(method, url, headers={"X-Admin-Key": key}, timeout=30)
    except httpx.HTTPError as e:
        raise SIAError(f"could not reach {url} ({type(e).__name__})")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        raise SIAError(f"{r.status_code}: {detail}")
    return r.json()


def cmd_admin(a):
    if a.action == "requests":
        rows = admin_request("GET", "/v1/admin/access-requests" + (f"?status={a.status}" if a.status else ""))
        for r in rows:
            say(f"{r['id']}  {r['status']:<9} {time.strftime('%Y-%m-%d', time.gmtime(r['created']))}  {r['email']}  "
                f"{r['name']}" + (f" ({r['company']})" if r["company"] else "") +
                (f"\n    {r['use_case'][:200]}" if r["use_case"] else ""))
        say(f"{len(rows)} request(s)")
        return 0
    r = admin_request("POST", f"/v1/admin/access-requests/{a.request_id}/{a.action}")
    if a.action == "reject":
        say(f"request {a.request_id}: rejected")
    else:
        say(f"request {a.request_id}: approved", f"organization {r['org_name']} ({r['org_id']})",
            f"api key        {r['api_key']}", "", f"Send this key to {r['email']} over a private channel. "
            "It is shown only once and can't be read back; rotate it if it leaks.")
    return 0


def ops(name):
    """Run a server operator command from sia/ops.py, mapping its errors onto exit code 4."""
    def run(a):
        from sia import ops as o
        try:
            return getattr(o, name)(a)
        except o.OpsError as e:
            raise SIAError(str(e))
    return run


# --- argument parsing ------------------------------------------------------------
def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=os.getenv("SIA_PROJECT", "default"))
    common.add_argument("--api-key", default=None, help="hosted API key (default: $SIA_API_KEY)")
    common.add_argument("--base-url", default=None, help="hosted API URL (default: $SIA_BASE_URL)")
    common.add_argument("--local", default=None, help="local mode database (default: $SIA_LOCAL_DB or sia_local.db)")
    common.add_argument("--name", default="system_prompt", help="which versioned prompt (default system_prompt)")

    ev = argparse.ArgumentParser(add_help=False)
    ev.add_argument("--dataset", required=True)
    ev.add_argument("--target", required=True, help="module:function called as f(input) or f(input, prompt)")
    ev.add_argument("--scorer", action="append", help="built-in (exact_match, contains, json_match), "
                                                       "regex:<pattern> or module:function; repeatable")
    ev.add_argument("--trials", type=int, default=1)
    ev.add_argument("--threshold", type=float, default=0.75, help="per-example pass threshold")
    ev.add_argument("--json", help="write machine-readable results to this file ('-' = stdout)")

    p = argparse.ArgumentParser(prog="sia", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("eval", help="run, compare and gate evaluations").add_subparsers(dest="action", required=True)
    r = e.add_parser("run", parents=[common, ev], help="evaluate a target on a dataset")
    r.add_argument("--prompt-file")
    r.add_argument("--version", help="version number or 'active'")
    r.set_defaults(fn=cmd_eval_run)
    c = e.add_parser("compare", parents=[common], help="compare two stored evaluations")
    c.add_argument("baseline")
    c.add_argument("candidate")
    c.add_argument("--json")
    c.set_defaults(fn=cmd_eval_compare)
    g = e.add_parser("gate", parents=[common, ev], help="CI gate: evaluate a candidate (and baseline) and decide")
    g.add_argument("--candidate-file", help="prompt file to test (e.g. the one changed in this pull request)")
    g.add_argument("--candidate-version", help="stored version number to test")
    g.add_argument("--baseline-file")
    g.add_argument("--baseline-version", help="version number, 'active' (default when a candidate prompt is given) "
                                              "or 'none'")
    g.add_argument("--min-score", type=float)
    g.add_argument("--max-regressions", type=int)
    g.add_argument("--require-improvement", action="store_true")
    g.set_defaults(fn=cmd_eval_gate)

    i = sub.add_parser("improve", parents=[common, ev], help="draft a candidate from failures, evaluate, compare")
    i.add_argument("--provider", default="gemini")
    i.add_argument("--model", default=None)
    i.set_defaults(fn=cmd_improve)

    v = sub.add_parser("versions", parents=[common], help="list, create, approve, reject, activate versions")
    v.add_argument("action", choices=["list", "show", "create", "approve", "reject", "activate"])
    v.add_argument("version", type=int, nargs="?")
    v.add_argument("--file", help="create: prompt file")
    v.add_argument("--comparison", help="approve: comparison id that shows the improvement")
    v.add_argument("--override", help="approve without an 'improved' comparison; the reason is audited")
    v.add_argument("--reason")
    v.add_argument("--reviewer")
    v.set_defaults(fn=cmd_versions)

    rn = sub.add_parser("runs", parents=[common], help="inspect recorded runs")
    rn.add_argument("action", choices=["list", "show", "export"])
    rn.add_argument("run_id", nargs="?")
    rn.add_argument("--status")
    rn.add_argument("--tag")
    rn.add_argument("--q", help="search text")
    rn.add_argument("--limit", type=int, default=20)
    rn.add_argument("--out", help="export: JSON lines file")
    rn.set_defaults(fn=cmd_runs)

    f = sub.add_parser("feedback", parents=[common], help="list and review feedback")
    f.add_argument("action", choices=["list", "review"])
    f.add_argument("feedback_id", nargs="?")
    f.add_argument("--status")
    f.add_argument("--verify", action="store_true")
    f.add_argument("--reject", action="store_true")
    f.add_argument("--dataset", help="review --verify: add the correction to this dataset")
    f.add_argument("--note")
    f.add_argument("--reviewer")
    f.set_defaults(fn=cmd_feedback)

    s = sub.add_parser("serve", help="run the hosted API and dashboard")
    s.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.getenv("PORT", 8000)))
    s.set_defaults(fn=cmd_serve)
    d = sub.add_parser("dashboard", help="dashboard for local mode (no keys, this machine only)")
    d.add_argument("--db", default=os.getenv("SIA_LOCAL_DB", "sia_local.db"))
    d.add_argument("--port", type=int, default=8000)
    d.set_defaults(fn=lambda a: cmd_serve(a, a.db), host="127.0.0.1")

    t = sub.add_parser("create-tenant", aliases=["create-org"], help="create an organization and print its key")
    t.add_argument("org_name")
    t.set_defaults(fn=cmd_create_tenant)
    sp = sub.add_parser("set-plan", help="change an organization's plan (admin)")
    sp.add_argument("org_id")
    sp.add_argument("plan", choices=["free", "pro", "team"])
    sp.set_defaults(fn=cmd_set_plan)
    ad = sub.add_parser("admin", help="access requests on a hosted server (uses SIA_BASE_URL and SIA_ADMIN_KEY)")
    ad.add_argument("action", choices=["requests", "approve", "reject"])
    ad.add_argument("request_id", nargs="?")
    ad.add_argument("--status", choices=["pending", "approved", "rejected"])
    ad.set_defaults(fn=cmd_admin)

    db = argparse.ArgumentParser(add_help=False)
    db.add_argument("--db", default=os.getenv("SIA_DB", "sia.db"), help="server database (default: $SIA_DB or sia.db)")
    b = sub.add_parser("backup", parents=[db], help="copy the server database safely, check it, keep the newest N")
    b.add_argument("--dir", default="backups")
    b.add_argument("--keep", type=int, default=14)
    b.set_defaults(fn=ops("backup_cmd"))
    rs = sub.add_parser("restore", parents=[db], help="replace the server database with a backup (server stopped)")
    rs.add_argument("file")
    rs.set_defaults(fn=ops("restore_cmd"))
    return p


def main(argv=None):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    p = parser()
    a = p.parse_args(argv)
    if a.cmd == "versions" and a.action not in ("list", "create") and a.version is None:
        p.error(f"versions {a.action} needs a version number")
    if a.cmd == "versions" and a.action == "create" and not a.file:
        p.error("versions create needs --file")
    if a.cmd == "runs" and a.action == "show" and not a.run_id:
        p.error("runs show needs a run id")
    if a.cmd == "admin" and a.action != "requests" and not a.request_id:
        p.error(f"admin {a.action} needs a request id (see: sia admin requests)")
    if a.cmd == "feedback" and a.action == "review" and (not a.feedback_id or a.verify == a.reject):
        p.error("feedback review needs an id and exactly one of --verify / --reject")
    try:
        return a.fn(a)
    except SIAError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
