#!/usr/bin/env python
"""Arm source-PDF hashing on a shipped case (DEMO_TICKETS.md C7).

The shipped catalog was baked before ingest recorded `pdf_sha256`, and no ratified
citation carried `doc_sha256`, so every hash check passed vacuously: a swapped or
edited contract was served, highlighted and costed with no warning. This stamps the
hashes, parse-free and idempotent:

  * each catalog entry whose PDF is on disk gets `pdf_sha256` when absent — when it
    is present and DIFFERENT the script prints the two values and exits 1 (a hash is
    a claim about bytes; it is never overwritten by a script);
  * each rule in rules_ratified.json (and rules_proposed.json when present) whose
    citation has an empty `doc_sha256` gets its document's hash.

Every other key of every entry stays byte-identical and each file keeps its own
indentation (catalog indent=1, rules indent=2). Running it twice is a no-op.
tests/test_provenance_shipped.py proves all of it. With --ledger, a
`provenance.backfill` event per stamped document is appended to the case ledger
(off by default so the shipped case directory gains no runtime file).

    python scripts/backfill_pdf_sha.py --case cases/santacruz
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from core.catalog import Catalog  # noqa: E402
from core.provenance import file_sha256  # noqa: E402
from scripts.backfill_text_origin import _detect_indent, _save_in_place  # noqa: E402

BASIS = "bytes on disk at backfill; original ingest predates hashing"


class HashMismatch(RuntimeError):
    """A catalog entry already carries a hash that differs from its file."""


def _declared_files(case_dir: str) -> dict[str, str]:
    try:
        with open(os.path.join(case_dir, "case.yaml")) as f:
            manifest = yaml.safe_load(f) or {}
    except OSError:
        return {}
    return {s["id"]: s["file"] for s in manifest.get("sources", [])
            if s.get("id") and s.get("file")}


def _source_pdf(case_dir: str, entry: dict, declared: dict[str, str]) -> str | None:
    """The PDF this CASE serves for the entry, resolved the way the app resolves it:
    the declared case.yaml file inside the case directory, else sources/<doc_id>.pdf,
    else the path the entry recorded — but only inside the case directory. A baked
    entry may record an absolute path from the machine that ingested it; hashing
    THAT file would bind the case to bytes it does not ship."""
    case_real = os.path.realpath(case_dir)
    candidates = []
    if entry["doc_id"] in declared:
        candidates.append(os.path.join(case_dir, declared[entry["doc_id"]]))
    candidates.append(os.path.join(case_dir, "sources", f"{entry['doc_id']}.pdf"))
    rec = entry.get("file") or ""
    candidates.append(rec if os.path.isabs(rec) else os.path.join(case_dir, rec))
    for path in candidates:
        real = os.path.realpath(path)
        if os.path.commonpath([real, case_real]) == case_real and os.path.isfile(real):
            return real
    return None


def _insert_after(entry: dict, anchor: str, key: str, value) -> None:
    """Add `key` to the SAME dict right after `anchor` (ingest_document's key order,
    so a back-filled entry reads like a freshly ingested one); at the end when the
    anchor is missing. Every other key keeps its position and value."""
    items = list(entry.items())
    entry.clear()
    placed = False
    for k, v in items:
        entry[k] = v
        if k == anchor:
            entry[key] = value
            placed = True
    if not placed:
        entry[key] = value


def _write_rules(path: str, data: dict, indent: int, trailing_newline: bool) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
        if trailing_newline:
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _backfill_rules(path: str, hashes: dict[str, str], verbose: bool) -> list[dict]:
    """Stamp citation.doc_sha256 on every rule of one library file. Returns one
    record per rule touched or skipped: {rule_id, doc_id, changed}."""
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        raw = f.read()
    data = json.loads(raw)
    indent = _detect_indent(path)
    report, dirty = [], False
    for rule in data.get("rules", []):
        cit = rule.get("citation") or {}
        doc_id = cit.get("doc_id") or ""
        sha = hashes.get(doc_id)
        if not sha:
            continue
        if cit.get("doc_sha256"):
            report.append({"rule_id": rule.get("id"), "doc_id": doc_id, "changed": False})
            continue
        cit["doc_sha256"] = sha          # appended after the existing citation keys
        rule["citation"] = cit
        dirty = True
        report.append({"rule_id": rule.get("id"), "doc_id": doc_id, "changed": True})
        if verbose:
            print(f"{os.path.basename(path)}: {rule.get('id')} bound to {doc_id} {sha[:12]}")
    if dirty:
        _write_rules(path, data, indent, raw.endswith(b"\n"))
    return report


def backfill(case_dir: str, verbose: bool = True, ledger: bool = False) -> dict:
    """Returns {documents: [{doc_id, pdf_sha256, changed}], rules: [...]}.
    Raises HashMismatch (after touching nothing) when a stored hash disagrees with
    the file on disk."""
    cat_path = os.path.join(case_dir, "catalog.json")
    cat = Catalog(cat_path)
    indent = _detect_indent(cat_path)
    declared = _declared_files(case_dir)
    docs, hashes, dirty = [], {}, False
    for entry in cat.documents():
        doc_id = entry["doc_id"]
        pdf_path = _source_pdf(case_dir, entry, declared)
        if pdf_path is None:
            if verbose:
                print(f"{doc_id}: PDF not found; skipped")
            docs.append({"doc_id": doc_id, "pdf_sha256": entry.get("pdf_sha256", ""),
                         "changed": False})
            continue
        sha = file_sha256(pdf_path) or ""
        stored = entry.get("pdf_sha256") or ""
        if stored and stored != sha:
            raise HashMismatch(f"{doc_id}: catalog records sha {stored[:12]} but "
                               f"{os.path.relpath(pdf_path, case_dir)} on disk is "
                               f"{sha[:12]} — not overwriting; re-ingest or restore the file")
        hashes[doc_id] = sha
        changed = not stored
        if changed:
            _insert_after(entry, "file", "pdf_sha256", sha)   # where ingest writes it
            dirty = True
        docs.append({"doc_id": doc_id, "pdf_sha256": sha, "changed": changed})
        if verbose:
            print(f"{doc_id}: {sha[:12]}" + ("" if changed else " — already recorded"))
    if dirty:
        _save_in_place(cat, indent)
    rules = []
    for name in ("rules_ratified.json", "rules_proposed.json"):
        rules += _backfill_rules(os.path.join(case_dir, "rules", name), hashes, verbose)
    if ledger and any(d["changed"] for d in docs):
        from core.caseio import load_case
        led = load_case(os.path.abspath(case_dir)).ledger()
        for d in docs:
            if d["changed"]:
                led.append("provenance.backfill",
                           {"doc_id": d["doc_id"], "pdf_sha256": d["pdf_sha256"],
                            "basis": BASIS}, actor="system")
    return {"documents": docs, "rules": rules}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--case", required=True, help="case directory holding catalog.json")
    ap.add_argument("--ledger", action="store_true",
                    help="also append provenance.backfill events to the case ledger")
    args = ap.parse_args(argv)
    try:
        backfill(args.case, ledger=args.ledger)
    except HashMismatch as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
