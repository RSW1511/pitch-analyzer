"""M9 second brain: the company's pricing history as plain files any person or AI can read.

* ``bids-index.csv``: one row per bid (agency, vehicle, FTE, wraps, price, TEP, basis,
  offerors, outcome, ratings, winning price if known).
* ``lessons/<bid>.md``: sourced facts kept separate from judgements; strengths and
  weaknesses verbatim; the per-site rate card; zero-cost discriminators; what to ask in
  the next debrief.

Phase 5 feeds it after every award or loss. Entries are drafted here and approved by a
person; nothing in it is used to make a decision automatically.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from .pricing.model import PriceResult
from .pricing.money import round_cents

INDEX_COLS = [
    "bid", "agency", "vehicle", "solicitation", "fte_base", "fte_optional", "wrap_rates", "matrix_total",
    "tep", "basis", "offerors", "outcome", "technical_rating", "price_rating", "winning_price", "competitor_prices", "lessons",
]


@dataclass
class Outcome:
    bid: str
    outcome: str  # Won | Lost | No bid
    technical_rating: str = ""
    strengths: List[str] = field(default_factory=list)
    weaknesses: List[str] = field(default_factory=list)
    offerors: Optional[int] = None
    winning_price: str = ""
    competitor_prices: Dict[str, str] = field(default_factory=dict)
    evaluator_comments: List[str] = field(default_factory=list)
    debrief_questions: List[str] = field(default_factory=list)
    judgements: List[str] = field(default_factory=list)


def upsert_index(path: str | Path, row: Dict[str, str]) -> None:
    path = Path(path)
    rows: List[Dict[str, str]] = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    for i, r in enumerate(rows):
        if r.get("bid") == row["bid"]:
            rows[i] = {**r, **{k: v for k, v in row.items() if v != ""}}
            break
    else:
        rows.append(row)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in INDEX_COLS})


def record_bid(
    root: str | Path,
    bid: str,
    result: PriceResult,
    meta: Dict[str, str],
    wraps: Dict[str, str],
    outcome: Optional[Outcome] = None,
    zero_cost_discriminators: Optional[List[str]] = None,
    assumptions_version: str = "",
) -> Path:
    root = Path(root)
    (root / "lessons").mkdir(parents=True, exist_ok=True)
    row = {
        "bid": bid,
        "agency": meta.get("agency", ""),
        "vehicle": meta.get("vehicle", ""),
        "solicitation": meta.get("solicitation", ""),
        "fte_base": str(result.fte_base),
        "fte_optional": str(result.fte_optional),
        "wrap_rates": json.dumps(wraps, sort_keys=True),
        "matrix_total": str(round_cents(result.matrix_total)),
        "tep": str(round_cents(result.tep)),
        "basis": meta.get("basis", ""),
        "lessons": f"lessons/{bid}.md",
    }
    if outcome:
        row.update(
            {
                "offerors": "" if outcome.offerors is None else str(outcome.offerors),
                "outcome": outcome.outcome,
                "technical_rating": outcome.technical_rating,
                "winning_price": outcome.winning_price,
                "competitor_prices": json.dumps(outcome.competitor_prices, sort_keys=True),
            }
        )
    upsert_index(root / "bids-index.csv", row)

    # per-site rate card (bid rates become the contract rate card, TOEP IV.2.1.6)
    card = []
    for lp in result.lines:
        if lp.period == "base":
            card.append((lp.line.place, lp.line.labor_category, lp.rate))
    lines = [f"# {bid}", "", "## Sourced facts", ""]
    lines += [f"- {k}: {v}" for k, v in meta.items()]
    lines += [
        f"- matrix total: {round_cents(result.matrix_total)}",
        f"- total evaluated price: {round_cents(result.tep)}",
        f"- FTE: {result.fte_base} base + {result.fte_optional} optional",
        f"- assumptions version: {assumptions_version or 'n/a'}",
        f"- wraps: {json.dumps(wraps, sort_keys=True)}",
        "",
        "## Evaluation (verbatim from the government)",
        "",
    ]
    if outcome:
        lines += [f"- outcome: {outcome.outcome}", f"- technical rating: {outcome.technical_rating}", f"- offerors: {outcome.offerors}"]
        lines += ["", "### Strengths", ""] + [f"> {s}" for s in outcome.strengths]
        lines += ["", "### Weaknesses", ""] + ([f"> {w}" for w in outcome.weaknesses] or ["> (none)"])
        if outcome.evaluator_comments:
            lines += ["", "### Evaluator comments", ""] + [f"> {c}" for c in outcome.evaluator_comments]
    else:
        lines += ["(pending)"]
    lines += ["", "## Rate card (base period, per site)", "", "| Place | Labor category | Rate |", "|---|---|---|"]
    lines += [f"| {p} | {c} | {r} |" for p, c, r in card]
    lines += ["", "## Zero-cost discriminators", ""] + [f"- {z}" for z in (zero_cost_discriminators or ["(none recorded)"])]
    lines += ["", "## Judgements (kept separate from the facts above)", ""] + [f"- {j}" for j in (outcome.judgements if outcome else ["(pending debrief)"])]
    lines += ["", "## Ask in the next debrief", ""] + [f"- {q}" for q in (outcome.debrief_questions if outcome and outcome.debrief_questions else ["Which strengths drove the rating? What would have changed it?"])]
    out = root / "lessons" / f"{bid}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
