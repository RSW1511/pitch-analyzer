"""M6 checks and alerts: the nineteen engine rules (Game Plan section 4) as code.

Each check returns Findings. A Finding has a severity:
  BLOCK   - do not export (the price or file is wrong or non-compliant)
  REVIEW  - a person must decide; carries a dollar impact where it can be computed
  INFO    - worth knowing; logged in the review pack

Checks never change a number. They read a Case, a priced result, and optional
extra context (TE4 versions, Q&A rows, intake register, a submitted workbook).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from .build_case import norm, same_place
from .pricing.model import EXEMPT, SCA, Case, PriceResult, Scenario, price_case
from .pricing.money import D, round_cents

BLOCK, REVIEW, INFO = "BLOCK", "REVIEW", "INFO"


@dataclass
class Finding:
    rule: int
    severity: str
    title: str
    detail: str
    impact: Optional[Decimal] = None  # dollars on the TEP basis where computable
    evidence: str = ""

    def to_dict(self):
        d = asdict(self)
        d["impact"] = None if self.impact is None else str(round_cents(self.impact))
        return d


@dataclass
class Context:
    case: Case
    scenario: Scenario
    result: PriceResult
    te4: Optional[object] = None  # extract.te4.Te4
    te4_prior: Optional[object] = None
    te5_places: Optional[List[str]] = None
    qa_rows: Optional[List[dict]] = None
    register: Optional[List[dict]] = None  # intake register rows
    assumptions: Optional[List[dict]] = None  # rows of assumptions.csv
    matrix: Optional[dict] = None  # a filled matrix read by extract.price_matrix
    extra: Dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
def rule1_qa_governs(ctx: Context) -> List[Finding]:
    """Q&A answers are a governing document: surface staffing facts that appear only there."""
    out = []
    if not ctx.qa_rows:
        return [Finding(1, INFO, "No Q&A attachment loaded", "Load the Q&A answers: they govern as much as the PWS.")]
    pat = re.compile(r"\b(?:\w+)\s*\((\d+)\)")
    by_role = {}
    for l in ctx.case.lines:
        key = l.role or l.labor_category
        by_role[key] = by_role.get(key, Decimal(0)) + l.fte
    for r in ctx.qa_rows:
        ans = str(r.get("answer", ""))
        for m in pat.finditer(ans):
            n = int(m.group(1))
            ctx_txt = ans[max(0, m.start() - 40): m.end() + 60]
            if re.search(r"FTE|not including|required", ctx_txt, re.I) and "Support" in ctx_txt + str(r.get("question", "")) + str(r.get("ref", "")):
                priced = by_role.get("support", Decimal(0))
                sev = INFO if priced == n else REVIEW
                out.append(
                    Finding(
                        1,
                        sev,
                        f"Q&A #{r.get('number')} states {n} FTE for support",
                        f"Case prices {priced}. Q&A text: {ans[:160]}",
                        evidence=f"Q&A #{r.get('number')}",
                    )
                )
                break
    return out


def rule2_conflicts_priced_to_table(ctx: Context) -> List[Finding]:
    out = []
    for a in ctx.assumptions or []:
        if a.get("status") == "Conflict flagged" and a["key"].startswith("fte."):
            k = a["key"]
            role = k.split(".", 1)[1]
            cur = (
                ctx.scenario.dec(k)
                if ctx.scenario.get(k) not in (None, "")
                else sum((l.fte for l in ctx.case.lines if l.role == role), Decimal(0))
            )
            up = price_case(ctx.case, ctx.scenario.set(k, str(cur + 1)))
            out.append(
                Finding(
                    2,
                    REVIEW,
                    f"Table-vs-text conflict on {k}",
                    f"Priced to the table ({cur}); the alternative reading (+1) is flagged. {a.get('rationale','')}",
                    impact=up.tep - ctx.result.tep,
                    evidence=a.get("source", ""),
                )
            )
    return out


def rule5_site_level(ctx: Context) -> List[Finding]:
    if ctx.te4 is None or ctx.te4_prior is None:
        return []
    from .differ import diff_te4

    ch = [c for c in diff_te4(ctx.te4_prior, ctx.te4) if c.relevance >= 3 and c.location != "(totals)"]
    if not ch:
        return []
    same_tot = ctx.te4.declared_total == ctx.te4_prior.declared_total
    return [
        Finding(
            5,
            REVIEW,
            "Site mix changed between statement versions" + (" with unchanged totals" if same_tot else ""),
            "; ".join(f"{c.location}: {c.old}->{c.new}" for c in ch[:8]) + ". Re-price at site level (see delta report).",
        )
    ]


def rule6_base_vs_optional(ctx: Context) -> List[Finding]:
    out = []
    r = ctx.result
    if ctx.te4 is not None:
        d = ctx.te4.declared_total
        want_base = d.get("pm", 0) + d.get("rcc", 0) + d.get("tcc", 0) + d.get("rtl_sl", 0) + d.get("support", 0)
        want_opt = d.get("tsgli_opt1", 0) + d.get("rcc_opt2", 0)
        if r.fte_base != want_base or r.fte_optional != want_opt:
            out.append(
                Finding(
                    6,
                    REVIEW,
                    "Base/optional split differs from TE4",
                    f"Priced {r.fte_base} base + {r.fte_optional} optional; TE4 says {want_base} + {want_opt}.",
                )
            )
        else:
            out.append(Finding(6, INFO, "Base/optional split matches TE4", f"{want_base} base + {want_opt} optional."))
    return out


def rule7_wage_determinations(ctx: Context) -> List[Finding]:
    out = []
    sca = [l for l in ctx.case.lines if l.classification == SCA]
    hw = {}
    for l in sca:
        hw.setdefault((l.wd_number, l.wd_rev, l.hw), []).append(l.place)
    if len({k[2] for k in hw}) > 1:
        out.append(Finding(7, REVIEW, "More than one H&W rate in use", f"{ {k: len(v) for k, v in hw.items()} }. Separate SCLS breakout worksheets are required (TOEP IV.2.2)."))
    if ctx.register:
        wds_used = {norm(l.wd_number) for l in sca}
        for row in ctx.register:
            m = re.search(r"WD[_ ]?(\d{4})[_ -](\d{4})", row.get("name", ""), re.I)
            if m:
                num = f"{m.group(1)}{m.group(2)}"
                if num not in wds_used:
                    out.append(
                        Finding(
                            7,
                            REVIEW,
                            f"Wage determination {m.group(1)}-{m.group(2)} is in the package but no line uses it",
                            f"{row.get('name')}. Check whether any site belongs on it: an area-specific determination can carry a different H&W rate. Sites priced on a different WD than the area's must be justified.",
                            evidence=row.get("path", ""),
                        )
                    )
    if ctx.te4 is not None:
        for s in ctx.te4.sites:
            if s.get("rcc") and not any(same_place(s["place"], l.place, ctx.case.site_aliases) for l in ctx.case.lines if l.task == "labor"):
                out.append(Finding(7, BLOCK, f"No priced line for TE4 site {s['place']}", "Map every site to a wage determination and revision."))
    return out


def rule8_sca_vs_exempt(ctx: Context) -> List[Finding]:
    n_sca = sum(l.fte for l in ctx.case.lines if l.classification == SCA)
    n_ex = sum(l.fte for l in ctx.case.lines if l.classification == EXEMPT)
    return [
        Finding(
            8,
            INFO,
            "SCA-vs-exempt split is an offeror choice",
            f"{n_sca} FTE priced SCA, {n_ex} FTE priced exempt/non-SCA. The government declined to say which roles are SCA (Q&A #41); the split is a visible lever.",
        )
    ]


def rule9_hours_vs_rate_basis(ctx: Context) -> List[Finding]:
    out = []
    s = ctx.scenario
    billed, divisor = s.dec("hours.exempt_billed", 1912), s.dec("hours.exempt_divisor", 2080)
    if billed != divisor:
        exempt_total = sum((lp.extended for lp in ctx.result.lines if lp.line.classification == EXEMPT), Decimal(0))
        gap = exempt_total * (divisor / billed - 1)
        out.append(
            Finding(
                9,
                REVIEW,
                "Exempt rate divided by %s hours but billed on %s" % (divisor, billed),
                "Rate recovers the annual cost over 2,080 paid hours while labour is billed on productive hours, so full-year cost is under-recovered by the amount shown (matrix basis, all periods). Confirm this is deliberate.",
                impact=gap,
            )
        )
    for l in ctx.case.lines:
        if l.classification == SCA:
            productive = s.dec("hours.sca_total", 2080) - s.dec("hours.sca_vacation", 80) - s.dec("hours.sca_sick", 56) - l.holiday_hours
            if productive != Decimal(1856) and l.holiday_hours == Decimal(88):
                out.append(Finding(9, REVIEW, "SCA productive hours differ from 1,856", f"{productive}; SCA rates divide by productive hours and labour tabs must use the same figure (else ~12% double count)."))
                break
    return out


def rule10_state_costs(ctx: Context) -> List[Finding]:
    out = []
    by_state: Dict[str, List[Decimal]] = {}
    for l in ctx.case.lines:
        if l.classification == SCA and l.state:
            b = ctx.case.site_burden.get(l.key) or ctx.case.state_burden.get(l.state)
            if b is not None:
                by_state.setdefault(l.state, []).append(b)
    if by_state:
        med = sorted(sum(v) / len(v) for v in by_state.values())[len(by_state) // 2]
        hi = {st: sum(v) / len(v) for st, v in by_state.items() if sum(v) / len(v) - med > D("0.05")}
        if hi:
            out.append(
                Finding(
                    10,
                    INFO,
                    "State-specific cost loads detected",
                    "Employer burden is above the median by more than 5 points in: "
                    + ", ".join(f"{k} (+{(v - med) * 100:.1f} pts)" for k, v in sorted(hi.items()))
                    + ". Check these against state gross-receipts, payroll-tax and insurance rules.",
                )
            )
    missing = [l.key for l in ctx.case.lines if l.classification == SCA and l.key not in ctx.case.site_burden and l.state not in ctx.case.state_burden and not ctx.scenario.get(f"burden.state.{l.state}")]
    if missing:
        out.append(Finding(10, BLOCK, "SCA lines without an employer-burden value", ", ".join(missing[:6])))
    return out


def rule11_overseas(ctx: Context) -> List[Finding]:
    o = [l for l in ctx.case.lines if l.state == "OCONUS"]
    if not o:
        return []
    return [
        Finding(
            11,
            REVIEW,
            "Overseas sites priced on a proxy wage",
            f"{len(o)} OCONUS lines priced from a proxy salary (${o[0].salary}); no WD exists for these sites and no overseas allowances are priced. Confirm the proxy and allowances with the pricing manager.",
        )
    ]


def rule12_tep_basis(ctx: Context) -> List[Finding]:
    r = ctx.result
    gap = r.tep - r.matrix_total
    return [
        Finding(
            12,
            INFO,
            "Compare and solve on Total Evaluated Price",
            f"Matrix total {round_cents(r.matrix_total)} vs TEP {round_cents(r.tep)} (+{round_cents(gap)} from the {ctx.case.extension_months}-month 52.217-8 extension at the last option year's rates).",
            impact=gap,
        )
    ]


def rule13_annualise(ctx: Context) -> List[Finding]:
    r = ctx.result
    p = ctx.case.periods
    if len(p) < 2 or p[0].months == 12:
        return []
    first, second = r.period_total[p[0].key], r.period_total[p[1].key]
    raw = (second / first - 1) * 100
    ann = (second / (first * D(12) / D(p[0].months)) - 1) * 100
    return [
        Finding(
            13,
            INFO,
            "Annualise partial periods before computing escalation",
            f"{p[0].key} covers {p[0].months} months: raw growth to {p[1].key} is {raw:.1f}%, annualised {ann:.1f}%. Use the annualised figure.",
        )
    ]


def rule15_price_every_site(ctx: Context) -> List[Finding]:
    """Rates become the contract rate card; flag floors and cross-subsidy."""
    out = []
    sca = [(lp, lp.line) for lp in ctx.result.lines if lp.period == "base" and lp.line.classification == SCA]
    below = []
    for lp, l in sca:
        productive = ctx.scenario.dec("hours.sca_total", 2080) - ctx.scenario.dec("hours.sca_vacation", 80) - ctx.scenario.dec("hours.sca_sick", 56) - l.holiday_hours
        floor = (l.wage * 2080 + l.hw * 2080) / productive
        if lp.rate < floor:
            below.append(f"{l.place} {lp.rate} < WD floor {round_cents(floor)}")
    if below:
        out.append(Finding(15, BLOCK, "Rate below the wage-determination floor", "; ".join(below[:5])))
    return out


def rule16_zero_cost_discriminators(ctx: Context) -> List[Finding]:
    fte = sum(l.fte for l in ctx.case.lines if l.classification == SCA)
    hw = {l.hw for l in ctx.case.lines if l.classification == SCA}
    if not hw:
        return []
    h = max(hw)
    return [
        Finding(
            16,
            INFO,
            "Zero-cost discriminator candidate: cash in lieu of health benefits",
            f"The mandatory H&W (${h}/hr x 2,080 = ${h * 2080}/yr per FTE) can be offered as cash in lieu at no added cost to the price; on a prior bid it earned a significant strength. Applies to {fte} SCA FTE.",
        )
    ]


ALL_CHECKS = [
    rule1_qa_governs,
    rule2_conflicts_priced_to_table,
    rule5_site_level,
    rule6_base_vs_optional,
    rule7_wage_determinations,
    rule8_sca_vs_exempt,
    rule9_hours_vs_rate_basis,
    rule10_state_costs,
    rule11_overseas,
    rule12_tep_basis,
    rule13_annualise,
    rule15_price_every_site,
    rule16_zero_cost_discriminators,
]


def run_checks(ctx: Context, only: Optional[Iterable[int]] = None) -> List[Finding]:
    out: List[Finding] = []
    for fn in ALL_CHECKS:
        out += fn(ctx)
    if only:
        out = [f for f in out if f.rule in set(only)]
    order = {BLOCK: 0, REVIEW: 1, INFO: 2}
    out.sort(key=lambda f: (order[f.severity], -(abs(f.impact) if f.impact is not None else 0)))
    return out


def has_blockers(findings: Iterable[Finding]) -> bool:
    return any(f.severity == BLOCK for f in findings)
