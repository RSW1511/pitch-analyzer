"""Delta re-pricing (Game Plan phases 3-4): after an amendment, show what moved in price.

Prices the old and new case under the same scenario and reports per-line, per-site
dollar movement on both the matrix and Total Evaluated Price basis. The answer to
"An amendment moved one coordinator between two bases: same total headcount, different
money" comes out of ``line_deltas`` with no re-building.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Dict, List

from .pricing.model import Case, PriceResult, Scenario, price_case
from .pricing.money import round_cents


def line_deltas(old: PriceResult, new: PriceResult) -> List[Dict[str, object]]:
    def _agg(r: PriceResult):
        d = {}
        for lp in r.lines:
            e = d.setdefault(lp.line.key, {"ext": Decimal(0), "place": lp.line.place, "cat": lp.line.labor_category, "task": lp.line.task})
            e["ext"] += lp.extended
        return d

    a, b = _agg(old), _agg(new)
    out = []
    for k in sorted(set(a) | set(b)):
        ea, eb = a.get(k), b.get(k)
        before = ea["ext"] if ea else Decimal(0)
        after = eb["ext"] if eb else Decimal(0)
        if before == after:
            continue
        ref = eb or ea
        out.append(
            {
                "line": k,
                "place": ref["place"],
                "category": ref["cat"],
                "task": ref["task"],
                "change": "added" if ea is None else "removed" if eb is None else "re-priced",
                "matrix_delta": round_cents(after - before),
            }
        )
    out.sort(key=lambda r: abs(r["matrix_delta"]), reverse=True)
    return out


def delta_summary(old_case: Case, new_case: Case, s: Scenario) -> Dict[str, object]:
    ro, rn = price_case(old_case, s), price_case(new_case, s)
    return {
        "scenario": s.name,
        "fte_old": f"{ro.fte_base}+{ro.fte_optional}",
        "fte_new": f"{rn.fte_base}+{rn.fte_optional}",
        "matrix_old": round_cents(ro.matrix_total),
        "matrix_new": round_cents(rn.matrix_total),
        "matrix_delta": round_cents(rn.matrix_total - ro.matrix_total),
        "tep_delta": round_cents(rn.tep - ro.tep),
        "lines": line_deltas(ro, rn),
    }
