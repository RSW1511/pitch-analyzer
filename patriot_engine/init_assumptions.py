"""Write the first assumptions.csv for a case.

The rows are the proposal an AI agent (or a person) would draft; they are not approved
until the pricing manager runs ``approve``. Every row carries a source and a confidence so
the register doubles as the defence if the government questions a number.

Lever defaults live in the case's ``levers.yaml`` (company-specific values stay out of the
code). Calibrated values from a prior bid are substituted for ``@cal.<name>|<default>``
tokens. Salary-by-role and employer-burden-by-state rows are generated from the case.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .assumptions import write_rows
from .calibrate import Calibration
from .pricing.model import Case, Scenario

SCEN = ["Base", "Aggressive", "Conservative"]
TOKEN = re.compile(r"^@cal\.(\w+)\|(.*)$")


def load_levers(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _resolve(v, cal: Optional[Calibration]) -> str:
    s = str(v)
    m = TOKEN.match(s)
    if not m:
        return s
    name, default = m.groups()
    if cal is not None and hasattr(cal, name):
        val = getattr(cal, name)
        if isinstance(val, Decimal) and val != 0:
            return str(val)
    return default


def _vals(v) -> List:
    return list(v) if isinstance(v, (list, tuple)) else [v] * len(SCEN)


def _row(key, desc, zone, unit, vals, source, phase, conf, status="Assumed", rationale=""):
    r = {"key": key, "description": desc, "zone": zone, "unit": unit, "source": source, "file_hash": "", "phase": str(phase), "confidence": conf, "status": status, "rationale": rationale}
    for s, v in zip(SCEN, vals):
        r[s] = str(v)
    return r


def base_scenario(spec: dict, cal: Optional[Calibration] = None) -> Scenario:
    """The Base column of a levers file as a Scenario (used to calibrate against a prior bid)."""
    vals: Dict[str, str] = {}
    for g in spec.get("gov", []):
        vals[g["key"]] = _resolve(g["value"], cal)
    for l in spec.get("levers", []):
        vals[l["key"]] = _resolve(_vals(l["values"])[0], cal)
    return Scenario("Base", vals)


def build_rows(case: Case, spec: dict, cal: Optional[Calibration] = None, phase: int = 2) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    base = sum((l.fte for l in case.lines if l.task == "labor"), Decimal(0))
    opt = sum((l.fte for l in case.lines if l.task != "labor"), Decimal(0))
    rows.append(_row("fte.base_total", "Base FTE required by the staffing table", "gov", "FTE", _vals(str(base)), "Technical Exhibit 4 (column totals)", phase, "High"))
    rows.append(_row("fte.optional_total", "Optional-task FTE in the staffing table", "gov", "FTE", _vals(str(opt)), "Technical Exhibit 4 (optional-task columns)", phase, "High"))
    for g in spec.get("gov", []):
        rows.append(_row(g["key"], g["description"], "gov", g.get("unit", ""), _vals(_resolve(g["value"], cal)), g["source"], phase, g.get("confidence", "Medium"), g.get("status", "Assumed"), g.get("rationale", "")))
    for l in spec.get("levers", []):
        rows.append(_row(l["key"], l["description"], "lever", l.get("unit", ""), [_resolve(v, cal) for v in _vals(l["values"])], l["source"], phase, l.get("confidence", "Medium"), l.get("status", "Assumed"), l.get("rationale", "")))

    seen = set()
    for line in case.lines:
        if line.classification != "EXEMPT" or not line.role or line.role in seen:
            continue
        seen.add(line.role)
        sal, conf, src = line.salary, "Medium", "Bid salaries workbook"
        if cal and line.role in cal.implied_salaries:
            sal, conf, src = cal.implied_salaries[line.role], "Low", "Implied from a prior bid's base rate; no salary in any file"
        rows.append(_row(f"salary.{line.role}", f"Salary: {line.labor_category} ({line.role})", "lever", "usd", _vals(str(sal)), src, phase, conf))
    if cal:
        for st, b in sorted(cal.state_burden.items()):
            rows.append(_row(f"burden.state.{st}", f"Employer burden before OH/G&A/fee, SCA lines in {st}", "lever", "pct", _vals(f"{b:.5f}"), "Reverse-engineered from a prior bid's 'All Other Indirects and Profit' column", phase, "Low", rationale="Median across sites in the state; replace with real payroll-tax/insurance rates"))
    return rows


def write_assumptions(path: str | Path, case: Case, spec: dict, cal: Optional[Calibration] = None, phase: int = 2) -> List[Dict[str, str]]:
    rows = build_rows(case, spec, cal, phase)
    write_rows(path, SCEN, rows)
    return rows
