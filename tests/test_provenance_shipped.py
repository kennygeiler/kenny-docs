"""Source-PDF hashing is ARMED on the shipped data (DEMO_TICKETS.md C7).

Before this the catalog carried no pdf_sha256 and no ratified citation a doc_sha256,
so every hash check passed vacuously. Now a swapped or edited contract makes every
/doc/* route answer 409, costing refuse with reason `provenance`, and the ledger
record the mismatch — proved here by flipping one byte of a tmp copy."""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core.provenance import file_sha256  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE = os.path.join(ROOT, "cases", "santacruz")
FIRE = "firefighters_local3535_mou"
GOLDEN = "Cost an 8-hour overtime shift for a Firefighter/Paramedic (56 hr, top step)"
HEX = set("0123456789abcdef")


def _catalog(case_dir=CASE) -> list[dict]:
    with open(os.path.join(case_dir, "catalog.json")) as f:
        return json.load(f)["documents"]


def _ratified(case_dir=CASE) -> list[dict]:
    with open(os.path.join(case_dir, "rules", "rules_ratified.json")) as f:
        return json.load(f)["rules"]


def test_every_shipped_doc_is_hashed():
    for entry in _catalog():
        sha = entry.get("pdf_sha256", "")
        assert len(sha) == 64 and set(sha) <= HEX, entry["doc_id"]
        pdf = os.path.join(CASE, "sources", entry["doc_id"] + ".pdf")
        assert sha == file_sha256(pdf), entry["doc_id"]


def test_stamping_doc_sha256_does_not_change_the_rule_for_the_skeptic():
    """The baked skeptic reviews hash the rule they reviewed; arming the citation's
    doc_sha256 is bookkeeping, not a rule change (same exclusion as the approval
    fingerprint in core/provenance.py)."""
    from core import provenance, skeptic
    rule = next(r for r in _ratified() if r["id"] == f"{FIRE}:overtime_premium_rate")
    unbound = json.loads(json.dumps(rule))
    unbound["citation"].pop("doc_sha256")
    assert skeptic.rule_sha256(rule) == skeptic.rule_sha256(unbound)
    assert provenance.rule_fingerprint(rule) == provenance.rule_fingerprint(unbound)
    changed = json.loads(json.dumps(rule))
    changed["compute"] = "effective_base * 2 * hours"
    assert skeptic.rule_sha256(changed) != skeptic.rule_sha256(rule)


def test_ratified_citations_are_bound():
    hashes = {d["doc_id"]: d["pdf_sha256"] for d in _catalog()}
    rules = [r for r in _ratified() if r.get("status") == "ratified"]
    assert rules
    for r in rules:
        cit = r["citation"]
        assert cit.get("doc_sha256") == hashes[cit["doc_id"]], r["id"]


