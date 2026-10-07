"""M1 collector: USAspending award, modification ledger and offers (Game Plan 3.1-3.2).

Free API, no key. What it gives: incumbent, vehicle, period-of-performance end, total
obligated, every modification's dollars, number of offers. What it does NOT give: CLIN
prices, rates or FTE (those come from the award document itself via FOIA or vehicle
access) and "base and all options" can equal obligations, understating potential value.

Guardrails (section 3.5): public data only; every response is saved with its URL,
retrieval date and SHA-256 under 00-inputs/web/; requests are polite (one at a time,
with a pause) and the HTTP layer is injectable so the parser is tested offline.

NOTE: field names below follow the documented USAspending v2 responses. They are read
defensively (``.get``), and the parser is covered by tests that use recorded-shape data,
but it has not been exercised against the live API from the build sandbox, whose egress
policy blocks api.usaspending.gov.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..pricing.money import D, round_cents

API = "https://api.usaspending.gov/api/v2"

# Rule 14: split incumbent price changes into escalation vs WD adjustment vs scope.
CLASSIFIERS = [
    ("admin", re.compile(r"stop[- ]work|change of address|novation|point of contact|poc\b|administrative|correct(ion)? of|cdrl|funding doc", re.I)),
    ("wage_determination", re.compile(r"wage determination|\bwd\b|sca\b|service contract (act|labor)|52\.222-4[34]|price adjustment|health (and|&) welfare|\bh&w\b", re.I)),
    ("option_exercise", re.compile(r"exercise (of )?(the )?option|option (year|period)|\boy[1-4]\b|52\.217", re.I)),
    ("scope", re.compile(r"scope|add(ition|s)? of|reduc|decrease in|increase in|staffing|positions?|fte|revised pws|pws", re.I)),
    ("funding", re.compile(r"incremental fund|obligat|fund(s|ing)\b|ceiling", re.I)),
]


ACTION_TYPE_MAP = [
    ("option_exercise", re.compile(r"exercise an option|option", re.I)),
    ("funding", re.compile(r"funding only|incremental", re.I)),
    ("scope", re.compile(r"change order|additional work|out of scope|new work", re.I)),
    ("admin", re.compile(r"administrative|no cost|close ?out|terminate|stop", re.I)),
]


def classify_mod(description: str, action_type: str = "") -> str:
    """Description first (it says what the money was for), FPDS action type as a fallback.
    "Supplemental agreement for work within scope" is deliberately *not* a class: it says
    nothing about why the price moved."""
    for name, rx in CLASSIFIERS:
        if rx.search(description or ""):
            return name
    for name, rx in ACTION_TYPE_MAP:
        if rx.search(action_type or ""):
            return name
    return "unclassified"


@dataclass
class Mod:
    number: str
    date: str
    kind: str
    amount: Decimal
    running_total: Decimal
    description: str = ""
    action_type: str = ""

    def to_dict(self):
        d = asdict(self)
        d["amount"] = str(round_cents(self.amount))
        d["running_total"] = str(round_cents(self.running_total))
        return d


@dataclass
class Award:
    generated_id: str
    piid: str = ""
    parent_piid: str = ""
    recipient: str = ""
    uei: str = ""
    start: str = ""
    end: str = ""
    total_obligation: Decimal = Decimal(0)
    base_and_all_options: Decimal = Decimal(0)
    offers: Optional[int] = None
    extent_competed: str = ""
    set_aside: str = ""
    naics: str = ""
    psc: str = ""
    raw_hash: str = ""

    def to_dict(self):
        d = asdict(self)
        d["total_obligation"] = str(self.total_obligation)
        d["base_and_all_options"] = str(self.base_and_all_options)
        return d


HttpFn = Callable[[str, str, Optional[dict]], dict]


def default_http(method: str, url: str, body: Optional[dict] = None) -> dict:
    import requests

    r = requests.request(method, url, json=body, timeout=60, headers={"User-Agent": "patriot-engine/0.1 (academic capstone; public data)"})
    r.raise_for_status()
    return r.json()


class UsaSpending:
    def __init__(self, http: HttpFn = default_http, save_dir: Optional[str | Path] = None, pause: float = 0.5):
        self.http = http
        self.save_dir = Path(save_dir) if save_dir else None
        self.pause = pause

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{API}{path}"
        data = self.http(method, url, body)
        self._save(url, body, data)
        if self.pause:
            time.sleep(self.pause)
        return data

    def _save(self, url: str, body: Optional[dict], data: dict) -> None:
        if not self.save_dir:
            return
        self.save_dir.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(data, sort_keys=True).encode()
        h = hashlib.sha256(blob).hexdigest()
        stem = re.sub(r"[^A-Za-z0-9]+", "_", url.replace(API, ""))[:60]
        (self.save_dir / f"{date.today().isoformat()}_{stem}_{h[:8]}.json").write_text(
            json.dumps({"url": url, "body": body, "retrieved": date.today().isoformat(), "sha256": h, "response": data}, indent=1)
        )

    # -- endpoints -------------------------------------------------------------
    def find_award(self, keyword: str, award_types: Optional[List[str]] = None) -> List[dict]:
        body = {
            "filters": {"keywords": [keyword], "award_type_codes": award_types or ["A", "B", "C", "D"]},
            "fields": ["Award ID", "Recipient Name", "Start Date", "End Date", "Award Amount", "generated_internal_id"],
            "limit": 10,
            "page": 1,
        }
        return self._call("POST", "/search/spending_by_award/", body).get("results", [])

    def award(self, generated_id: str) -> Award:
        d = self._call("GET", f"/awards/{generated_id}/")
        return parse_award(generated_id, d)

    def transactions(self, generated_id: str) -> List[dict]:
        out, page = [], 1
        while True:
            body = {"award_id": generated_id, "limit": 100, "page": page, "sort": "action_date", "order": "asc"}
            d = self._call("POST", "/transactions/", body)
            out += d.get("results", [])
            if not d.get("page_metadata", {}).get("hasNext"):
                break
            page += 1
        return out


def parse_award(generated_id: str, d: dict) -> Award:
    pop = d.get("period_of_performance") or {}
    rec = d.get("recipient") or {}
    ltcd = d.get("latest_transaction_contract_data") or {}
    parent = d.get("parent_award") or {}
    naics = d.get("naics_hierarchy", {}).get("base_code", {}) if isinstance(d.get("naics_hierarchy"), dict) else {}
    psc = d.get("psc_hierarchy", {}).get("base_code", {}) if isinstance(d.get("psc_hierarchy"), dict) else {}
    offers = ltcd.get("number_of_offers_received")
    return Award(
        generated_id=generated_id,
        piid=d.get("piid", ""),
        parent_piid=(parent or {}).get("piid", ""),
        recipient=rec.get("recipient_name", ""),
        uei=rec.get("recipient_uei", ""),
        start=pop.get("start_date", ""),
        end=pop.get("end_date", "") or pop.get("potential_end_date", ""),
        total_obligation=D(d.get("total_obligation") or 0),
        base_and_all_options=D(d.get("base_and_all_options") or d.get("base_exercised_options") or 0),
        offers=int(offers) if offers not in (None, "") else None,
        extent_competed=ltcd.get("extent_competed_description", "") or ltcd.get("extent_competed", ""),
        set_aside=ltcd.get("type_set_aside_description", "") or ltcd.get("type_set_aside", ""),
        naics=naics.get("code", "") if isinstance(naics, dict) else "",
        psc=psc.get("code", "") if isinstance(psc, dict) else "",
        raw_hash=hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest(),
    )


def build_mod_ledger(transactions: List[dict]) -> List[Mod]:
    """Chronological modification ledger with a running total and a Rule-14 class."""
    rows = sorted(transactions, key=lambda t: (t.get("action_date", ""), str(t.get("modification_number", ""))))
    out, running = [], Decimal(0)
    for t in rows:
        amt = D(t.get("federal_action_obligation") or 0)
        running += amt
        desc = t.get("description") or t.get("award_description") or ""
        at = t.get("action_type_description") or t.get("action_type") or ""
        out.append(Mod(str(t.get("modification_number", "")), t.get("action_date", ""), classify_mod(desc, at), amt, running, desc, at))
    return out


def summarise_ledger(ledger: List[Mod]) -> Dict[str, object]:
    by: Dict[str, Decimal] = {}
    for m in ledger:
        by[m.kind] = by.get(m.kind, Decimal(0)) + m.amount
    return {
        "transactions": len(ledger),
        "total": str(round_cents(ledger[-1].running_total)) if ledger else "0.00",
        "by_class": {k: str(round_cents(v)) for k, v in sorted(by.items())},
    }


def reconcile(ledger: List[Mod], award: Award, documents_in_hand: Optional[int] = None) -> List[str]:
    """Check the ledger against the award record and the documents on disk (RCC: 26 transactions)."""
    out = []
    if award.total_obligation and ledger and round_cents(ledger[-1].running_total) != round_cents(award.total_obligation):
        out.append(f"ledger sums to {round_cents(ledger[-1].running_total)} but award shows {round_cents(award.total_obligation)} obligated")
    if documents_in_hand is not None and documents_in_hand != len(ledger):
        out.append(f"{len(ledger)} transactions on USAspending but {documents_in_hand} modification documents in hand: file the gap in the FOIA request")
    if award.base_and_all_options and award.base_and_all_options == award.total_obligation:
        out.append("'base and all options' equals obligations: potential value is understated; take per-period values from the award document")
    return out
