"""Pluggable structured-data source (PRD 5.8).

`DataSource` is the interface the engine reads subjects from. `CSVAdapter` is the
shipping implementation; `DBAdapter`/`APIAdapter` are documented seams so the same
engine can later read from an HRIS/payroll system with no other changes.

The `schema` (declared in case.yaml) types raw fields: a `list` field is split on
commas, a `bool` field parses yes/true/1, a `float` field is cast, etc. This keeps
the CSV human-editable while giving the DSL well-typed facts.
"""
from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from typing import Any

_TRUE = {"yes", "true", "1", "y", "t"}


def _coerce(value: str, typ: str) -> Any:
    v = (value or "").strip()
    if typ == "float":
        return float(v) if v != "" else 0.0
    if typ == "int":
        return int(float(v)) if v != "" else 0
    if typ == "bool":
        return v.lower() in _TRUE
    if typ == "list":
        return [p.strip() for p in v.split(",") if p.strip()]
    return v  # str / unknown


class DataSource(ABC):
    @abstractmethod
    def records(self, schema: dict[str, str]) -> list[dict[str, Any]]:
        """Return subject records, each a dict with fields typed per `schema`."""
        raise NotImplementedError


class CSVAdapter(DataSource):
    def __init__(self, path: str):
        self.path = path

    def records(self, schema: dict[str, str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with open(self.path, newline="") as f:
            for row in csv.DictReader(f):
                rec = {k: _coerce(v, schema.get(k, "str")) for k, v in row.items()}
                out.append(rec)
        return out


class DBAdapter(DataSource):  # pragma: no cover - scale seam, not implemented in MVP
    """Seam for a SQL-backed source. Implement `records()` with a parameterized
    query and the same coercion rules. Deliberately unimplemented at MVP."""

    def __init__(self, dsn: str, query: str):
        self.dsn = dsn
        self.query = query

    def records(self, schema: dict[str, str]) -> list[dict[str, Any]]:
        raise NotImplementedError("DBAdapter is a scale-roadmap seam (PRD §8)")


class APIAdapter(DataSource):  # pragma: no cover - scale seam, not implemented in MVP
    """Seam for an HRIS/payroll REST source."""

    def __init__(self, base_url: str, endpoint: str):
        self.base_url = base_url
        self.endpoint = endpoint

    def records(self, schema: dict[str, str]) -> list[dict[str, Any]]:
        raise NotImplementedError("APIAdapter is a scale-roadmap seam (PRD §8)")


def _case_path(case_dir: str, p: str) -> str:
    import os
    return p if os.path.isabs(p) else os.path.join(case_dir, p)


def load_rate_bindings(cfg: dict, case_dir: str) -> dict[str, dict]:
    """{classification: salary-schedule row} from the data block's `rates` entry (M1.4/I14):

        rates:
          schedule: data/salary_schedule.csv   # one row per title x step, with citation
          bindings: data/schedule_refs.csv     # classification -> schedule_ref

    The schedule row carries doc_id, page, row_bbox, hourly_bbox and the verbatim quote;
    bboxes are parsed to float lists. A case without `rates` returns {} (the rate is then
    looked up by title, or reported unsourced). A binding to a missing ref is dropped here
    and caught by the case's own tests."""
    import os
    rates = (cfg or {}).get("rates") or {}
    if not rates.get("schedule") or not rates.get("bindings"):
        return {}
    sched_path = _case_path(case_dir, rates["schedule"])
    bind_path = _case_path(case_dir, rates["bindings"])
    if not (os.path.exists(sched_path) and os.path.exists(bind_path)):
        return {}
    with open(sched_path, newline="") as f:
        by_ref = {}
        for row in csv.DictReader(f):
            for k in ("row_bbox", "hourly_bbox"):
                row[k] = [float(v) for v in (row.get(k) or "").split(",") if v.strip()]
            by_ref[row["schedule_ref"]] = row
    out: dict[str, dict] = {}
    with open(bind_path, newline="") as f:
        for b in csv.DictReader(f):
            row = by_ref.get((b.get("schedule_ref") or "").strip())
            if row is not None:
                out[(b.get("classification") or "").strip()] = row
    return out


def make_adapter(cfg: dict, case_dir: str) -> DataSource:
    import os
    kind = (cfg or {}).get("adapter", "csv")
    if kind == "csv":
        path = cfg["path"]
        if not os.path.isabs(path):
            path = os.path.join(case_dir, path)
        return CSVAdapter(path)
    if kind == "db":
        return DBAdapter(cfg.get("dsn", ""), cfg.get("query", ""))
    if kind == "api":
        return APIAdapter(cfg.get("base_url", ""), cfg.get("endpoint", ""))
    raise ValueError(f"unknown data adapter {kind!r}")
