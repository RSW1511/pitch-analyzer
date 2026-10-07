"""M4 assumptions register.

``assumptions.csv`` is the only file the AI agent and the Python engine share
(Game Plan section 7). One row per number. Scenarios are extra columns. The
pricing manager approves the file; approval is recorded against its SHA-256 so
any later edit invalidates it. Nothing is priced for export before sign-off.
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .pricing.model import Scenario

META_COLS = ["key", "description", "zone", "unit", "source", "file_hash", "phase", "confidence", "status", "rationale"]
CONFIDENCE = ("High", "Medium", "Low")
STATUS = ("Assumed", "Confirmed", "Changed by Q&A", "Conflict flagged")
ZONES = ("gov", "lever")
DEFAULT_SCENARIOS = ("Base", "Aggressive", "Conservative")


class AssumptionError(ValueError):
    pass


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_rows(path: str | Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        rows = [dict(r) for r in rd]
        return list(rd.fieldnames or []), rows


def scenario_names(fieldnames: Iterable[str]) -> List[str]:
    return [c for c in fieldnames if c not in META_COLS]


def validate(path: str | Path) -> List[str]:
    """Return a list of problems (empty = valid). Mirrors Game Plan section 10."""
    problems: List[str] = []
    fields, rows = read_rows(path)
    scen = scenario_names(fields)
    if not scen:
        problems.append("no scenario columns")
    seen = set()
    for r in rows:
        k = r.get("key", "")
        if not k:
            problems.append("row with empty key")
            continue
        if k in seen:
            problems.append(f"{k}: duplicate key")
        seen.add(k)
        if r.get("zone") not in ZONES:
            problems.append(f"{k}: zone must be one of {ZONES}")
        if r.get("confidence") not in CONFIDENCE:
            problems.append(f"{k}: confidence must be one of {CONFIDENCE}")
        if r.get("status") not in STATUS:
            problems.append(f"{k}: status must be one of {STATUS}")
        if not r.get("source"):
            problems.append(f"{k}: every number needs a source (document + section)")
        vals = {s: r.get(s, "") for s in scen}
        if any(v == "" for v in vals.values()):
            problems.append(f"{k}: blank scenario value")
        if r.get("zone") == "gov" and len(set(vals.values())) > 1:
            problems.append(f"{k}: government-fixed item differs between scenarios {vals}")
    return problems


def load_scenarios(path: str | Path) -> Dict[str, Scenario]:
    probs = validate(path)
    if probs:
        raise AssumptionError("assumptions.csv is invalid:\n  " + "\n  ".join(probs))
    fields, rows = read_rows(path)
    out = {}
    for s in scenario_names(fields):
        out[s] = Scenario(s, {r["key"]: r[s] for r in rows})
    return out


def write_rows(path: str | Path, scenarios: List[str], rows: List[Dict[str, str]]) -> None:
    cols = META_COLS[:4] + scenarios + META_COLS[4:]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


# ---------------------------------------------------------------------------
# Approval gate
# ---------------------------------------------------------------------------

def _log_path(csv_path: str | Path) -> Path:
    return Path(csv_path).with_name("approvals.jsonl")


def approve(csv_path: str | Path, approver: str, phase: str, note: str = "") -> dict:
    probs = validate(csv_path)
    if probs:
        raise AssumptionError("cannot approve an invalid file:\n  " + "\n  ".join(probs))
    entry = {
        "file": Path(csv_path).name,
        "sha256": sha256_file(csv_path),
        "approver": approver,
        "phase": phase,
        "note": note,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with open(_log_path(csv_path), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def approval_for(csv_path: str | Path) -> Optional[dict]:
    """The latest approval whose hash still matches the file, else None."""
    log = _log_path(csv_path)
    if not log.exists():
        return None
    digest = sha256_file(csv_path)
    hit = None
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if e["sha256"] == digest:
            hit = e
    return hit


def version_label(csv_path: str | Path) -> str:
    """Short, stable label that goes into export filenames and the run log."""
    return sha256_file(csv_path)[:8]


# ---------------------------------------------------------------------------
# Phase change list (the defence if the government questions the price)
# ---------------------------------------------------------------------------

def diff(old_csv: str | Path, new_csv: str | Path) -> List[Dict[str, str]]:
    _, a = read_rows(old_csv)
    f2, b = read_rows(new_csv)
    scen = scenario_names(f2)
    A = {r["key"]: r for r in a}
    out = []
    for r in b:
        o = A.get(r["key"])
        if o is None:
            out.append({"key": r["key"], "change": "added", "old": "", "new": "|".join(r.get(s, "") for s in scen), "why": r.get("rationale", "")})
            continue
        changed = [s for s in scen if o.get(s, "") != r.get(s, "")]
        if changed or o.get("status") != r.get("status"):
            out.append(
                {
                    "key": r["key"],
                    "change": "changed " + ",".join(changed) if changed else "status",
                    "old": "|".join(o.get(s, "") for s in scen),
                    "new": "|".join(r.get(s, "") for s in scen),
                    "why": r.get("rationale", "") or r.get("source", ""),
                }
            )
    new_keys = {r["key"] for r in b}
    for k, o in A.items():
        if k not in new_keys:
            out.append({"key": k, "change": "removed", "old": "|".join(o.get(s, "") for s in scen), "new": "", "why": ""})
    return out
