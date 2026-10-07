"""Recover the inputs that exist in no file from a *prior submitted bid*.

Deck slide 10: "Overhead, G&A and fee appear in no file... visible only baked into the
combined indirects column of a finished workbook." This module reads that column (and
the exempt-rate card) from a submitted Vol II and turns it into explicit, labelled
levers: employer burden per site/state, an exempt adder and exempt escalation.

Everything produced here is *reverse-engineered*: confidence Medium/Low, and it is
written to the assumptions register as such. The questions for Patriot (what is in the
overhead block, what is the adder) are raised by ``questions.py``.
"""

from __future__ import annotations

import difflib
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from .build_case import norm
from .pricing.model import EXEMPT, SCA, Case, Line, Scenario, markup, sca_breakout
from .pricing.money import D, round_cents


@dataclass
class Calibration:
    site_burden: Dict[str, Decimal] = field(default_factory=dict)
    state_burden: Dict[str, Decimal] = field(default_factory=dict)
    exempt_adder: Decimal = Decimal(0)
    esc_oy1: Decimal = Decimal(0)
    esc_oy2: Decimal = Decimal(0)
    implied_salaries: Dict[str, Decimal] = field(default_factory=dict)
    indirect_dollars: Dict[str, Decimal] = field(default_factory=dict)
    observed_rates: Dict[Tuple[str, str], Decimal] = field(default_factory=dict)
    residuals: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


def _same(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.92


def _sca_row_for(line: Line, scls_rows: List[dict], used: set):
    want_tcc = line.role == "tcc"
    for i, r in enumerate(scls_rows):
        if i in used:
            continue
        is_tcc = "tcc" in str(r["category"]).lower() or "transition" in str(r["category"]).lower()
        if is_tcc != want_tcc:
            continue
        if norm(str(r["place"])) == norm(line.place):
            used.add(i)
            return r
    return None


def calibrate_from_matrix(case: Case, matrix: dict, base: Scenario) -> Calibration:
    cal = Calibration()
    M = markup(base)

    # --- SCA: implied employer burden per line ---------------------------------
    scls = [r for r in matrix["scls"] if str(r["category"]).strip() and "Administrative" not in str(r["category"])]
    used: set = set()
    by_state: Dict[str, List[Decimal]] = {}
    for line in case.lines:
        if line.classification != SCA:
            continue
        row = _sca_row_for(line, scls, used)
        if row is None:
            cal.notes.append(f"no SCLS breakout row for {line.key}")
            continue
        elems = sca_breakout(line, case, base, indirect_override=Decimal(0))
        base_cost = elems["wages"] + elems["vacation"] + elems["sick"] + elems["holiday"] + elems["hw"]
        ind = D(row["indirects"])
        burden = (1 + ind / base_cost) / M - 1
        cal.site_burden[line.key] = burden
        cal.indirect_dollars[line.key] = ind
        if line.state:
            by_state.setdefault(line.state, []).append(burden)
    for st, vals in by_state.items():
        cal.state_burden[st] = D(statistics.median([float(v) for v in vals]))

    # --- Exempt: observed rate cards -------------------------------------------
    labor = matrix["labor"]
    optional = matrix["optional"]
    obs: Dict[str, List[Optional[Decimal]]] = {}
    used_rows: Dict[str, set] = {p: set() for p in labor}
    for line in case.lines:
        if line.classification != EXEMPT:
            continue
        rates = []
        for p in ("base", "oy1", "oy2"):
            r = None
            if line.task == "labor":
                rows = labor[p]["rows"]
                for x in rows:
                    if x["row"] in used_rows[p]:
                        continue
                    same_cat = _same(x["category"], line.labor_category)
                    same_pws = norm(str(x.get("pws") or "")) == norm(line.pws_ref)
                    same_place = norm(str(x["place"])) == norm(line.place) if line.state == "OCONUS" else True
                    if same_cat and same_place and (same_pws or line.state == "OCONUS") and not x["scls"]:
                        r = x
                        used_rows[p].add(x["row"])
                        break
            else:
                tab = "opt1" if line.task == "opt1" else "opt2"
                for x in optional[tab][p]["rows"]:
                    if _same(x["category"], line.labor_category) and not x["scls"]:
                        key = (tab, p, x["row"])
                        if key in used_rows.setdefault("_opt", set()):
                            continue
                        used_rows["_opt"].add(key)
                        r = x
                        break
            rates.append(r["rate"] if r else None)
        obs[line.key] = rates
        for p, rt in zip(("base", "oy1", "oy2"), rates):
            if rt is not None:
                cal.observed_rates[(line.key, p)] = rt

    known = [(l, obs[l.key]) for l in case.lines if l.classification == EXEMPT and l.salary > 0 and l.key in obs and all(obs[l.key])]
    if known:
        best = None
        fr = base.dec("rate.fringe")
        div = base.dec("hours.exempt_divisor", 2080)
        for A in range(5000, 5601, 1):
            for e1 in [Decimal(i) / Decimal(10000) for i in range(130, 156)]:
                for e2 in [Decimal(i) / Decimal(10000) for i in range(130, 156)]:
                    ok, err = 0, Decimal(0)
                    for l, rs in known:
                        s0 = l.salary
                        for sal, obs_r in zip((s0, s0 * (1 + e1), s0 * (1 + e1) * (1 + e2)), rs):
                            v = (sal * (1 + fr) * M + A) / div
                            ok += round_cents(v) == obs_r
                            err += abs(v - obs_r)
                    key = (ok, -err)
                    if best is None or key > best[0]:
                        best = (key, Decimal(A), e1, e2)
        (ok, _), cal.exempt_adder, cal.esc_oy1, cal.esc_oy2 = best
        cal.residuals["exempt_rate_matches"] = f"{ok}/{3 * len(known)} observed exempt rates reproduced to the cent"
        # worst absolute error
        worst = Decimal(0)
        for l, rs in known:
            s0 = l.salary
            for sal, obs_r in zip((s0, s0 * (1 + cal.esc_oy1), s0 * (1 + cal.esc_oy1) * (1 + cal.esc_oy2)), rs):
                v = (sal * (1 + fr) * M + cal.exempt_adder) / div
                worst = max(worst, abs(round_cents(v) - obs_r))
        cal.residuals["exempt_rate_max_error"] = f"${worst}"

        # implied salaries for exempt lines the salary workbook does not carry
        for l in case.lines:
            if l.classification == EXEMPT and l.salary == 0 and l.key in obs and obs[l.key][0]:
                sal = (obs[l.key][0] * div - cal.exempt_adder) / ((1 + fr) * M)
                cal.implied_salaries[l.role or l.key] = round_cents(sal)
                cal.notes.append(
                    f"{l.key}: salary not in any file; implied ${round_cents(sal)} from the submitted base rate (Low confidence)"
                )
    return cal
