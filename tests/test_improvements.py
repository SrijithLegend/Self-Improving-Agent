"""Post-launch improvements (docs/IMPROVEMENTS.md): example dedupe, India redactors, ops checks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sia_devtools import SIAClient  # noqa: E402
from test_devtools import hosted, make_api, org  # noqa: E402


# --- 1. re-running a script must not duplicate dataset examples ---------------------------------------------------
def quickstart(sia):
    ds = sia.dataset("regressions")
    eid = ds.add_example({"question": "Is clearance returnable?"}, "DECISION: NO")
    return ds, eid


def test_rerunning_a_script_keeps_the_dataset_the_same(tmp_path, capsys):
    db = str(tmp_path / "local.db")
    ds1, id1 = quickstart(SIAClient(project="p", local=db))
    ds2, id2 = quickstart(SIAClient(project="p", local=db))  # the same script, run again
    assert id1 == id2 and len(ds2.examples()) == 1 and ds2.version == ds1.version
    assert "skipped 1 duplicate" in capsys.readouterr().err


def test_duplicates_in_one_batch_and_the_opt_out(tmp_path):
    ds = SIAClient(project="p", local=str(tmp_path / "local.db")).dataset("d")
    ids = ds.add_examples([{"input": "a", "expected_output": "1"}, {"input": "a", "expected_output": "1"},
                           {"input": "a", "expected_output": "2"}, {"input": "b", "expected_output": "1"}])
    assert ids[0] == ids[1] and len(set(ids)) == 3 and len(ds.examples()) == 3
    assert ds.add_example("a", "1", allow_duplicate=True) not in ids  # a real need: added again on request
    assert len(ds.examples()) == 4
    # an edited-away example no longer counts: adding it back is a real add
    ds.update_example(ids[3], expected_output="changed")
    assert ds.add_example("b", "1") not in ids and len(ds.examples()) == 5


def test_hosted_api_skips_duplicates_and_counts_only_new_ones_against_the_plan(tmp_path, monkeypatch):
    api, c = make_api(tmp_path, monkeypatch)
    o = org(c)
    monkeypatch.setitem(api.PLANS["free"], "examples_per_dataset", 2)
    sia = hosted(api, o["api_key"])
    ds = sia.dataset("d")
    first = ds.add_examples([{"input": "a", "expected_output": "1"}, {"input": "b", "expected_output": "2"}])
    again = ds.add_examples([{"input": "a", "expected_output": "1"}, {"input": "b", "expected_output": "2"}])
    assert first == again and len(ds.examples()) == 2  # at the plan limit, a re-run is not refused
    h = {"Authorization": f"Bearer {o['api_key']}"}
    r = c.post(f"/v1/projects/{sia.project_id}/datasets/{ds.id}/examples", headers=h,
               json={"examples": [{"input": "a", "expected_output": "1"}]})
    assert r.status_code == 201 and r.json() == {"ids": [first[0]], "skipped": 1}
    r = c.post(f"/v1/projects/{sia.project_id}/datasets/{ds.id}/examples", headers=h,
               json={"examples": [{"input": "c", "expected_output": "3"}]})
    assert r.status_code == 402  # a genuinely new example past the limit still is


# --- 2. redaction for the Indian market and more secret formats ---------------------------------------------------
def aadhaar_like(base="23456789012"):
    from sia_devtools.store import _verhoeff
    return next(base + str(d) for d in range(10) if _verhoeff(base + str(d)))


def test_verhoeff_matches_the_reference_example():
    from sia_devtools.store import _verhoeff
    assert _verhoeff("2363") and not _verhoeff("2364")  # 236 -> check digit 3 (the standard worked example)
    assert sum(_verhoeff("23456789012" + str(d)) for d in range(10)) == 1  # exactly one check digit fits


def test_aadhaar_and_pan_are_opt_in_and_checked():
    from sia_devtools.store import DEFAULT_REDACT, redactor
    good = aadhaar_like()
    bad = good[:-1] + str((int(good[-1]) + 1) % 10)  # wrong check digit: an order id, not an Aadhaar
    spaced = f"{good[:4]} {good[4:8]} {good[8:]}"
    text = f"My Aadhaar is {spaced}, also {good}, order {bad}, PAN ABCPE1234F, ref ABCXE1234F, 1{good}"
    assert redactor(DEFAULT_REDACT)(text) == text  # opt-in: the defaults leave them alone
    out = redactor(DEFAULT_REDACT + ["aadhaar", "pan"])(text)
    assert out == (f"My Aadhaar is [REDACTED:aadhaar], also [REDACTED:aadhaar], order {bad}, PAN [REDACTED:pan], "
                   f"ref ABCXE1234F, 1{good}")  # X isn't a PAN holder type; a 13-digit run isn't an Aadhaar
    assert redactor(["aadhaar"])(f"{good[:4]}-{good[4:8]} {good[8:]}") == f"{good[:4]}-{good[4:8]} {good[8:]}"


def test_more_secret_formats_are_masked_by_default():
    from sia_devtools.store import DEFAULT_REDACT, redactor
    scrub = redactor(DEFAULT_REDACT)
    secrets = ["ghp_" + "a1B2" * 9, "github_pat_" + "11ABCDEFG0" * 3, "xoxb-1234567890-abcdefghij",
               "xoxp-1234567890-0987654321-abc", "AKIAIOSFODNN7EXAMPLE", "rzp_live_AbCdEf123456"]
    for s in secrets:
        assert scrub(f"token={s} end") == "token=[REDACTED:secrets] end", s
    for keep in ("rzp_test_AbCdEf123456", "AKIA short", "the ghp_ prefix", "xoxb"):
        assert scrub(keep) == keep, keep


def test_project_setting_redacts_aadhaar_before_storage(tmp_path):
    from sia_devtools import SIAClient
    sia = SIAClient(project="support-bot", local=str(tmp_path / "local.db"))
    sia.update_settings(redact=["secrets", "cards", "aadhaar", "pan"])
    good = aadhaar_like()
    ds = sia.dataset("d")
    ds.add_example({"question": f"Update my Aadhaar {good} and PAN ABCPE1234F"}, "OK")
    assert ds.examples()[0]["input"] == {"question": "Update my Aadhaar [REDACTED:aadhaar] and PAN [REDACTED:pan]"}


# --- 5. async apps can trace -------------------------------------------------------------------------------------
def test_trace_works_on_async_functions_with_concurrent_tasks(tmp_path):
    import asyncio

    import pytest
    sia = SIAClient(project="p", local=str(tmp_path / "local.db"))

    @sia.trace
    async def retrieve(q):
        await asyncio.sleep(0.01)
        return f"docs for {q}"

    @sia.trace("answer")
    async def answer(q):
        docs = await retrieve(q)
        if q == "boom":
            raise ValueError("model timed out")
        return f"{q}: {docs}"

    async def main():
        out = await asyncio.gather(answer("a"), answer("b"), answer("c"))
        with pytest.raises(ValueError):
            await answer("boom")
        return out

    assert asyncio.run(main()) == ["a: docs for a", "b: docs for b", "c: docs for c"]
    runs = sia.runs(limit=50)
    roots = {r["input"]["q"]: r for r in runs if r["name"] == "answer"}
    children = [r for r in runs if r["name"].endswith("retrieve")]
    assert set(roots) == {"a", "b", "c", "boom"} and len(children) == 4
    for child in children:  # each concurrent task's child points at its own parent, not a sibling's
        assert roots[child["input"]["q"]]["id"] == child["parent_id"]
    assert roots["boom"]["status"] == "error" and "model timed out" in roots["boom"]["error"]
    assert roots["a"]["output"] == "a: docs for a" and roots["a"]["status"] == "success"


# --- 6. operations: access logs actually print, error-rate alerts, optional Sentry -------------------------------
def test_access_lines_are_emitted_as_json(tmp_path, monkeypatch):
    import json
    import logging
    api, c = make_api(tmp_path, monkeypatch)
    lines = []
    h = logging.Handler()
    h.emit = lambda record: lines.append(record.getMessage())
    logging.getLogger("sia.access").addHandler(h)
    try:
        c.get("/health?token=secret-value")
    finally:
        logging.getLogger("sia.access").removeHandler(h)
    assert logging.getLogger("sia").handlers and logging.getLogger("sia.access").isEnabledFor(logging.INFO)
    rec = json.loads(lines[-1])
    assert (rec["method"], rec["path"], rec["status"]) == ("GET", "/health", 200) and "secret-value" not in lines[-1]


def test_logcheck_alerts_on_5xx_rate_and_security_events(tmp_path, capsys):
    import json
    from sia_devtools import cli
    now = 1_800_000_000.0
    line = lambda ts, status: json.dumps({"ts": ts, "method": "GET", "path": "/v1/x", "status": status})  # noqa: E731
    ok = [line(now - 60, 200)] * 18
    bad = [line(now - 30, 500)] * 2 + [line(now - 3600, 500)] * 50  # the old errors are outside the window
    sec = [json.dumps({"ts": now - 10, "event": "invalid_admin_key"})] * 3
    noise = ["INFO:     Started server process", "2026-10-22T10:00:00Z " + line(now - 5, 503), "{not json"]

    from sia import ops
    assert ops.logcheck(ok + bad + sec, now=now) == 1  # 2 of 20 in the window = 10%, over the 5% default
    out = json.loads(capsys.readouterr().out)
    assert (out["requests"], out["errors_5xx"], out["error_rate"]) == (20, 2, 0.1)
    assert out["security_events"] == {"invalid_admin_key": 3} and out["reasons"] == ["5xx rate 10.0% over 5.0%"]
    assert ops.logcheck(ok + bad, now=now, max_error_rate=0.10) == 0  # exactly at the threshold: no alert
    capsys.readouterr()
    assert ops.logcheck(ok + bad + noise, now=now, max_error_rate=0.05) == 1  # 3 of 21 > 5%
    assert json.loads(capsys.readouterr().out)["errors_5xx"] == 3  # the docker-prefixed line counts
    assert ops.logcheck(bad, now=now) == 0  # 2 requests: below min_requests, no alert on tiny samples
    capsys.readouterr()
    assert ops.logcheck(ok + sec, now=now, max_security_events=2) == 1
    capsys.readouterr()
    f = tmp_path / "server.log"
    f.write_text("\n".join(ok), encoding="utf-8")
    assert cli.main(["logcheck", str(f), "--minutes", "5"]) == 0


def test_sentry_reports_unhandled_errors_without_headers_or_bodies(tmp_path, monkeypatch):
    import types
    sent = {"init": None, "captured": 0, "tags": {}}

    class Scope:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def set_tag(self, k, v):
            sent["tags"][k] = v

        def set_context(self, k, v):
            sent[k] = v

    fake = types.SimpleNamespace(init=lambda **kw: sent.update(init=kw), new_scope=Scope,
                                 capture_exception=lambda: sent.update(captured=sent["captured"] + 1))
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    api, c = make_api(tmp_path, monkeypatch, SENTRY_DSN="https://key@example.ingest.sentry.io/1")
    assert sent["init"]["send_default_pii"] is False and sent["init"]["default_integrations"] is False
    assert sent["init"]["include_local_variables"] is False and sent["init"]["max_request_body_size"] == "never"

    def boom():
        raise RuntimeError("db exploded")
    monkeypatch.setattr(api.billing, "settings", boom)
    from fastapi.testclient import TestClient
    r = TestClient(api.app, raise_server_exceptions=False).get("/v1/plans", headers={"X-LLM-Key": "sk-live-abc"})
    assert r.status_code == 500 and sent["captured"] == 1
    assert sent["tags"]["request_id"] == r.json()["request_id"] and sent["request"] == {"method": "GET",
                                                                                         "path": "/v1/plans"}
    scrubbed = api.sentry_scrub({"request": {"method": "POST", "url": "https://h/v1/x?key=abc", "data": {"a": 1},
                                             "headers": {"X-LLM-Key": "sk-1"}, "cookies": "c"},
                                 "user": {"ip_address": "1.2.3.4"}})
    assert scrubbed == {"request": {"method": "POST", "url": "https://h/v1/x"}}
    frames = [{"function": "f", "vars": {"key": "'sia_SUPERSECRETKEY123'"}}]
    scrubbed = api.sentry_scrub({"exception": {"values": [{"type": "E", "stacktrace": {"frames": frames}}]}})
    assert "vars" not in str(scrubbed) and "sia_" not in str(scrubbed)


def test_sentry_dsn_without_the_package_is_a_warning_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # import fails
    api, c = make_api(tmp_path, monkeypatch, SENTRY_DSN="https://key@example.ingest.sentry.io/1")
    assert api.sentry is None and c.get("/health").status_code == 200
