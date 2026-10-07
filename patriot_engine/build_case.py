"""Turn extracted facts (TE4 + bid-salary workbook + case.yaml) into a priceable Case.

Everything the government fixes lives in the Case. Everything the pricing
manager may flex lives in the Scenario. This module also reports every place
where it had to guess (unmatched site names, fuzzy matches, unlocated
positions), so the checks and the question generator can surface them.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .extract.bid_inputs import BidInputs
from .extract.te4 import Te4
from .pricing.model import EXEMPT, SCA, TASK_LABOR, TASK_OPT1, TASK_OPT2, Case, Line, Period
from .pricing.money import D


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_case_config(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def state_of(city_state: str) -> str:
    m = re.search(r",\s*([A-Za-z]{2,3})\s*$", city_state or "")
    if m:
        return m.group(1).upper()
    m = re.search(r"\b([A-Z]{2})\b\s*$", city_state or "")
    return m.group(1) if m else ""


@dataclass
class BuildReport:
    fuzzy_matches: List[str] = field(default_factory=list)
    unmatched_sites: List[str] = field(default_factory=list)
    unlocated: Dict[str, int] = field(default_factory=dict)
    split_mismatch: List[str] = field(default_factory=list)
    assumed: List[str] = field(default_factory=list)


def _match_site(name: str, table: Dict[str, dict], aliases: Dict[str, str], report: BuildReport) -> Optional[str]:
    n = norm(name)
    n = aliases.get(n, n)
    if n in table:
        return n
    # Wage tables abbreviate; accept a unique prefix/containment match before fuzzy.
    cands = [k for k in table if k.startswith(n) or n.startswith(k)]
    if len(cands) == 1:
        report.fuzzy_matches.append(f"{name!r} -> {table[cands[0]]['site']!r} (prefix)")
        return cands[0]
    best = difflib.get_close_matches(n, list(table), n=1, cutoff=0.8)
    if best:
        report.fuzzy_matches.append(f"{name!r} -> {table[best[0]]['site']!r} (fuzzy)")
        return best[0]
    report.unmatched_sites.append(name)
    return None


def sca_line_from_row(row: dict, place: str, fte, role: str, roles: dict, task: str = TASK_LABOR, state: str = "", key: str = ""):
    """One SCA line from a wage-table row (shared by the case builder and what-if tools)."""
    return Line(
        key=key or f"{task}/{role}/{place}",
        labor_category=roles["rcc"]["labor_category"],
        place=row["site"],
        task=task,
        classification=SCA,
        fte=D(fte),
        pws_ref=roles["rcc"]["pws_ref"],
        lcat_id=str(roles["rcc"].get("lcat_id", "")),
        role="",
        wd_number=row["wd"],
        wd_rev=row["wd_rev"],
        occupation=row.get("scls_title", "Advocate"),
        wage=D(row["wage"]),
        hw=D(row["hw"]),
        holiday_hours=D(row["holidays"]) * 8,
        state=state or state_of(row.get("city_state", "")),
    )


def build_case(
    cfg: dict,
    te4: Te4,
    bid: BidInputs,
    salaries: Optional[Dict[str, Decimal]] = None,
    site_burden: Optional[Dict[str, Decimal]] = None,
    state_burden: Optional[Dict[str, Decimal]] = None,
):
    """Return (Case, BuildReport). ``salaries`` supplies roles missing from the bid
    workbook (e.g. TSGLI roles, which only appear in the submitted Vol II)."""
    salaries = salaries or {}
    report = BuildReport()
    roles = cfg["roles"]
    aliases = cfg.get("site_aliases", {})
    oconus_tokens = [norm(x) for x in cfg.get("oconus_places", [])]

    wd_by_site = {norm(r["site"]): r for r in bid.sca}
    wd_by_site = {k: {**v} for k, v in wd_by_site.items()}
    oconus_rows = {norm(r["site"]): r for r in bid.oconus}
    exempt_rows = bid.exempt

    def exempt_salary(role_key: str) -> Decimal:
        match = roles[role_key].get("salary_match")
        if role_key in salaries:
            return D(salaries[role_key])
        for r in exempt_rows:
            if match and norm(match) in norm(r["labor_category"]):
                return D(r["salary"])
        report.assumed.append(f"no salary for role '{role_key}' in any input file; left at 0 until calibrated or supplied")
        return Decimal(0)

    def exempt_fte(role_key: str) -> Optional[int]:
        match = roles[role_key].get("salary_match")
        for r in exempt_rows:
            if match and norm(match) in norm(r["labor_category"]):
                return int(r["fte"])
        return None

    lines: List[Line] = []

    def add_exempt(role_key, place, fte, task=TASK_LABOR, key_suffix=""):
        rc = roles[role_key]
        lines.append(
            Line(
                key=f"{task}/{role_key}/{place}{key_suffix}",
                labor_category=rc["labor_category"],
                place=place,
                task=task,
                classification=EXEMPT,
                fte=D(fte),
                pws_ref=rc.get("pws_ref", ""),
                lcat_id=str(rc.get("lcat_id", "")),
                role=role_key,
                salary=exempt_salary(role_key),
            )
        )

    primary = next((s for s in te4.sites if s.get("pm")), te4.sites[0])
    hq = primary["place"]

    # Exempt management/support
    if te4.declared_total.get("pm"):
        add_exempt("pm", hq, te4.declared_total["pm"])
    split = cfg["splits"]["rtl_sl"]
    if sum(split.values()) != te4.declared_total.get("rtl_sl", sum(split.values())):
        report.split_mismatch.append(
            f"rtl_sl: TE4 says {te4.declared_total.get('rtl_sl')} but case.yaml splits into {sum(split.values())}"
        )
    for role_key, n in split.items():
        fte = exempt_fte(role_key) or n
        add_exempt(role_key, hq, fte)
    if te4.declared_total.get("support"):
        add_exempt("support", hq, te4.declared_total["support"])

    # Recovery Care Coordinators by site
    for s in te4.sites:
        n = s.get("rcc", 0)
        if not n:
            continue
        place = s["place"]
        is_oconus = any(tok in norm(place) for tok in oconus_tokens)
        if is_oconus:
            key = _match_site(place, oconus_rows, aliases, report)
            row = oconus_rows.get(key) if key else None
            sal = D(row["salary"]) if row else D(salaries.get("oconus", 0))
            if not row:
                report.assumed.append(f"{place}: OCONUS proxy salary not found; used salaries['oconus']")
            lines.append(
                Line(
                    key=f"{TASK_LABOR}/rcc/{place}",
                    labor_category=roles["rcc"]["labor_category"],
                    place=row["site"] if row else place,
                    task=TASK_LABOR,
                    classification=EXEMPT,
                    fte=D(n),
                    pws_ref=roles["rcc"]["pws_ref"],
                    lcat_id=str(roles["rcc"].get("lcat_id", "")),
                    role="oconus",
                    salary=sal,
                    state="OCONUS",
                    notes="OCONUS priced on a proxy wage, non-SCA build (Rule 11)",
                )
            )
        else:
            key = _match_site(place, wd_by_site, aliases, report)
            row = wd_by_site.get(key) if key else None
            if row is None:
                continue
            lines.append(
                Line(
                    key=f"{TASK_LABOR}/rcc/{place}",
                    labor_category=roles["rcc"]["labor_category"],
                    place=row["site"],
                    task=TASK_LABOR,
                    classification=SCA,
                    fte=D(n),
                    pws_ref=roles["rcc"]["pws_ref"],
                    lcat_id=str(roles["rcc"].get("lcat_id", "")),
                    wd_number=row["wd"],
                    wd_rev=row["wd_rev"],
                    occupation=row.get("scls_title", "Advocate"),
                    wage=D(row["wage"]),
                    hw=D(row["hw"]),
                    holiday_hours=D(row["holidays"]) * 8,
                    state=state_of(s.get("city", "")) or state_of(row.get("city_state", "")),
                )
            )

    # Transition Case Coordinators (SCA, at the HQ site)
    if te4.declared_total.get("tcc"):
        hqrow = wd_by_site.get(_match_site(hq, wd_by_site, aliases, report) or "")
        if hqrow:
            lines.append(
                Line(
                    key=f"{TASK_LABOR}/tcc/{hq}",
                    labor_category=roles["tcc"]["labor_category"],
                    place=hqrow["site"],
                    task=TASK_LABOR,
                    classification=SCA,
                    fte=D(te4.declared_total["tcc"]),
                    pws_ref=roles["tcc"]["pws_ref"],
                    lcat_id=str(roles["tcc"].get("lcat_id", "")),
                    role="tcc",
                    wd_number=hqrow["wd"],
                    wd_rev=hqrow["wd_rev"],
                    occupation=hqrow.get("scls_title", "Advocate"),
                    wage=D(hqrow["wage"]),
                    hw=D(hqrow["hw"]),
                    holiday_hours=D(hqrow["holidays"]) * 8,
                    state=state_of(primary.get("city", "")),
                )
            )

    # Optional Task 2: one RCC with no assigned location (priced at the highest WD wage)
    unl = te4.unlocated()
    report.unlocated = dict(unl)
    if unl.get("rcc_opt2"):
        top = max(bid.sca, key=lambda r: D(r["wage"]))
        report.assumed.append(
            f"Optional Task 2 RCC has no location in TE4; priced at the highest WD wage "
            f"({top['site']}, ${top['wage']})"
        )
        lines.append(
            Line(
                key=f"{TASK_OPT2}/rcc_opt2/Unknown",
                labor_category=roles["rcc_opt2"]["labor_category"],
                place="Unknown",
                task=TASK_OPT2,
                classification=SCA,
                fte=D(unl["rcc_opt2"]),
                pws_ref=roles["rcc_opt2"]["pws_ref"],
                lcat_id=str(roles["rcc_opt2"].get("lcat_id", "")),
                role="rcc_opt2",
                wd_number=top["wd"],
                wd_rev=top["wd_rev"],
                occupation=top.get("scls_title", "Advocate"),
                wage=D(top["wage"]),
                hw=D(top["hw"]),
                holiday_hours=D(top["holidays"]) * 8,
                state="UNK",
            )
        )

    # Optional Task 1: TSGLI
    for role_key, n in cfg["splits"]["tsgli_opt1"].items():
        if te4.declared_total.get("tsgli_opt1"):
            add_exempt(role_key, hq, n, task=TASK_OPT1)

    periods = [Period(p["key"], int(p["months"]), p.get("label", "")) for p in cfg["periods"]]
    case = Case(
        name=cfg["name"],
        periods=periods,
        lines=lines,
        odc={k: D(v) for k, v in cfg["odc"].items()},
        odc_optional={k: D(v) for k, v in cfg["odc_optional"].items()},
        extension_months=int(cfg.get("extension_months", 0)),
        extension_includes_odc=bool(cfg.get("extension_includes_odc", False)),
        site_burden={k: D(v) for k, v in (site_burden or {}).items()},
        state_burden={k: D(v) for k, v in (state_burden or {}).items()},
        site_aliases=dict(aliases),
    )
    return case, report


_PLACE_SUFFIX = re.compile(r"(oconus|afb|sfb|ab|field)$")


def _place_stem(name: str) -> str:
    n = norm(name)
    prev = None
    while prev != n:
        prev = n
        n = _PLACE_SUFFIX.sub("", n)
    return n


def same_place(a: str, b: str, aliases: Optional[Dict[str, str]] = None) -> bool:
    """Tolerant place-name comparison for TE4 vs TE5 vs wage tables (they are typed by hand)."""
    aliases = aliases or {}
    na, nb = norm(a), norm(b)
    x, y = _place_stem(aliases.get(na, na)), _place_stem(aliases.get(nb, nb))
    if not x or not y:
        return False
    if x == y:
        return True
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    if len(short) >= 4 and short in long_:
        return True
    return difflib.SequenceMatcher(None, x, y).ratio() >= 0.85
