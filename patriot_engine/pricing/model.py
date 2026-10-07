"""M5 pricing core.

Pure functions: (case, scenario) -> PriceResult. Same inputs always give the same
price. No I/O, no randomness, no AI.

Rate mechanics are taken from the RCC Vol II and Game Plan v2 section 9:

* SCA lines: rate = ROUND((wages + paid leave + H&W + indirects/profit) / productive hours, 2)
  where wages are on productive hours, leave is vacation/sick/holiday hours x wage,
  H&W is the WD rate x 2,080, and indirects are a site-specific load. Held flat
  across years (WD changes are recovered by contract adjustment, not escalation).
* Exempt lines: rate = salary x (1+fringe) x (1+OH) x (1+G&A) x (1+fee) / 2,080
  plus a per-FTE adder, escalated per year, but billed on 1,912 hours.
* Hours for the 10-month base period are per-FTE hours truncated to cents, then
  multiplied by FTE (how RCC's workbook carries 1,546.66 and 1,593.33).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from .money import D, round_cents, trunc_cents

SCA = "SCA"
EXEMPT = "EXEMPT"

TASK_LABOR = "labor"
TASK_OPT1 = "opt1"
TASK_OPT2 = "opt2"
TASKS = (TASK_LABOR, TASK_OPT1, TASK_OPT2)


@dataclass(frozen=True)
class Period:
    key: str  # "base", "oy1", "oy2"
    months: int  # full-performance months priced in this period
    label: str = ""


@dataclass
class Line:
    """One priced labor line (category x place x task)."""

    key: str
    labor_category: str
    place: str
    task: str
    classification: str
    fte: Decimal
    pws_ref: str = ""
    lcat_id: str = ""
    role: str = ""  # lever group, e.g. "pm", "rtl", "support", "rcc", "tcc"
    # SCA
    wd_number: str = ""
    wd_rev: str = ""
    occupation: str = ""
    occ_code: str = ""
    wage: Decimal = Decimal(0)
    hw: Decimal = Decimal(0)
    holiday_hours: Decimal = Decimal(88)
    state: str = ""
    # Exempt
    salary: Decimal = Decimal(0)
    # Which periods this line is priced in (optional tasks differ)
    periods: Tuple[str, ...] = ("base", "oy1", "oy2")
    notes: str = ""


@dataclass
class Case:
    """Everything fixed by the solicitation (government zone) for one bid."""

    name: str
    periods: List[Period]
    lines: List[Line]
    odc: Dict[str, Decimal]  # period key -> government ODC/travel (CR, no fee)
    odc_optional: Dict[str, Decimal]  # period key -> optional-task ODC
    extension_months: int = 6
    extension_includes_odc: bool = False
    site_burden: Dict[str, Decimal] = field(default_factory=dict)  # line key -> employer burden
    state_burden: Dict[str, Decimal] = field(default_factory=dict)  # state -> burden fallback
    site_aliases: Dict[str, str] = field(default_factory=dict)  # normalised TE name -> wage-table name


class Scenario:
    """A complete set of lever values (one column of assumptions.csv)."""

    def __init__(self, name: str, values: Mapping[str, object]):
        self.name = name
        self._v: Dict[str, object] = dict(values)

    def get(self, key: str, default=None):
        return self._v.get(key, default)

    def dec(self, key: str, default=None) -> Decimal:
        if key in self._v and self._v[key] not in ("", None):
            return D(self._v[key])
        if default is None:
            raise KeyError(f"scenario '{self.name}' is missing lever '{key}'")
        return D(default)

    def with_(self, **overrides) -> "Scenario":
        v = dict(self._v)
        for k, val in overrides.items():
            v[k.replace("__", ".")] = val
        return Scenario(self.name, v)

    def set(self, key: str, value) -> "Scenario":
        v = dict(self._v)
        v[key] = value
        return Scenario(self.name, v)

    def items(self):
        return self._v.items()


@dataclass
class LinePrice:
    line: Line
    period: str
    hours: Decimal
    rate: Decimal
    extended: Decimal  # unrounded hours x rate, as Excel carries it
    indirects: Decimal = Decimal(0)  # SCA only, dollars per FTE-year


@dataclass
class PriceResult:
    scenario: str
    lines: List[LinePrice]
    # period -> task -> FFP total
    ffp: Dict[str, Dict[str, Decimal]]
    odc: Dict[str, Decimal]
    odc_optional: Dict[str, Decimal]
    period_total: Dict[str, Decimal]
    matrix_total: Decimal
    tep: Decimal
    extension: Decimal
    fte_base: Decimal
    fte_optional: Decimal
    hours_total: Decimal

    def clin(self, period: str, task: str) -> Decimal:
        return round_cents(self.ffp[period][task])

    def summary(self) -> Dict[str, str]:
        return {
            "scenario": self.scenario,
            "matrix_total": str(round_cents(self.matrix_total)),
            "tep": str(round_cents(self.tep)),
            "fte_base": str(self.fte_base),
            "fte_optional": str(self.fte_optional),
        }


# ---------------------------------------------------------------------------
# Rate builders
# ---------------------------------------------------------------------------

def markup(s: Scenario) -> Decimal:
    """(1+OH)(1+G&A)(1+fee): the layered cascade (Game Plan section 9)."""
    return (1 + s.dec("rate.overhead")) * (1 + s.dec("rate.ga")) * (1 + s.dec("rate.fee"))


def sca_breakout(line: Line, case: Case, s: Scenario, indirect_override: Optional[Decimal] = None):
    """Return the SCLS Breakout elements for one SCA line (dollars per FTE-year)."""
    total_h = s.dec("hours.sca_total", 2080)
    vac = s.dec("hours.sca_vacation", 80)
    sick = s.dec("hours.sca_sick", 56)
    hol = line.holiday_hours
    productive = total_h - vac - sick - hol
    wages = line.wage * productive
    vac_d, sick_d, hol_d = vac * line.wage, sick * line.wage, hol * line.wage
    hw = line.hw * total_h
    base = wages + vac_d + sick_d + hol_d + hw
    if indirect_override is not None:
        ind = indirect_override
    else:
        # Lookup order: explicit line lever, calibrated line value, state lever, calibrated state value.
        burden = None
        v = s.get(f"burden.line.{line.key}")
        if v not in (None, ""):
            burden = D(v)
        if burden is None:
            burden = case.site_burden.get(line.key)
        if burden is None:
            v = s.get(f"burden.state.{line.state}")
            if v not in (None, ""):
                burden = D(v)
        if burden is None:
            burden = case.state_burden.get(line.state)
        if burden is None:
            raise KeyError(
                f"no employer-burden for line '{line.key}' (state '{line.state}'); "
                "calibrate from a prior bid or add it to assumptions"
            )
        ind = base * ((1 + burden) * markup(s) - 1)
    rate = round_cents((base + ind) / productive)
    return {
        "productive": productive,
        "wages": wages,
        "vacation": vac_d,
        "sick": sick_d,
        "holiday": hol_d,
        "hw": hw,
        "indirects": ind,
        "rate": rate,
        "hours_total": total_h,
        "vac_h": vac,
        "sick_h": sick,
        "hol_h": hol,
    }


def exempt_rate(line: Line, s: Scenario, period_index: int) -> Decimal:
    """Salary x wraps / 2080 + adder, escalated. Billed on a different hours basis."""
    salary = line.salary
    if line.role and s.get(f"salary.{line.role}") not in (None, ""):
        salary = D(s.get(f"salary.{line.role}"))
    step_delta = s.dec("exempt.salary_steps", 0)
    step_val = s.dec("exempt.step_value", Decimal("0.03"))
    salary = salary * (1 + step_delta * step_val)
    sal_esc = salary
    if period_index >= 1:
        sal_esc = sal_esc * (1 + s.dec("esc.exempt.oy1"))
    if period_index >= 2:
        sal_esc = sal_esc * (1 + s.dec("esc.exempt.oy2"))
    loaded = sal_esc * (1 + s.dec("rate.fringe")) * markup(s)
    loaded += s.dec("exempt.adder", 0)
    return round_cents(loaded / s.dec("hours.exempt_divisor", 2080))


def fte_for(line: Line, s: Scenario) -> Decimal:
    """FTE can be flexed by a lever where the government does not fix it."""
    v = s.get(f"fte.line.{line.key}")
    if v not in (None, ""):
        return D(v)
    if line.role:
        v = s.get(f"fte.{line.role}")
        if v not in (None, ""):
            return D(v)
    return line.fte


def per_fte_hours(line: Line, period: Period, s: Scenario, productive_sca: Decimal) -> Decimal:
    basis = productive_sca if line.classification == SCA else s.dec("hours.exempt_billed", 1912)
    return trunc_cents(basis * period.months / 12)


def price_case(
    case: Case,
    s: Scenario,
    rate_overrides: Optional[Mapping[Tuple[str, str], Decimal]] = None,
    indirect_overrides: Optional[Mapping[str, Decimal]] = None,
) -> PriceResult:
    """Price every line in every period under scenario ``s``."""
    rate_overrides = rate_overrides or {}
    indirect_overrides = indirect_overrides or {}
    out: List[LinePrice] = []
    ffp: Dict[str, Dict[str, Decimal]] = {p.key: {t: Decimal(0) for t in TASKS} for p in case.periods}
    fte_base = Decimal(0)
    fte_opt = Decimal(0)
    hours_total = Decimal(0)

    for line in case.lines:
        fte = fte_for(line, s)
        if line.task == TASK_LABOR:
            fte_base += fte
        else:
            fte_opt += fte
        sca_elems = None
        if line.classification == SCA:
            sca_elems = sca_breakout(line, case, s, indirect_overrides.get(line.key))
        for i, period in enumerate(case.periods):
            if period.key not in line.periods:
                continue
            if sca_elems is not None:
                rate = sca_elems["rate"]
                productive = sca_elems["productive"]
            else:
                rate = exempt_rate(line, s, i)
                productive = s.dec("hours.sca_total", 2080) - s.dec("hours.sca_vacation", 80) - s.dec(
                    "hours.sca_sick", 56
                ) - Decimal(88)
            if (line.key, period.key) in rate_overrides:
                rate = D(rate_overrides[(line.key, period.key)])
            hours = per_fte_hours(line, period, s, productive) * fte
            ext = hours * rate
            out.append(
                LinePrice(
                    line,
                    period.key,
                    hours,
                    rate,
                    ext,
                    sca_elems["indirects"] if sca_elems else Decimal(0),
                )
            )
            ffp[period.key][line.task] += ext
            hours_total += hours

    odc = {p.key: D(case.odc.get(p.key, 0)) for p in case.periods}
    odc_opt = {p.key: D(case.odc_optional.get(p.key, 0)) for p in case.periods}
    period_total = {
        p.key: sum(ffp[p.key].values(), Decimal(0)) + odc[p.key] + odc_opt[p.key] for p in case.periods
    }
    matrix_total = sum(period_total.values(), Decimal(0))

    from .tep import extension_value

    ext = extension_value(case, ffp, odc, odc_opt)
    return PriceResult(
        scenario=s.name,
        lines=out,
        ffp=ffp,
        odc=odc,
        odc_optional=odc_opt,
        period_total=period_total,
        matrix_total=matrix_total,
        tep=matrix_total + ext,
        extension=ext,
        fte_base=fte_base,
        fte_optional=fte_opt,
        hours_total=hours_total,
    )
