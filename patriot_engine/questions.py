"""M7 question generator.

Starter list of questions for the government (and, separately, for the company's own
pricing manager), de-duplicated against the Q&A already published and against what
we already asked, and ranked by price impact (Game Plan section 1, our addition).

Questions are drafted from rules, not from a language model, so the list is
reproducible. A person edits the wording before anything is sent (AI makes no
decisions for the company).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from .build_case import norm, same_place, sca_line_from_row
from .pricing.model import SCA, Case, Line, Scenario, price_case
from .pricing.money import D, round_cents


@dataclass
class Question:
    audience: str  # "government" | "internal"
    text: str
    why: str
    impact: Optional[Decimal] = None
    source: str = ""
    status: str = "new"  # new | already asked (Q&A #n) | asked by us
    rank: int = 0

    def to_dict(self):
        d = asdict(self)
        d["impact"] = None if self.impact is None else str(round_cents(self.impact))
        return d


def _similar(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]{4,}", a.lower()))
    tb = set(re.findall(r"[a-z0-9]{4,}", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _priced_with(case: Case, s: Scenario, extra_lines: List[Line] = (), drop: Iterable[str] = ()) -> Decimal:
    lines = [l for l in case.lines if l.key not in set(drop)] + list(extra_lines)
    c2 = Case(**{**case.__dict__, "lines": lines})
    return price_case(c2, s).tep


def te_mismatch_questions(te4, te5_places: List[str], case: Case, s: Scenario, bid, roles: dict) -> List[Question]:
    """TE4 vs TE5 place disagreements, each priced as a one-FTE move (for example one coordinator moved between two bases)."""
    if not te5_places:
        return []
    only4 = [x["place"] for x in te4.sites if not any(same_place(x["place"], p, case.site_aliases) for p in te5_places)]
    only5 = [p for p in te5_places if not any(same_place(x["place"], p, case.site_aliases) for x in te4.sites)]
    if not only4 and not only5:
        return []
    impact = None
    rows = {norm(r["site"]): r for r in bid.sca}
    if len(only4) == 1 and len(only5) == 1:
        a = next((l for l in case.lines if same_place(l.place, only4[0], case.site_aliases) and l.task == "labor" and l.classification == SCA), None)
        row = next((r for k, r in rows.items() if same_place(r["site"], only5[0], case.site_aliases)), None)
        if a is not None and row is not None:
            new = sca_line_from_row(row, only5[0], 1, "rcc", roles, state=a.state)
            new.key = f"labor/rcc/{only5[0]}"
            base = price_case(case, s).tep
            # moving requires a burden value for the new site; fall back to the old site's
            case2 = Case(**{**case.__dict__, "site_burden": {**case.site_burden, new.key: case.site_burden.get(a.key, Decimal(0))}})
            lines = []
            for l in case.lines:
                if l.key == a.key:
                    l = Line(**{**l.__dict__, "fte": l.fte - 1})
                    if l.fte <= 0:
                        continue
                lines.append(l)
            lines.append(new)
            case2.lines = lines
            impact = abs(price_case(case2, s).tep - base)
    txt = (
        "Technical Exhibits 4 and 5 list different places of performance"
        f" (TE4 only: {', '.join(only4) or 'none'}; TE5 only: {', '.join(only5) or 'none'})."
        " Which list is correct?"
    )
    return [Question("government", txt, "Staffing table and area-of-responsibility table disagree; each site has its own wage determination rate.", impact, "TE4 vs TE5")]


def build_questions(
    case: Case,
    s: Scenario,
    te4=None,
    te5_places: Optional[List[str]] = None,
    bid=None,
    roles: Optional[dict] = None,
    qa_rows: Optional[List[dict]] = None,
    patriot_asked: Optional[List[str]] = None,
    assumptions: Optional[List[dict]] = None,
    alt_hw: Optional[Dict[str, dict]] = None,
) -> List[Question]:
    qs: List[Question] = []
    base = price_case(case, s)

    # 1. Positions with no location (an optional-task position the government has not placed).
    for l in case.lines:
        if l.place == "Unknown":
            sca_rates = [lp.rate for lp in base.lines if lp.line.classification == SCA and lp.line.task == "labor" and lp.period == "oy1"]
            avg = sum(sca_rates, Decimal(0)) / len(sca_rates) if sca_rates else Decimal(0)
            hi = [lp for lp in base.lines if lp.line.key == l.key]
            imp = sum(((lp.rate - avg) * lp.hours for lp in hi), Decimal(0))
            qs.append(
                Question(
                    "government",
                    f"Optional Task {l.task[-1]} adds {l.fte} {l.labor_category} with no assigned location. Can the Government give a planning location, or confirm the rate card applies at whichever site is later named?",
                    "Without a location we price at the highest wage determination rate in the package; the bid rate becomes the contract rate.",
                    abs(imp),
                    "TE4 note",
                )
            )

    # 2. TE4 / TE5 disagreements.
    if te4 is not None and te5_places and bid is not None and roles is not None:
        qs += te_mismatch_questions(te4, te5_places, case, s, bid, roles)

    # 3. Overseas pricing basis.
    ocon = [lp for lp in base.lines if lp.line.state == "OCONUS"]
    if ocon:
        total = sum((lp.extended for lp in ocon), Decimal(0))
        qs.append(
            Question(
                "government",
                "For the OCONUS places of performance, which wage determination or allowance schedule applies, and are post, COLA or SOFA-related costs reimbursable outside the FFP rate?",
                "No wage determination covers these sites; we price off a CONUS proxy wage with no allowances. Every 10% of error is the impact shown.",
                total * D("0.10"),
                "Rule 11",
            )
        )

    # 4. Second wage determination for part of the footprint.
    for wd, info in (alt_hw or {}).items():
        n = sum((l.fte for l in case.lines if l.classification == SCA and info["city_token"].lower() in l.place.lower()), Decimal(0))
        diff = (D(info["hw"]) - max((l.hw for l in case.lines if l.classification == SCA), default=Decimal(0))) * 2080
        yrs = sum(D(p.months) for p in case.periods) / 12
        qs.append(
            Question(
                "government",
                f"Wage determination {wd} (H&W ${info['hw']}) was added for the {info['area']} area. Does it apply to all Government sites in that area, or only to named sites?",
                "H&W differs between the two determinations; sites priced on the wrong one are non-compliant.",
                abs(diff) * n * yrs,
                f"WD {wd}",
            )
        )

    # 5. Internal questions from low-confidence or conflicted assumptions.
    for a in assumptions or []:
        open_q = "open question" in (a.get("rationale", "") + a.get("description", "")).lower() or "ask" in a.get("rationale", "").lower()
        if a.get("status") == "Conflict flagged" or (a.get("confidence") == "Low" and open_q):
            if a["key"].startswith("burden."):
                continue
            qs.append(
                Question(
                    "internal",
                    f"{a.get('description') or a['key']}: {a.get('rationale') or 'please confirm the value and its basis.'}",
                    f"Register row {a['key']} is {a.get('status')} / {a.get('confidence')} confidence.",
                    None,
                    a.get("source", ""),
                )
            )

    # De-duplicate against published Q&A and our own questions.
    for q in qs:
        if q.audience != "government":
            continue
        for r in qa_rows or []:
            if _similar(q.text, r.get("question", "")) >= 0.6:
                q.status = f"already asked (Q&A #{r.get('number')})"
                break
        else:
            for t in patriot_asked or []:
                if _similar(q.text, t) >= 0.6:
                    q.status = "asked by us"
                    break

    qs.sort(key=lambda q: (q.audience != "government", q.status != "new", -(q.impact or 0)))
    for i, q in enumerate(qs, 1):
        q.rank = i
    return qs
