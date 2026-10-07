"""Decimal helpers. All price math is Decimal so that scenario totals can be
matched to the penny against spreadsheet results (Game Plan section 7, step 5)."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, getcontext

getcontext().prec = 40

CENT = Decimal("0.01")


def D(x) -> Decimal:
    """Coerce to Decimal without binary-float noise."""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(repr(x))
    return Decimal(str(x).replace(",", "").replace("$", "").strip())


def round_cents(x: Decimal) -> Decimal:
    """Excel ROUND(x, 2): half away from zero."""
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def trunc_cents(x: Decimal) -> Decimal:
    """Round toward zero at two decimals (how RCC's 10-month hours were typed)."""
    return D(x).quantize(CENT, rounding=ROUND_DOWN)