@pytest.fixture
def case_copy(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(CASE, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return str(case)


@pytest.fixture
def client(case_copy):
    from fastapi.testclient import TestClient
    return TestClient(core_app.app), case_copy


def _flip_one_byte(path: str, offset: int = 4096) -> None:
    """Same size, same name, one byte different — the quietest possible swap."""
    with open(path, "r+b") as f:
        f.seek(offset)
        b = f.read(1)
        f.seek(offset)
        f.write(bytes([b[0] ^ 0x01]))


def test_intact_copy_serves_and_costs(client):
    c, _ = client
    assert c.get(f"/doc/{FIRE}/page/8", params={"bbox": "141.418,505.662,511.182,441.459"}).status_code == 200
    assert c.get(f"/doc/{FIRE}/file").status_code == 200
    assert c.get(f"/doc/{FIRE}/clauses", params={"page": 22}).status_code == 200
    res = c.post("/chat", json={"prompt": GOLDEN}).json()
    assert res["mode"] == "costing", res
    assert res["result"]["line_items"][0]["total"] == 640.8
    assert res["result"]["line_items"][0]["citations"][0]["doc_sha256"].startswith("b30076962d29")


def test_tampered_source_is_refused(client):
    c, case_dir = client
    _flip_one_byte(os.path.join(case_dir, "sources", FIRE + ".pdf"))
    page = c.get(f"/doc/{FIRE}/page/8", params={"bbox": "141.418,505.662,511.182,441.459"})
    assert page.status_code == 409
    assert c.get(f"/doc/{FIRE}/file").status_code == 409
    clauses = c.get(f"/doc/{FIRE}/clauses", params={"page": 22})
    assert clauses.status_code == 409 and clauses.json()["reason"] == "provenance"
    res = c.post("/chat", json={"prompt": GOLDEN}).json()
    assert res["mode"] == "blocked" and res["reason"] == "provenance", res
    assert "no longer match" in res["message"]
    assert "result" not in res or not res.get("result")
    # the other contracts are untouched: a Chief Officers question still answers
    other = c.post("/chat", json={"prompt": "How much paid bereavement leave does a "
                                            "Division Chief get?"}).json()
    assert other["mode"] != "blocked", other
    events = c.get("/admin/ledger").json()
    assert events["verified"] is True
    with open(os.path.join(case_dir, "ledger.jsonl")) as f:
        kinds = [json.loads(line)["type"] for line in f if line.strip()]
    assert kinds.count("provenance.mismatch") >= 3        # page, file, clauses
    assert "costing.blocked" in kinds


# --------------------------------------------------------------------------- #
# back-fill script
# --------------------------------------------------------------------------- #
def _strip_hashes(case_dir: str) -> None:
    """Put the pre-C7 files back (same layouts: catalog indent=1, rules indent=2,
    rules file without a trailing newline)."""
    cat_path = os.path.join(case_dir, "catalog.json")
    with open(cat_path) as f:
        data = json.load(f)
    for d in data["documents"]:
        d.pop("pdf_sha256", None)
    with open(cat_path, "w") as f:
        json.dump(data, f, indent=1)
    rules_path = os.path.join(case_dir, "rules", "rules_ratified.json")
    with open(rules_path) as f:
        rules = json.load(f)
    for r in rules["rules"]:
        (r.get("citation") or {}).pop("doc_sha256", None)
    with open(rules_path, "w") as f:
        json.dump(rules, f, indent=2)


def _is_subsequence(old_lines, new_lines) -> bool:
    i = 0
    strip = lambda s: s[:-1] if s.endswith(",") else s  # noqa: E731
    for line in new_lines:
        if i < len(old_lines) and strip(line) == strip(old_lines[i]):
            i += 1
    return i == len(old_lines)


def test_backfill_is_idempotent_and_refuses_mismatch(case_copy):
    from scripts.backfill_pdf_sha import HashMismatch, backfill
    _strip_hashes(case_copy)
    cat_path = os.path.join(case_copy, "catalog.json")
    rules_path = os.path.join(case_copy, "rules", "rules_ratified.json")
    cat_before = open(cat_path, "rb").read()
    rules_before = open(rules_path, "rb").read()
    assert b"pdf_sha256" not in cat_before and b"doc_sha256" not in rules_before

    report = backfill(case_copy, verbose=False)
    assert all(d["changed"] for d in report["documents"]) and len(report["documents"]) == 5
    assert all(r["changed"] for r in report["rules"]) and len(report["rules"]) == 5
    cat_after = open(cat_path, "rb").read()
    rules_after = open(rules_path, "rb").read()
    # the back-fill only INSERTS lines: every old line survives, in order
    assert _is_subsequence(cat_before.decode().splitlines(), cat_after.decode().splitlines())
    assert _is_subsequence(rules_before.decode().splitlines(), rules_after.decode().splitlines())
    old_docs = {d["doc_id"]: d for d in json.loads(cat_before)["documents"]}
    for d in json.loads(cat_after)["documents"]:
        keys = list(d)
        assert keys[keys.index("file") + 1] == "pdf_sha256"      # ingest's key order
        assert [k for k in keys if k != "pdf_sha256"] == list(old_docs[d["doc_id"]])
        assert {k: v for k, v in d.items() if k != "pdf_sha256"} == old_docs[d["doc_id"]]
    old_rules = {r["id"]: r for r in json.loads(rules_before)["rules"]}
    for r in json.loads(rules_after)["rules"]:
        cit = dict(r["citation"])
        assert list(cit)[-1] == "doc_sha256"
        cit.pop("doc_sha256")
        assert {**r, "citation": cit} == old_rules[r["id"]]
    # the shipped files ARE this script's output, byte for byte
    assert cat_after == open(os.path.join(CASE, "catalog.json"), "rb").read()
    assert rules_after == open(os.path.join(CASE, "rules", "rules_ratified.json"), "rb").read()

    report2 = backfill(case_copy, verbose=False)
    assert not any(d["changed"] for d in report2["documents"])
    assert not any(r["changed"] for r in report2["rules"])
    assert open(cat_path, "rb").read() == cat_after
    assert open(rules_path, "rb").read() == rules_after

    # a recorded hash that disagrees with the file is never overwritten
    _flip_one_byte(os.path.join(case_copy, "sources", FIRE + ".pdf"))
    with pytest.raises(HashMismatch, match=FIRE):
        backfill(case_copy, verbose=False)
    assert open(cat_path, "rb").read() == cat_after
    assert open(rules_path, "rb").read() == rules_after


def test_backfill_cli_exit_codes(case_copy):
    from scripts.backfill_pdf_sha import main
    assert main(["--case", case_copy]) == 0          # already armed: no-op
    _flip_one_byte(os.path.join(case_copy, "sources", FIRE + ".pdf"))
    assert main(["--case", case_copy]) == 1


def test_backfill_ledger_events(case_copy):
    from scripts.backfill_pdf_sha import BASIS, backfill
    _strip_hashes(case_copy)
    backfill(case_copy, verbose=False, ledger=True)
    with open(os.path.join(case_copy, "ledger.jsonl")) as f:
        events = [json.loads(line) for line in f if line.strip()]
    mine = [e for e in events if e["type"] == "provenance.backfill"]
    assert {e["payload"]["doc_id"] for e in mine} == {d["doc_id"] for d in _catalog(case_copy)}
    assert all(e["payload"]["basis"] == BASIS and len(e["payload"]["pdf_sha256"]) == 64
               for e in mine)
