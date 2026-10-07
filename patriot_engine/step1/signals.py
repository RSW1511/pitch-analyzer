"""Rule 4: scan letters and cover pages for signals, not just the PWS.

The RCC RFI letter said the government was "considering a 25% reduction in services"
while the draft PWS still showed 97 FTE. A line-by-line read of the PWS misses it; a
scan of the letter does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import List, Optional

PATTERNS = [
    ("reduction", re.compile(r"(?:consider\w*|anticipat\w*|expect\w*|plan\w*|propos\w*)[^.]{0,60}?(\d{1,3})\s*(?:%|percent)\s*(?:reduction|decrease|cut|increase|growth)(?:\s+in\s+(\w+(?:\s+\w+)?))?", re.I)),
    ("reduction", re.compile(r"(\d{1,3})\s*(?:%|percent)\s*(?:reduction|decrease|cut)", re.I)),
    ("reduction", re.compile(r"\b(reduc\w+|consolidat\w+|descop\w+|eliminat\w+|streamlin\w+)\b[^.]{0,80}\b(services?|positions?|staff\w*|personnel|requirement\w*)\b", re.I)),
    ("growth", re.compile(r"\b(expand\w*|increase[ds]?|additional|new requirement\w*|surge)\b[^.]{0,80}\b(services?|positions?|staff\w*|locations?|sites?)\b", re.I)),
    ("vehicle", re.compile(r"\b(small business set[- ]aside|unrestricted|full and open|sole[- ]source|8\(a\)|sdvosb|hubzone|wosb)\b", re.I)),
    ("period", re.compile(r"\b(\d{1,2})[- ]month (?:base|option|transition)|(\w+) option (?:year|period)s?\b", re.I)),
    ("timing", re.compile(r"\b(responses? (?:are )?due|comments? (?:are )?due|anticipated (?:rfp|solicitation|award)[^.]{0,40})\b[^.]{0,60}", re.I)),
]


@dataclass
class Signal:
    kind: str
    text: str
    percent: Optional[int] = None

    def to_dict(self):
        return asdict(self)


def scan(text: str) -> List[Signal]:
    text = re.sub(r"\s+", " ", text)
    out, seen = [], set()
    for kind, rx in PATTERNS:
        for m in rx.finditer(text):
            snippet = text[max(0, m.start() - 60): m.end() + 60].strip()
            pct = None
            for g in m.groups():
                if g and g.isdigit() and 0 < int(g) <= 100:
                    pct = int(g)
                    break
            key = (kind, snippet[:80])
            if key in seen:
                continue
            seen.add(key)
            out.append(Signal(kind, snippet, pct))
    return out


def fte_adjustment(signals: List[Signal], base_fte: float) -> Optional[float]:
    """Apply the largest stated reduction to a headcount (a *starting* estimate only:
    a 25% headcount cut is not a 25% price cut, see baseline.role_level_estimate)."""
    cuts = [s.percent for s in signals if s.kind == "reduction" and s.percent]
    if not cuts:
        return None
    return round(base_fte * (1 - max(cuts) / 100.0), 2)
