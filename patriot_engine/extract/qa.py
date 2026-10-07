"""M2 extractor: government Q&A workbook (Attachment 0012) -> rows.

The sheet repeats a "Question #" header for each section (General, TOEP, Cost/Price,
QASP, CDRL, MISC), and a few answers sit in the wrong column. Rows are found by their
numeric question number; columns by position after each header row.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

import openpyxl


def extract_qa(path: str | Path) -> List[Dict[str, str]]:
    wb = openpyxl.load_workbook(str(path), data_only=True)
    ws = wb.worksheets[0]
    rows: List[Dict[str, str]] = []
    section = ""
    colmap = {}
    for r in ws.iter_rows(values_only=True):
        cells = [("" if c is None else str(c).strip()) for c in r]
        if not any(cells):
            continue
        low = [c.lower() for c in cells]
        if any(c.startswith("question") and "#" in c for c in low):
            colmap = {}
            for i, c in enumerate(low):
                if c.startswith("question") and "#" in c:
                    colmap["number"] = i
                elif c.startswith("answer"):
                    colmap["answer"] = i
                elif "reference" in c or c in ("pws", "toep", "cost/price", "qasp", "cdrl", "misc"):
                    colmap.setdefault("ref", i)
                    if c in ("pws", "toep", "cost/price", "qasp", "cdrl", "misc"):
                        section = cells[i]
                elif c.startswith("question") or "question" in c:
                    colmap["question"] = i
            # header cell texts such as "TOEP" in column C sit beside "Reference location & Question"
            for i, c in enumerate(cells):
                if c in ("PWS", "TOEP", "Cost/Price", "QASP", "CDRL", "MISC"):
                    section = c
            continue
        if not colmap:
            continue
        num = cells[colmap.get("number", 0)]
        if not re.fullmatch(r"\d+(\.0)?", num or ""):
            continue
        rows.append(
            {
                "number": str(int(float(num))),
                "section": section,
                "ref": cells[colmap["ref"]] if "ref" in colmap and colmap["ref"] < len(cells) else "",
                "question": cells[colmap["question"]] if "question" in colmap and colmap["question"] < len(cells) else "",
                "answer": cells[colmap["answer"]] if "answer" in colmap and colmap["answer"] < len(cells) else "",
            }
        )
    return rows
