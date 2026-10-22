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
