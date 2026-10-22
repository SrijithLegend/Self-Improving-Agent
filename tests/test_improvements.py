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
