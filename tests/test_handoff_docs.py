"""The handoff surface says what is true and points at things that exist
(DEMO_TICKETS.md K4, K5).

README sentences the 2026-10-07 audit found false must stay gone; STATUS.md must keep
its ten sections; every relative link in the two handoff pages must resolve; every
`TICKETS.md <id>` comment in code must still find its heading in the archive; and every
question the README promises an answer for must be in the smoke table.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import demo_smoke  # noqa: E402


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    return out.stdout.split("\n") if out.returncode == 0 else []


# --------------------------------------------------------------------------- #
# K4: README
# --------------------------------------------------------------------------- #
def test_readme_has_none_of_the_false_sentences():
    readme = _read("README.md")
    for bad in ("No rules exist yet", "not the ~33", "§11 (pitfalls)", "read as *pending*",
                "config-not-code", "deterministic fallbacks cover every LLM touchpoint",
                "routed to the right document", "Four green cards", "approver: 'kenny'"):
        assert bad not in readme, bad


def test_readme_states_what_the_gate_checks_and_who_wrote_the_rules():
    readme = _read("README.md")
    for must in ("fires in no known answer", "(R1)", "(R2)", "(R3)", "(R4)", "(R5)",
                 "one set of inputs", "produced none of them", "longevity_10yr",
                 "has not yet been ratified by a human", "KENNY_LLM=off",
                 "/chat/replay/", "Try to break it", "OCR'd scan", "Skeptic",
                 "STATUS.md", "DEMO_TICKETS.md", "six known answers"):
        assert must in readme, must


def test_readme_architecture_references_resolve():
    readme, arch = _read("README.md"), _read("ARCHITECTURE.md")
    headings = {m for m in re.findall(r"^## (\d+)\.", arch, flags=re.M)}
    refs = set(re.findall(r"ARCHITECTURE §(\d+)", readme))
    assert refs and refs <= headings, (refs, headings)


def test_readme_admin_tabs_match_the_page():
    readme, admin = _read("README.md"), _read("core", "templates", "admin.html")
    tabs = re.findall(r'role="tab"[^>]*>([^<]+?)\s*<span', admin)
    assert tabs, "admin tabs not found"
    for t in tabs:
        assert f"**{t.strip()}**" in readme, t
    assert 'id="approverName"' in admin and "approver's name" in readme


def test_readme_demo_questions_are_in_the_smoke_table():
    readme = re.sub(r"\s+", " ", _read("README.md"))   # prose wraps the prompts
    chat = _read("core", "templates", "chat.html")
    for q in re.findall(r'data-q="([^"]+)"', chat):
        assert q in readme, q
    for q in (demo_smoke.Q_OVERTIME, demo_smoke.Q_BEREAVEMENT, demo_smoke.Q_OFF_CORPUS,
              demo_smoke.Q_REGULAR):
        assert q in readme, q


def test_readme_live_rule_facts_match_the_library():
    import json
    lib = json.load(open(os.path.join(ROOT, "cases", "santacruz", "rules", "rules_ratified.json")))
    rules = {r["id"]: r for r in lib["rules"]}
    assert len(rules) == 5
    lon = rules["firefighters_local3535_mou:longevity_10yr"]["approver"]
    assert lon.startswith("analyst (hand-authored 2026-10-07") and "claude" not in lon.lower()
    humans = [r for r in rules.values() if r["approver"] == "kenny"]
    assert len(humans) == 4
    readme = _read("README.md")
    assert "four original" in readme and "The fifth" in readme


def test_architecture_and_prd_no_longer_overclaim():
    arch, prd = _read("ARCHITECTURE.md"), _read("PRD.md")
    assert "Nothing in `core/` is\ncase-specific" not in arch
    assert "`core/` is case-agnostic." not in arch
    assert "Its two verification scenarios" not in arch
    assert "synthetic sample corpus" not in prd
    assert "two known answers, both green" not in prd
    assert "One\nlogin opens both surfaces" not in prd


# --------------------------------------------------------------------------- #
# K5: STATUS.md, the archive, links
# --------------------------------------------------------------------------- #
STATUS_SECTIONS = ["## a. Last verified", "## b. Names", "## c. What is running where",
                   "## d. What works", "## e. Known wrong today", "## f. Hazards",
                   "## g. Local-only state", "## h. Deploy policy", "## i. Where things are",
                   "## j. Next"]


def test_status_has_the_ten_sections_in_order():
    status = _read("STATUS.md")
    pos = [status.find(h) for h in STATUS_SECTIONS]
    assert all(p >= 0 for p in pos), [h for h, p in zip(STATUS_SECTIONS, pos) if p < 0]
    assert pos == sorted(pos)
    for must in ("scripts/demo_smoke.py", "backfill_quote_sha.py", "backfill_provenance.py",
                 "longevity_10yr", "code_rev", "railway up", "Re-read all documents",
                 "linear.app", "github.com/kennygeiler/kenny-docs", "3b326a6",
                 "test_lookup_names_the_classification_it_could_not_find"):
        assert must in status, must


def _links(text):
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        yield target.split("#")[0]


def test_every_relative_link_in_the_handoff_pages_resolves():
    for name in ("STATUS.md", "README.md", "DEMO_TICKETS.md"):
        base = os.path.dirname(os.path.join(ROOT, name))
        for target in _links(_read(name)):
            assert os.path.exists(os.path.join(base, target)), (name, target)
    for name in ("TICKETS.md", "OCR_TICKETS.md", "README.md"):
        base = os.path.join(ROOT, "archive", "backlog-2026-08-14")
        for target in _links(_read("archive", "backlog-2026-08-14", name)):
            assert os.path.exists(os.path.join(base, target)), (name, target)


def test_old_backlogs_are_archived_with_corrections():
    tracked = _tracked()
    tickets = sorted(p for p in tracked if p.endswith("TICKETS.md"))
    if tickets:  # git available
        assert tickets == ["DEMO_TICKETS.md", "archive/backlog-2026-08-14/OCR_TICKETS.md",
                           "archive/backlog-2026-08-14/TICKETS.md"], tickets
    assert not os.path.exists(os.path.join(ROOT, "TICKETS.md"))
    assert not os.path.exists(os.path.join(ROOT, "OCR_TICKETS.md"))
    old = _read("archive", "backlog-2026-08-14", "TICKETS.md")
    ocr = _read("archive", "backlog-2026-08-14", "OCR_TICKETS.md")
    assert "STATUS (2026-08-14): implemented." not in old
    assert "The backlog is complete." not in ocr
    for text in (old, ocr):
        assert "Archived 2026-10-07" in text and "Re-audited 2026-10-07" in text
        assert "| Id | Claimed (2026-08-14) | Actual on shipped data (2026-10-07) |" in text
    # the bodies survived the move
    assert "### A1. Hash source PDFs into the provenance chain" in old
    assert "## OCR-1 — Document X-ray overlay" in ocr
    assert "TICKETS.md <id>" in _read("archive", "backlog-2026-08-14", "README.md")


def test_code_comments_that_cite_the_old_backlogs_still_resolve():
    old = _read("archive", "backlog-2026-08-14", "TICKETS.md")
    ocr = _read("archive", "backlog-2026-08-14", "OCR_TICKETS.md")
    headings = set(re.findall(r"^### ([A-G]\d+)\.", old, flags=re.M))
    headings |= set(re.findall(r"^## (OCR-\d+)", ocr, flags=re.M))
    pat = re.compile(r"(?<!DEMO_)TICKETS\.md\s+((?:OCR-)?[A-Z]\d+)")
    seen = 0
    for sub in ("core", "scripts", "tests"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, sub)):
            for fn in files:
                if not fn.endswith((".py", ".html", ".js", ".css", ".sh")):
                    continue
                text = open(os.path.join(dirpath, fn), encoding="utf-8", errors="ignore").read()
                for ref in pat.findall(text):
                    seen += 1
                    assert ref in headings, (fn, ref)
    assert seen >= 40


def test_demo_tickets_carries_the_state_after_wave_1_note():
    demo = _read("DEMO_TICKETS.md")
    head = demo[:4000]
    assert "State after wave 1" in head and "9129f7d" in head and "STATUS.md" in head
