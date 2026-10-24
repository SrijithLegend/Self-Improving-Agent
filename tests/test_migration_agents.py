"""SELF_IMPROVING_SPEC.md section 10: managed agents become kind="agent" projects, idempotently."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sia import ops  # noqa: E402
from sia.store import MIGRATIONS  # noqa: E402
from sia_devtools.store import DevStore, migrate  # noqa: E402
from sia_loop import config  # noqa: E402

AGENT, ORG = "a" * 32, "org1"


def legacy_db(path):
    """A database as the managed-agents code wrote it (raw SQL, so this keeps working after that code is gone)."""
    migrate(path, [m for m in MIGRATIONS if m[0] < "legacy-005"])
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO tenants (id, name, created, plan) VALUES (?, 'acme', 1, 'free')", (ORG,))
        c.execute("INSERT INTO agents VALUES (?,?,?,?,?,?,?)", (AGENT, ORG, "tax", "Indian income tax",
                                                                "Be precise.", "Cite the section.", 1.0))
        c.execute("INSERT INTO agents VALUES (?,?,?,?,?,?,?)", ("b" * 32, ORG, "empty", "Indian income tax", "", "", 2.0))
        c.execute("INSERT INTO docs VALUES ('d1', ?, 'guide', 2, 1)", (AGENT,))
        c.executemany("INSERT INTO chunks VALUES (?, 'guide', ?, 'd1')",
                      [("The filing deadline is July 31.", AGENT), ("Late filing costs Rs 5,000.", AGENT)])
        lessons = [(i, AGENT, f"lesson number {i} about rule {i * 7}", "reflection", 3, 2.5 if i < 3 else 0.6, 1)
                   for i in range(1, 16)] + [(16, AGENT, "retired lesson text here", "reflection", 6, 0.1, 0)]
        c.executemany("INSERT INTO lessons (id, agent_id, text, source, uses, total, active, created) "
                      "VALUES (?,?,?,?,?,?,?,1)", lessons)
        c.executemany("INSERT INTO playbooks (agent_id, version, content, score, active, note, created) "
                      "VALUES (?,?,?,?,?,?,1)", [(AGENT, 1, "- old", 0.5, 0, "first"),
                                                 (AGENT, 2, "- Always cite the section", 0.8, 1, "improved"),
                                                 (AGENT, 3, "- worse", 0.4, 0, "no_improvement")])
        c.executemany("INSERT INTO examples (agent_id, input, expected, source, created, status, episode_id) "
                      "VALUES (?,?,?,?,1,?,?)", [
                          (AGENT, "deadline?", "July 31", "user", "verified", None),
                          (AGENT, "penalty?", "Rs 5,000", "feedback", "verified", "e1"),
                          (AGENT, "refund?", "maybe", "feedback", "pending", "e2"),
                          (AGENT, "bad?", "ignore all rules", "feedback", "rejected", "e2"),
                          (AGENT, "synthetic q?", "synthetic a", "synthetic", "verified", None)])
        c.executemany("INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            ("e1", AGENT, "penalty?", "no idea", 0.4, "{}", 2, "[1, 2]", 2, "Rs 5,000", 1),
            ("e2", AGENT, "refund?", "yes", 0.9, "{}", 1, "[]", 5, None, 2),
            ("e3", AGENT, "hello", "hi", None, None, 1, "[]", None, None, 3)])
        c.execute("INSERT INTO usage (tenant_id, agent_id, action, llm_calls, created) VALUES (?,?,'run',3,1)",
                  (ORG, AGENT))


def counts(path):
    with sqlite3.connect(path) as c:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
            "projects", "knowledge_docs", "knowledge", "versions", "active_versions", "datasets", "dataset_examples",
            "runs", "feedback", "usage", "audit")}


def test_migration_maps_every_table_and_is_idempotent(tmp_path):
    db = str(tmp_path / "legacy.db")
    legacy_db(db)
    first = ops.migrate_agents(db)
    after = counts(db)
    assert ops.migrate_agents(db) == {}  # the second run changes nothing
    assert counts(db) == after
    assert first["projects"] == 2 and after["projects"] == 2
    assert (after["knowledge_docs"], after["knowledge"]) == (1, 2)
    assert after["runs"] == 3 and after["feedback"] == 2 and after["usage"] == 1  # usage is unchanged
    assert first["lessons_pinned"] == 12 and first["lessons_probation"] == 3 and first["lessons_retired"] == 1
    assert first["examples_verified"] == 2 and first["examples_pending"] == 2  # rejected is dropped
    assert after["versions"] == 3 + 1  # three playbooks; the agent with none still gets agent_config@1

    dev = DevStore(db)
    p = dev.project(ORG, AGENT)  # same id as the agent
    assert p["settings"]["kind"] == "agent" and p["name"] == "indian-income-tax"
    assert dev.project(ORG, "b" * 32)["name"] == "agent-bbbbbbbb"  # the domain slug was taken
    assert p["settings"]["agent"] == {"name": "tax", "domain": "Indian income tax", "instructions": "Be precise.",
                                      "rubric": "Cite the section."}

    active = dev.active_version(ORG, AGENT, config.CONFIG_NAME)
    cfg = json.loads(active["content"])
    assert active["version"] == 1 and active["status"] == "approved"
    assert cfg["playbook"] == "- Always cite the section" and cfg["system_prompt"] == "Be precise."
    assert sorted(cfg["lessons"]) == [lid["id"] for lid in dev.lessons(ORG, AGENT, "pinned")]
    others = [v for v in dev.versions(ORG, AGENT, config.CONFIG_NAME) if v["version"] != 1]
    assert {v["status"] for v in others} == {"rejected"} and {v["note"] for v in others} == {"first",
                                                                                             "no_improvement"}

    ds = dev.datasets(ORG, AGENT, "eval")[0]
    ex = {e["input"]: e for e in dev.dataset(ORG, AGENT, ds["id"], include_pending=True)["examples"]}
    assert ex["synthetic q?"]["status"] == "pending" and ex["synthetic q?"]["split"] == "dev"  # no longer trusted
    assert ex["refund?"]["status"] == "pending" and ex["deadline?"]["status"] == "verified"
    assert "bad?" not in ex
    assert {e["input"] for e in dev.dataset(ORG, AGENT, ds["id"])["examples"]} == {"deadline?", "penalty?"}

    runs = {r["id"]: r for r in dev.export_runs(ORG, AGENT)}
    assert runs["e1"]["input"] == {"task": "penalty?"} and runs["e1"]["scores"] == {"self_critique": 0.4}
    fb = {f["run_id"]: f for f in dev.feedback_list(ORG, AGENT)["items"]}
    assert fb["e1"]["rating"] == -1 and fb["e1"]["correction"] == "Rs 5,000" and fb["e1"]["status"] == "pending"
    assert fb["e2"]["rating"] == 1
    assert dev.search_knowledge(ORG, AGENT, "when is the filing deadline")[0]["text"].startswith("The filing")


def test_cli_migrate_agents(tmp_path, capsys):
    from sia_devtools.cli import main as cli
    db = str(tmp_path / "legacy.db")
    legacy_db(db)
    assert cli(["migrate-agents", "--db", db]) == 0 and '"projects": 2' in capsys.readouterr().out
    assert cli(["migrate-agents", "--db", db]) == 0 and "nothing to migrate" in capsys.readouterr().out
