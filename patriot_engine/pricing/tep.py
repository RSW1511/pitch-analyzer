"""Total Evaluated Price (Game Plan rule 12).

TOEP section V.2.1: TEP = FFP CLINs (incl. optional tasks) + government ODC CR
CLINs for base and option periods + the amount for the FAR 52.217-8 extension,
which "shall utilize the rates for Option Period 2". The extension is therefore
extension_months/12 x the last option period's FFP services. Whether the
government also adds ODCs for the extension is not stated, so it is a case flag
(default: no) and the difference is reported by ``extension_sensitivity``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict

from .money import D


def extension_value(case, ffp, odc, odc_opt) -> Decimal:
    if not case.extension_months:
        return Decimal(0)
    last = case.periods[-1].key
    services = sum(ffp[last].values(), Decimal(0))
    if case.extension_includes_odc:
        services += odc[last] + odc_opt[last]
    return services * D(case.extension_months) / D(12)


def total_evaluated_price(result) -> Decimal:
    return result.tep


def extension_sensitivity(case, result) -> Dict[str, Decimal]:
    """TEP under both readings of the extension, so the ambiguity is visible."""
    last = case.periods[-1].key
    svc = sum(result.ffp[last].values(), Decimal(0)) * D(case.extension_months) / D(12)
    odc = (result.odc[last] + result.odc_optional[last]) * D(case.extension_months) / D(12)
    base = result.matrix_total
    return {"services_only": base + svc, "services_and_odc": base + svc + odc}
