"""Scenario runner, Price-to-Win reverse solve and sensitivity (Game Plan section 10).

All targets are on the Total Evaluated Price basis, not the matrix total (Rule 12):
the last option year counts about 1.5x because the government adds a six-month
extension at its rates.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import product
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .model import Case, PriceResult, Scenario, price_case
from .money import D, round_cents

# lever -> (low, high, unit step used for sensitivity)
LEVER_BOUNDS: Dict[str, Tuple[Decimal, Decimal, Decimal]] = {
    "rate.fee": (D("0"), D("0.08"), D("0.01")),
    "rate.overhead": (D("0"), D("0.06"), D("0.01")),
    "rate.ga": (D("0"), D("0.06"), D("0.01")),
    "rate.fringe": (D("0.20"), D("0.40"), D("0.01")),
    "esc.exempt.oy1": (D("0"), D("0.05"), D("0.005")),
    "esc.exempt.oy2": (D("0"), D("0.05"), D("0.005")),
    "exempt.salary_steps": (D("-3"), D("3"), D("1")),
    "exempt.adder": (D("0"), D("10000"), D("1000")),
}


def run_all(case: Case, scenarios: Mapping[str, Scenario], **kw) -> Dict[str, PriceResult]:
    return {name: price_case(case, s, **kw) for name, s in scenarios.items()}


def tep_of(case: Case, s: Scenario, **kw) -> Decimal:
    return price_case(case, s, **kw).tep


def sensitivity(
    case: Case,
    s: Scenario,
    levers: Optional[Sequence[str]] = None,
    extra: Optional[Dict[str, Decimal]] = None,
) -> List[Dict[str, object]]:
    """Change in TEP and matrix total for one standard step up and down on each lever.

    ``extra`` adds FTE levers: {"fte.rtl": 1} means +/-1 FTE.
    """
    base = price_case(case, s)
    steps: Dict[str, Decimal] = {}
    for k in levers or LEVER_BOUNDS:
        if k in LEVER_BOUNDS:
            steps[k] = LEVER_BOUNDS[k][2]
    for k, v in (extra or {}).items():
        steps[k] = D(v)
    rows = []
    for k, step in steps.items():
        if s.get(k) not in (None, ""):
            cur = s.dec(k)
        elif k.startswith("fte."):
            role = k.split(".", 1)[1]
            cur = sum((l.fte for l in case.lines if l.role == role), Decimal(0))
        else:
            cur = Decimal(0)
        up = price_case(case, s.set(k, str(cur + step)))
        dn = price_case(case, s.set(k, str(cur - step)))
        rows.append(
            {
                "lever": k,
                "base_value": str(cur),
                "step": str(step),
                "tep_up": round_cents(up.tep - base.tep),
                "tep_down": round_cents(dn.tep - base.tep),
                "matrix_up": round_cents(up.matrix_total - base.matrix_total),
                "swing": abs(round_cents(up.tep - dn.tep)),
            }
        )
    rows.sort(key=lambda r: r["swing"], reverse=True)
    return rows


def solve_lever(
    case: Case,
    s: Scenario,
    key: str,
    target_tep: Decimal,
    lo: Optional[Decimal] = None,
    hi: Optional[Decimal] = None,
    resolution: Decimal = D("0.000001"),
) -> Optional[Decimal]:
    """Largest value of one lever that keeps TEP at or under the target, or None if even
    the lever's lowest value is over it.

    TEP is non-decreasing in every lever in LEVER_BOUNDS but is a step function (rates
    round to the cent), so the answer is the boundary, not an exact root: the returned
    value is under the target and a value ``resolution`` higher is not.
    """
    b = LEVER_BOUNDS.get(key)
    lo = lo if lo is not None else (b[0] if b else D(0))
    hi = hi if hi is not None else (b[1] if b else D(1))
    ok = lambda v: price_case(case, s.set(key, str(v))).tep <= target_tep
    if not ok(lo):
        return None
    if ok(hi):
        return hi
    while hi - lo > resolution:
        mid = (lo + hi) / 2
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


def feasible_region(
    case: Case,
    s: Scenario,
    grid: Mapping[str, Sequence[Decimal]],
    target_tep: Decimal,
) -> List[Dict[str, object]]:
    """All lever combinations on ``grid`` whose TEP is at or under the target.

    Ranked by retained margin (fee first, then fewest concessions). This is the
    "at 69 FTE, fee <= 1.8% and overhead <= 1.1%" answer from Game Plan section 10.
    """
    keys = list(grid)
    out = []
    for combo in product(*(grid[k] for k in keys)):
        s2 = s
        for k, v in zip(keys, combo):
            s2 = s2.set(k, str(v))
        r = price_case(case, s2)
        if r.tep <= target_tep:
            out.append(
                {
                    **{k: v for k, v in zip(keys, combo)},
                    "tep": round_cents(r.tep),
                    "headroom": round_cents(target_tep - r.tep),
                }
            )
    fee_key = "rate.fee"
    out.sort(
        key=lambda r: (
            -(r.get(fee_key, D(0))),
            -sum(r.get(k, D(0)) for k in keys if k != fee_key),
        )
    )
    return out


def price_to_win_report(case: Case, s: Scenario, target_tep: Decimal) -> Dict[str, object]:
    """Single-lever solves plus the position of each scenario against the target."""
    base = price_case(case, s)
    solves = {}
    for k in ("rate.fee", "rate.overhead", "rate.ga", "rate.fringe", "esc.exempt.oy1", "esc.exempt.oy2"):
        v = solve_lever(case, s, k, target_tep)
        solves[k] = None if v is None else str(v.quantize(D("0.0001")))
    return {
        "target_tep": round_cents(target_tep),
        "scenario_tep": round_cents(base.tep),
        "gap": round_cents(base.tep - target_tep),
        "single_lever_solves": solves,
    }
