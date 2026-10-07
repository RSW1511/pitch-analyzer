"""M3 differ: Word files by heading and table cell, never PDF text (Game Plan rule 3).

A PDF compare of two statement versions can produce hundreds of flags of which a handful
matter, with whole sections merely renumbered. This differ:

* aligns sections by number *and* by normalised title, so a renumbered section is
  reported as "renumbered", not as a delete plus an add;
* compares paragraphs inside matched sections sentence by sentence;
* ranks each change by price relevance (numbers, quantities, hours, travel, quals) so
  the handful that matter come first.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import docx
from docx.oxml.ns import qn

from .build_case import norm
from .extract.te4 import COLUMNS, Te4

SECTION_RE = re.compile(r"^\s*((?:\d+\.)+\d*|\d+)\s*[\.\)]?\s+(\S.{0,160})$")
PRICE_TERMS = re.compile(
    r"\b(fte|full[- ]time|hours?|overtime|travel|reimburs\w*|position|personnel|key personnel|"
    r"program manager|lead|coordinator|education|experience|degree|years?|clearance|location|"
    r"site|installation|optional|per diem|shift|24/7|weekend|on[- ]call|telework|gfp|laptop|wage|"
    r"salary|escalat\w*|wage determination|price|cost|\$)\b",
    re.I,
)
MONTH_RE = re.compile(r"^(january|february|march|april|may|june|july|august|september|october|november|december)\b", re.I)
NUMBER_RE = re.compile(r"\$?\d[\d,]*\.?\d*%?|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b", re.I)


@dataclass
class Change:
    kind: str  # added | removed | modified | renumbered | table-cell | site
    location: str
    old: str = ""
    new: str = ""
    relevance: int = 0  # 0 = cosmetic, 1 = text, 2 = numbers or priced terms, 3 = headcount/site
    note: str = ""

    def to_dict(self):
        return asdict(self)


def _iter_blocks(d):
    body = d.element.body
    ptab = {id(t._tbl): t for t in d.tables}
    for el in body.iterchildren():
        if el.tag == qn("w:p"):
            yield "p", docx.text.paragraph.Paragraph(el, d)
        elif el.tag == qn("w:tbl"):
            yield "t", docx.table.Table(el, d)


def sections(path: str | Path) -> Tuple[Dict[str, Dict], List[docx.table.Table]]:
    """-> {section_key: {"number", "title", "paras": [...], "order": n}} and the tables."""
    d = docx.Document(str(path))
    out: Dict[str, Dict] = {}
    tables = []
    cur = {"number": "", "title": "(preamble)", "paras": [], "order": 0}
    out["(preamble)"] = cur
    n = 0
    for kind, blk in _iter_blocks(d):
        if kind == "t":
            tables.append(blk)
            continue
        text = re.sub(r"\s+", " ", blk.text).strip()
        if not text:
            continue
        style = (blk.style.name or "").lower() if blk.style is not None else ""
        m = SECTION_RE.match(text)
        is_heading = style.startswith("heading") or (m is not None and len(text) < 90 and not text.endswith("."))
        if is_heading and m and MONTH_RE.match(m.group(2)):
            is_heading = False  # a date line such as "16 September 2025", not a section number
        if is_heading and m:
            n += 1
            number, title = m.group(1).rstrip("."), m.group(2)
            key = f"{number}|{norm(title)}"
            cur = {"number": number, "title": title, "paras": [], "order": n}
            out[key] = cur
        else:
            cur["paras"].append(text)
    return out, tables


def _relevance(old: str, new: str) -> int:
    a, b = set(NUMBER_RE.findall(old)), set(NUMBER_RE.findall(new))
    if a != b:
        return 2
    if PRICE_TERMS.search(old) or PRICE_TERMS.search(new):
        return 1
    return 0


def _para_diff(loc: str, old: List[str], new: List[str]) -> List[Change]:
    out: List[Change] = []
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        o, n = " ".join(old[i1:i2]), " ".join(new[j1:j2])
        kind = {"replace": "modified", "delete": "removed", "insert": "added"}[tag]
        rel = _relevance(o, n) if kind == "modified" else (1 + bool(NUMBER_RE.search(o + n)))
        if kind == "modified" and norm(o) == norm(n):
            continue  # whitespace/punctuation only
        out.append(Change(kind, loc, o[:600], n[:600], rel))
    return out


def diff_documents(old_path: str | Path, new_path: str | Path) -> List[Change]:
    a, ta = sections(old_path)
    b, tb = sections(new_path)
    changes: List[Change] = []

    by_title_a = {norm(v["title"]): k for k, v in a.items() if v["number"]}
    matched_b = set()
    for kb, vb in b.items():
        if kb in a:
            matched_b.add(kb)
            changes += _para_diff(f"{vb['number']} {vb['title']}".strip(), a[kb]["paras"], vb["paras"])
            continue
        ka = by_title_a.get(norm(vb["title"]))
        if ka is not None and ka in a:
            matched_b.add(kb)
            va = a[ka]
            changes.append(
                Change("renumbered", vb["title"], va["number"], vb["number"], 0, "section renumbered, not rewritten")
            )
            changes += _para_diff(f"{vb['number']} {vb['title']}", va["paras"], vb["paras"])
            by_title_a.pop(norm(vb["title"]), None)
            a = {k: v for k, v in a.items() if k != ka}
            continue
        if vb["number"]:
            changes.append(Change("added", f"{vb['number']} {vb['title']}", "", " ".join(vb["paras"])[:600], 1 + bool(NUMBER_RE.search(" ".join(vb["paras"])))))
    for ka, va in a.items():
        if ka not in b and va["number"] and norm(va["title"]) not in {norm(v["title"]) for v in b.values()}:
            changes.append(Change("removed", f"{va['number']} {va['title']}", " ".join(va["paras"])[:600], "", 1))

    changes += diff_tables(ta, tb)
    changes.sort(key=lambda c: (-c.relevance, c.location))
    return changes


def _table_rows(t) -> List[List[str]]:
    rows = []
    for r in t.rows:
        cells, prev = [], None
        for c in r.cells:
            if c._tc is prev:
                continue
            prev = c._tc
            cells.append(re.sub(r"\s+", " ", c.text).strip())
        rows.append(cells)
    return rows


def diff_tables(ta, tb) -> List[Change]:
    """Match tables by header signature, rows by first cell, compare cell by cell."""
    out: List[Change] = []
    sig = lambda t: norm(" ".join(_table_rows(t)[0])) if t.rows else ""
    pool = {sig(t): t for t in tb}
    for t in ta:
        s = sig(t)
        other = pool.get(s)
        if other is None:
            continue
        ra, rb = _table_rows(t), _table_rows(other)
        head = ra[0]
        rowkey = lambda r, i: norm(next((c for c in r if c), "")) or f"row{i}"  # first non-empty cell
        ka = {rowkey(r, i): r for i, r in enumerate(ra[1:], 1) if r}
        kb = {rowkey(r, i): r for i, r in enumerate(rb[1:], 1) if r}
        title = (head[0] if head else "table")[:40]
        for k, r in kb.items():
            if k not in ka:
                out.append(Change("table-cell", f"{title} / {next((c for c in r if c), '')}", "", " | ".join(r), 2, "row added"))
                continue
            for ci, (x, y) in enumerate(zip(ka[k], r)):
                if x != y:
                    col = head[ci] if ci < len(head) else str(ci)
                    out.append(Change("table-cell", f"{title} / {next((c for c in r if c), '')} / {col}", x, y, 2 if NUMBER_RE.search(x + y) else 1))
        for k, r in ka.items():
            if k not in kb:
                out.append(Change("table-cell", f"{title} / {next((c for c in r if c), '')}", " | ".join(r), "", 2, "row removed"))
    return out


# ---------------------------------------------------------------------------
# TE4 site-level diff (Rule 5): totals can be unchanged while the money moves.
# ---------------------------------------------------------------------------

def _site_key(name: str) -> str:
    return norm(name)


def diff_te4(old: Te4, new: Te4) -> List[Change]:
    a = {_site_key(s["place"]): s for s in old.sites}
    b = {_site_key(s["place"]): s for s in new.sites}
    out: List[Change] = []
    for k in sorted(set(a) | set(b)):
        sa, sb = a.get(k), b.get(k)
        place = (sb or sa)["place"]
        if sa is None:
            out.append(Change("site", place, "", _summ(sb), 3, "site added to TE4"))
            continue
        if sb is None:
            out.append(Change("site", place, _summ(sa), "", 3, "site removed from TE4"))
            continue
        for c in COLUMNS:
            if sa[c] != sb[c]:
                out.append(Change("site", f"{place} / {c}", str(sa[c]), str(sb[c]), 3, f"{c} {sa[c]} -> {sb[c]}"))
    same_totals = old.declared_total == new.declared_total and old.computed_total == new.computed_total
    if out and same_totals:
        out.append(
            Change(
                "site",
                "(totals)",
                str(old.declared_total),
                str(new.declared_total),
                3,
                "headcount totals unchanged but site mix moved: re-price at site level (Rule 5)",
            )
        )
    return out


def _summ(s: dict) -> str:
    return ", ".join(f"{c}={s[c]}" for c in COLUMNS if s.get(c))
