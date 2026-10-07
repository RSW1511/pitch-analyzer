"""Step 1 incumbent baseline and first Price-to-Win range (Game Plan 3.4).

Plain formula: latest incumbent labour per FTE-year (annualised, after wage
adjustments) x expected FTE (adjusted for any RFI signal) x wage-floor change since
then, plus the government's fixed travel, then convert to the Total Evaluated Price
basis. Give a range, not a point.

Lesson built in: a 25% headcount cut is not a 25% price cut, because the cut removes
mostly lower-cost roles. ``role_level_estimate`` prices by role; ``desk_estimate`` is
the quick total-level version and reports how far it could be off.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Tuple

from ..pricing.money import D, round_cents


@dataclass
class DeskEstimate:
    labor_per_fte: Decimal
    fte_expected: Decimal
    labor_low: Decimal
    labor_point: Decimal
    labor_high: Decimal
    notes: List[str]

    def to_dict(self):
        return {k: (str(round_cents(v)) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


def desk_estimate(
    latest_labor: Decimal,
    latest_fte: Decimal,
    expected_fte: Decimal,
    wage_change: Decimal = Decimal(0),
    mix_uplift_low: Decimal = Decimal(0),
    mix_uplift_high: Decimal = Decimal("0.10"),
) -> DeskEstimate:
    """Total-level estimate. ``mix_uplift`` widens the range upward because cuts usually
    remove cheaper roles (RCC: +8% cost per FTE after a 25% cut)."""
    per = D(latest_labor) / D(latest_fte)
    base = per * D(expected_fte) * (1 + D(wage_change))
    return DeskEstimate(
        labor_per_fte=per,
        fte_expected=D(expected_fte),
        labor_low=base * (1 + D(mix_uplift_low)),
        labor_point=base,
        labor_high=base * (1 + D(mix_uplift_high)),
        notes=[
            "total-level estimate: assumes the removed roles cost the average",
            f"range widened up to +{D(mix_uplift_high) * 100:.0f}% for role-mix effect",
        ],
    )


def role_level_estimate(
    old_roles: Mapping[str, Tuple[Decimal, Decimal]],
    new_fte: Mapping[str, Decimal],
    wage_change: Decimal = Decimal(0),
) -> Dict[str, object]:
    """old_roles: role -> (fte, annual cost per fte). new_fte: role -> expected fte."""
    total_old = sum((D(f) * D(c) for f, c in old_roles.values()), Decimal(0))
    fte_old = sum((D(f) for f, _ in old_roles.values()), Decimal(0))
    total_new = Decimal(0)
    rows = []
    for role, (f, c) in old_roles.items():
        n = D(new_fte.get(role, f))
        v = n * D(c) * (1 + D(wage_change))
        total_new += v
        rows.append({"role": role, "fte_old": str(f), "fte_new": str(n), "cost_per_fte": str(round_cents(D(c))), "new_cost": str(round_cents(v))})
    fte_new = sum((D(new_fte.get(r, f)) for r, (f, _) in old_roles.items()), Decimal(0))
    avg_old = total_old / fte_old if fte_old else Decimal(0)
    avg_new = total_new / fte_new if fte_new else Decimal(0)
    return {
        "rows": rows,
        "total_new": total_new,
        "avg_cost_old": avg_old,
        "avg_cost_new": avg_new,
        "mix_effect_pct": (avg_new / (avg_old * (1 + D(wage_change))) - 1) * 100 if avg_old else Decimal(0),
    }


def to_tep(labor_by_period: Mapping[str, Decimal], odc_per_period: Decimal, periods: List[str], extension_months: int = 6, fixed_other: Decimal = Decimal(0)) -> Dict[str, Decimal]:
    """Convert annual labour estimates into the TEP basis (adds the extension at the last period's rates)."""
    matrix = sum((D(labor_by_period[p]) for p in periods), Decimal(0)) + D(odc_per_period) * len(periods) + D(fixed_other)
    ext = D(labor_by_period[periods[-1]]) * D(extension_months) / D(12)
    return {"matrix": matrix, "extension": ext, "tep": matrix + ext}


def price_range(
    latest_labor: Decimal,
    latest_fte: Decimal,
    expected_fte_low: Decimal,
    expected_fte_high: Decimal,
    wage_change_low: Decimal,
    wage_change_high: Decimal,
    odc_per_period: Decimal,
    periods: Optional[List[Tuple[str, int]]] = None,
    escalation: Decimal = Decimal("0.014"),
    extension_months: int = 6,
) -> Dict[str, object]:
    """Low/high first Price-to-Win range on the TEP basis, from Step 1 documents only."""
    periods = periods or [("base", 10), ("oy1", 12), ("oy2", 12)]
    out = {}
    for label, fte, wage in (("low", expected_fte_low, wage_change_low), ("high", expected_fte_high, wage_change_high)):
        est = desk_estimate(latest_labor, latest_fte, fte, wage, mix_uplift_high=Decimal(0))
        annual = est.labor_point
        by_period = {}
        for i, (p, months) in enumerate(periods):
            by_period[p] = annual * (1 + D(escalation)) ** i * D(months) / D(12)
        # the last period is a full 12 months for the extension basis
        last_p, _ = periods[-1]
        full_last = annual * (1 + D(escalation)) ** (len(periods) - 1)
        t = to_tep(by_period, odc_per_period, [p for p, _ in periods], extension_months)
        t["extension"] = full_last * D(extension_months) / D(12)
        t["tep"] = t["matrix"] + t["extension"]
        out[label] = {k: str(round_cents(v)) for k, v in t.items()}
        out[label]["labor_per_year"] = str(round_cents(annual))
    return out
