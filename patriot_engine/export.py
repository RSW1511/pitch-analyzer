"""M8 outputs: fill the government's own template, generate Vol III, lock-check it.

Principles (Game Plan sections 7 and 9):
* The submission workbook is a *fresh copy of the government's blank template*, filled
  with one scenario's values. Never a cut-down copy of the working model: deleting tabs
  breaks links and can leak hidden company data.
* Python writes every number. Nothing is typed by hand, nothing is edited by an AI.
* The template is not re-formatted: only input cells change (TOEP III.3.3).
* External links, author metadata and hyperlinks are stripped (Rule 17).
* After writing, ``lock_check`` proves the file matches the blank template everywhere
  except the input cells, and ``recalc_totals`` recalculates the formulas in an
  independent engine (LibreOffice) so the workbook total can be matched to Python's
  to the penny.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import openpyxl
from openpyxl.utils import column_index_from_string as ci, get_column_letter as gl
from openpyxl.cell.cell import MergedCell
from openpyxl.worksheet.cell_range import CellRange

from .pricing.model import EXEMPT, SCA, TASK_LABOR, TASK_OPT1, TASK_OPT2, Case, Line, LinePrice, PriceResult, Scenario, fte_for, sca_breakout
from .pricing.money import D, round_cents

PERIODS = ("base", "oy1", "oy2")


@dataclass
class ExportReport:
    path: str
    scenario: str
    assumptions_version: str
    rows: Dict[str, int] = field(default_factory=dict)  # sheet -> data rows written
    total_rows: Dict[str, int] = field(default_factory=dict)
    scls_rows: Dict[str, int] = field(default_factory=dict)  # line key -> SCLS row
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Row growth (Rule 18: the template's SUM ranges are fixed at 14 rows)
# ---------------------------------------------------------------------------

def _grow(ws, total_row: int, slots: int, needed: int, last_data_row: int, merge_from: str, merge_to: str) -> int:
    """Make room for ``needed`` data rows above the totals row. Returns the new totals row."""
    extra = needed - slots
    if extra <= 0:
        return total_row
    merges = [CellRange(str(r)) for r in ws.merged_cells.ranges if r.min_row >= total_row]
    for r in merges:
        ws.unmerge_cells(str(r))
    heights = {r: d.height for r, d in ws.row_dimensions.items() if r >= total_row and d.height is not None}
    ws.insert_rows(total_row, extra)
    for r in merges:
        r.shift(row_shift=extra)
        ws.merge_cells(str(r))
    for r in heights:
        ws.row_dimensions[r].height = None
    for r, h in heights.items():
        ws.row_dimensions[r + extra].height = h
    ncols = ws.max_column
    for r in range(total_row, total_row + extra):
        for c in range(1, ncols + 1):
            ws.cell(r, c)._style = copy.copy(ws.cell(last_data_row, c)._style)
        ws.merge_cells(f"{merge_from}{r}:{merge_to}{r}")
    for dv in ws.data_validations.dataValidation:
        new = []
        for rng in str(dv.sqref).split():
            cr = CellRange(rng)
            if cr.min_row >= 17 and cr.max_row == last_data_row:
                cr.expand(down=extra)
            new.append(cr.coord)
        dv.sqref = openpyxl.worksheet.cell_range.MultiCellRange(" ".join(new))
    return total_row + extra


def _style_from(ws, src_row: int, dst_row: int):
    for c in range(1, ws.max_column + 1):
        ws.cell(dst_row, c)._style = copy.copy(ws.cell(src_row, c)._style)


def _order(lines: List[Line]) -> List[Line]:
    def key(l: Line):
        if l.classification == EXEMPT and l.state != "OCONUS":
            g = 0
        elif l.state == "OCONUS":
            g = 1
        elif l.role == "tcc":
            g = 2
        else:
            g = 3
        return g

    return sorted(lines, key=key)  # stable: keeps TE4 order inside a group


# ---------------------------------------------------------------------------
def export_submission(
    case: Case,
    result: PriceResult,
    scenario: Scenario,
    template_path: str | Path,
    mapping: dict,
    out_path: str | Path,
    offeror: Dict[str, str],
    pricing_assumptions: Optional[List[str]] = None,
    assumptions_version: str = "",
) -> ExportReport:
    wb = openpyxl.load_workbook(str(template_path), keep_links=False)
    rep = ExportReport(str(out_path), scenario.name, assumptions_version)

    lm, om, sm, summ = mapping["labor"], mapping["optional"], mapping["scls"], mapping["summary"]
    prime = offeror.get("short") or offeror["name"].split(",")[0]

    # --- SCLS Breakout first, so labor rates can link to it -----------------------
    ws_s = wb[sm["sheet"]]
    sc = {k: ci(v) for k, v in sm["columns"].items()}
    sca_lines = [l for l in _order(case.lines) if l.classification == SCA]
    first = sm["first_row"]
    for i, l in enumerate(sca_lines):
        r = first + i
        _style_from(ws_s, sm["example_row"], r)
        e = sca_breakout(l, case, scenario, None)
        # keys of repeated lines (a role with FTE > 1) share one breakout row
        rep.scls_rows[l.key] = r
        label = "Transition Case Coordinator (TCC)" if l.role == "tcc" else "Recovery Care Coordinator (RCC)"
        vals = {
            "category": label,
            "place": l.place,
            "site": "Government",
            "wd_number": l.wd_number,
            "wd_rev": int(l.wd_rev) if str(l.wd_rev).isdigit() else l.wd_rev,
            "occupation": l.occupation or "Advocate",
            "occ_code": "Not Set",
            "hours_total": int(e["hours_total"]),
            "vacation_h": int(e["vac_h"]),
            "sick_h": int(e["sick_h"]),
            "holiday_h": int(e["hol_h"]),
            "wage": float(l.wage),
            "hw_rate": float(l.hw),
            "indirects": float(e["indirects"]),
        }
        for k, v in vals.items():
            ws_s.cell(r, sc[k]).value = v
        ws_s.cell(r, sc["productive_h"]).value = f"=H{r}-I{r}-J{r}-K{r}"
        ws_s.cell(r, sc["total_wages"]).value = f"=M{r}*L{r}"
        ws_s.cell(r, sc["vacation"]).value = f"=I{r}*M{r}"
        ws_s.cell(r, sc["sick"]).value = f"=J{r}*M{r}"
        ws_s.cell(r, sc["holiday"]).value = f"=K{r}*M{r}"
        ws_s.cell(r, sc["hw"]).value = f"=H{r}*R{r}"
        ws_s.cell(r, sc["rate"]).value = f"=ROUND((N{r}+O{r}+P{r}+Q{r}+S{r}+T{r})/L{r},2)"
    rep.rows[sm["sheet"]] = len(sca_lines)

    # --- Labor tabs --------------------------------------------------------------
    lc = {k: ci(v) if isinstance(v, str) and k != "merge_to" else v for k, v in lm["columns"].items()}
    by_period: Dict[str, List[LinePrice]] = {p: [] for p in PERIODS}
    for lp in result.lines:
        by_period[lp.period].append(lp)
    for period, sheet in lm["sheets"].items():
        ws = wb[sheet]
        rows = [lp for lp in by_period[period] if lp.line.task == TASK_LABOR]
        ordered = _order([lp.line for lp in rows])
        lp_by_key = {lp.line.key: lp for lp in rows}
        total_row = _find_total(ws, "Labor Sub-Total", lm["first_row"])
        total_row = _grow(ws, total_row, lm["template_slots"], len(ordered), lm["first_row"] + lm["template_slots"] - 1, "B", lm["columns"]["merge_to"])
        for i, l in enumerate(ordered):
            r = lm["first_row"] + i
            lp = lp_by_key[l.key]
            _write_labor_row(ws, r, l, lc, prime)
            ws.cell(r, lc["fte"]).value = float(_fte(lp, scenario, case))
            ws.cell(r, lc["hours"]).value = float(lp.hours)
            if l.classification == SCA:
                ws.cell(r, lc["rate"]).value = f"='{sm['sheet']}'!U{rep.scls_rows[l.key]}"
            else:
                ws.cell(r, lc["rate"]).value = float(lp.rate)
            ws.cell(r, lc["ext"]).value = f"=+N{r}*P{r}"
        first_r, last_r = lm["first_row"], lm["first_row"] + max(len(ordered), lm["template_slots"]) - 1
        ws.cell(total_row, ci("N")).value = f"=SUM(N{first_r}:N{last_r})"
        ws.cell(total_row, ci("Q")).value = f"=SUM(Q{first_r}:Q{last_r})"
        ws.cell(total_row, ci("M")).value = f"=SUM(M{first_r}:M{last_r})"  # the template never totals FTEs (Rule 18)
        ws.cell(total_row, ci("M"))._style = copy.copy(ws.cell(total_row, ci("N"))._style)
        rep.rows[sheet] = len(ordered)
        rep.total_rows[sheet] = total_row

    # --- Optional-task tabs --------------------------------------------------------
    oc = {k: ci(v) for k, v in om["columns"].items() if k != "merge_to"}
    for task, sheet in om["sheets"].items():
        ws = wb[sheet]
        lines = _order([l for l in case.lines if l.task == task])
        total_row = _find_total(ws, "Total", om["first_row"])
        total_row = _grow(ws, total_row, om["template_slots"], len(lines), om["first_row"] + om["template_slots"] - 1, "B", om["columns"]["merge_to"])
        for i, l in enumerate(lines):
            r = om["first_row"] + i
            _write_labor_row(ws, r, l, oc, prime)
            for p in PERIODS:
                lp = next(x for x in result.lines if x.line.key == l.key and x.period == p)
                cols = {k: ci(v) for k, v in om["period_columns"][p].items()}
                ws.cell(r, cols["hours"]).value = float(lp.hours)
                ws.cell(r, cols["unit"]).value = "Hr."
                if l.classification == SCA:
                    ws.cell(r, cols["rate"]).value = f"='{sm['sheet']}'!U{rep.scls_rows[l.key]}"
                else:
                    ws.cell(r, cols["rate"]).value = float(lp.rate)
                h, rt, ex = gl(cols["hours"]), gl(cols["rate"]), gl(cols["ext"])
                ws.cell(r, cols["ext"]).value = f"=+{h}{r}*{rt}{r}"
            ws.cell(r, oc["fte"]).value = float(fte_for(l, scenario))
        a, b = om["first_row"], om["first_row"] + max(len(lines), om["template_slots"]) - 1
        for p in PERIODS:
            cols = om["period_columns"][p]
            for k in ("hours", "ext"):
                col = cols[k]
                ws[f"{col}{total_row}"].value = f"=SUM({col}{a}:{col}{b})"
        ws[f"M{total_row}"].value = f"=SUM(M{a}:M{b})"
        ws[f"M{total_row}"]._style = copy.copy(ws[f"N{total_row}"]._style)
        rep.rows[sheet] = len(lines)
        rep.total_rows[sheet] = total_row

    # --- SUMMARY ---------------------------------------------------------------------
    ws = wb[summ["sheet"]]
    ws[summ["offeror_name"]].value = offeror["name"]
    ws[summ["cage_code"]].value = offeror["cage"]
    ws[summ["version"]].value = offeror.get("version", "Initial")
    ws[summ["date"]].value = offeror["date"]
    cells = summ["clin_cells"]
    for p in PERIODS:
        lab = lm["sheets"][p]
        ws[cells[p]["labor"]].value = f"='{lab}'!Q{rep.total_rows[lab]}"
        for t, sheet in om["sheets"].items():
            col = om["period_columns"][p]["ext"]
            ws[cells[p][t]].value = f"=+'{sheet}'!{col}{rep.total_rows[sheet]}"
        for k, key in (("odc", "odc"), ("odc_opt", "odc_optional")):
            want = getattr(case, key)[p] if key == "odc" else case.odc_optional[p]
            have = ws[cells[p][k]].value
            if have is None or D(have) != D(want):
                rep.notes.append(f"{cells[p][k]}: government amount in template ({have}) differs from case ({want}); template left as issued")

    # --- Pricing assumptions ------------------------------------------------------------
    am = mapping["assumptions"]
    wa = wb[am["sheet"]]
    for i, text in enumerate(pricing_assumptions or []):
        wa[f"{am['column']}{am['first_row'] + i}"].value = text

    scrub_workbook(wb, offeror["name"])
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))
    return rep


def _fte(lp: LinePrice, s: Scenario, case: Case) -> Decimal:
    return fte_for(lp.line, s)


def _find_total(ws, label: str, start: int) -> int:
    for r in range(start, ws.max_row + 1):
        v = ws.cell(r, 1).value
        if isinstance(v, str) and v.strip().startswith(label):
            return r
    raise ValueError(f"{ws.title}: totals row '{label}' not found")


def _write_labor_row(ws, r: int, l: Line, cols: Dict[str, int], prime: str):
    ws.cell(r, cols["id"]).value = _num_or_text(l.lcat_id)
    ws.cell(r, cols["category"]).value = l.labor_category
    ws.cell(r, cols["place"]).value = l.place
    ws.cell(r, cols["prime"]).value = prime
    ws.cell(r, cols["pws"]).value = l.pws_ref
    ws.cell(r, cols["site"]).value = "Government"
    ws.cell(r, cols["scls"]).value = "X" if l.classification == SCA else None
    if "unit" in cols:
        ws.cell(r, cols["unit"]).value = "Hr."


def _num_or_text(v: str):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v or None


def scrub_workbook(wb, company: str):
    """Rule 17: strip author metadata, hyperlinks, comments, custom properties."""
    wb.properties.creator = company
    wb.properties.lastModifiedBy = company
    wb.properties.title = None
    wb.properties.subject = None
    wb.properties.description = None
    wb.properties.keywords = None
    wb.properties.category = None
    try:
        wb.custom_doc_props.props.clear()
    except Exception:
        pass
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.hyperlink is not None:
                    c.hyperlink = None
                if c.comment is not None:
                    c.comment = None


# ---------------------------------------------------------------------------
# Vol III
# ---------------------------------------------------------------------------

def make_vol3(vol2_path: str | Path, mapping: dict, out_path: str | Path, company: str) -> List[str]:
    """Blank every configured pricing cell in a copy of Vol II. Returns the cells blanked."""
    wb = openpyxl.load_workbook(str(vol2_path), keep_links=False)
    blank = mapping["vol3_blank"]
    lm, om, sm, summ = mapping["labor"], mapping["optional"], mapping["scls"], mapping["summary"]
    done: List[str] = []

    def clear(ws, coord):
        cell = ws[coord]
        if isinstance(cell, MergedCell):
            return  # the visible value lives in the merge's top-left cell
        cell.value = None
        done.append(f"{ws.title}!{coord}")

    for sheet in lm["sheets"].values():
        ws = wb[sheet]
        tr = _find_total(ws, "Labor Sub-Total", lm["first_row"])
        for r in range(lm["first_row"], tr + 1):
            for k in blank["labor_columns"]:
                clear(ws, f"{lm['columns'][k]}{r}")
    for sheet in om["sheets"].values():
        ws = wb[sheet]
        tr = _find_total(ws, "Total", om["first_row"])
        for r in range(om["first_row"], tr + 1):
            for p, cols in om["period_columns"].items():
                for k in blank["optional_columns"]:
                    clear(ws, f"{cols[k]}{r}")
        for p, cols in om["period_columns"].items():
            clear(ws, f"{cols['ext']}{tr}")
        # monthly price rows
        for r in range(tr + 1, tr + 3):
            for p, cols in om["period_columns"].items():
                if ws[f"{cols['ext']}{r}"].value is not None:
                    clear(ws, f"{cols['ext']}{r}")
    ws = wb[sm["sheet"]]
    for r in range(sm["first_row"], ws.max_row + 1):
        if ws.cell(r, 1).value:
            for k in blank["scls_columns"]:
                clear(ws, f"{sm['columns'][k]}{r}")
    ws = wb[summ["sheet"]]
    for c in blank["summary_cells"]:
        clear(ws, c)
    # labor subtotal cells that echo pricing
    for sheet in lm["sheets"].values():
        ws = wb[sheet]
        tr = _find_total(ws, "Labor Sub-Total", lm["first_row"])
        clear(ws, f"Q{tr}")
    scrub_workbook(wb, company)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))
    return done


def vol3_leaks(vol3_path: str | Path, result: PriceResult) -> List[str]:
    """Any rate or extended price from the result still present as a number in Vol III."""
    secret = {round(float(lp.rate), 2) for lp in result.lines if lp.rate} | {
        round(float(lp.extended), 2) for lp in result.lines
    }
    wb = openpyxl.load_workbook(str(vol3_path), data_only=False)
    leaks = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, (int, float)) and not isinstance(c.value, bool) and round(float(c.value), 2) in secret and c.value != 0:
                    # hours and FTE can coincide with a rate by accident; only flag non-hour columns
                    leaks.append(f"{ws.title}!{c.coordinate}={c.value}")
    return leaks


# ---------------------------------------------------------------------------
# Hygiene and lock checks
# ---------------------------------------------------------------------------

def hygiene_check(path: str | Path, forbidden: Tuple[str, ...] = ()) -> List[str]:
    """Rule 17: links to other bids' workbooks, author names, legacy file names."""
    problems = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        ext = [n for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml")]
        if ext:
            problems.append(f"{len(ext)} external link part(s): {ext[:3]}")
        core = z.read("docProps/core.xml").decode("utf8", "ignore") if "docProps/core.xml" in names else ""
        for m in re.finditer(r"<(?:dc:creator|cp:lastModifiedBy)>([^<]*)<", core):
            if "student" in m.group(1).lower() or any(f.lower() in m.group(1).lower() for f in forbidden):
                problems.append(f"author metadata: {m.group(1)}")
        if "docProps/custom.xml" in names:
            problems.append("custom document properties present")
        for n in names:
            if n.startswith("xl/worksheets/_rels/") or n.startswith("xl/comments"):
                data = z.read(n).decode("utf8", "ignore")
                if "hyperlink" in data.lower() or n.startswith("xl/comments"):
                    problems.append(f"hyperlinks/comments in {n}")
        wbx = z.read("xl/workbook.xml").decode("utf8", "ignore")
        if re.search(r"<externalReferences>", wbx):
            problems.append("workbook still declares external references")
    return problems


def template_traps(template_path: str | Path, mapping: dict) -> List[str]:
    """Rule 18: report the template's known traps before anyone relies on it."""
    out = []
    wb = openpyxl.load_workbook(str(template_path), keep_links=True)
    ws = wb[mapping["summary"]["sheet"]]
    seen: Dict[str, int] = {}
    for r in range(1, ws.max_row + 1):
        v = ws.cell(r, 1).value
        if isinstance(v, str) and v.startswith("CLIN "):
            seen[v] = seen.get(v, 0) + 1
    dup = [k for k, n in seen.items() if n > 1]
    if dup:
        out.append(f"duplicate CLIN numbers on SUMMARY: {dup}")
    lm = mapping["labor"]
    for sheet in lm["sheets"].values():
        w = wb[sheet]
        tr = _find_total(w, "Labor Sub-Total", lm["first_row"])
        f = str(w.cell(tr, ci("Q")).value)
        m = re.search(r"SUM\(Q(\d+):Q(\d+)\)", f)
        if m and int(m.group(2)) - int(m.group(1)) + 1 == lm["template_slots"]:
            out.append(f"{sheet}: SUM range fixed at {lm['template_slots']} rows ({f})")
        if w.cell(tr, ci("M")).value is None:
            out.append(f"{sheet}: FTEs are never totalled")
    hard = [c for p in mapping["summary"]["clin_cells"].values() for k, c in p.items() if k in ("odc", "odc_opt") and not str(ws[c].value).startswith("=")]
    if hard:
        out.append(f"hard-coded government amounts on SUMMARY: {hard}")
    if wb._external_links:
        out.append(f"{len(wb._external_links)} external link(s) already in the blank template")
    return out


def _cell_sig(c):
    return (c.value, c.number_format, c.font.b, c.fill.fgColor.rgb if c.fill and c.fill.fgColor else None, c.alignment.horizontal)


def lock_check(template_path: str | Path, out_path: str | Path, mapping: dict, report: ExportReport) -> List[str]:
    """Output must equal the blank template everywhere except the input cells."""
    t = openpyxl.load_workbook(str(template_path), keep_links=False)
    o = openpyxl.load_workbook(str(out_path), keep_links=False)
    diffs: List[str] = []
    if t.sheetnames != o.sheetnames:
        return [f"sheet list differs: {t.sheetnames} vs {o.sheetnames}"]
    lm, om, sm = mapping["labor"], mapping["optional"], mapping["scls"]
    summ = mapping["summary"]
    allow_summary = {summ["offeror_name"], summ["cage_code"], summ["version"], summ["date"]}
    for p in summ["clin_cells"].values():
        allow_summary |= {c for k, c in p.items() if k not in ("odc", "odc_opt", "total")}
    for ws_t in t.worksheets:
        ws_o = o[ws_t.title]
        name = ws_t.title
        shift = 0
        first_input, tot = None, None
        if name in lm["sheets"].values():
            first_input, tot = lm["first_row"], _find_total(ws_t, "Labor Sub-Total", lm["first_row"])
            shift = report.total_rows[name] - tot
        elif name in om["sheets"].values():
            first_input, tot = om["first_row"], _find_total(ws_t, "Total", om["first_row"])
            shift = report.total_rows[name] - tot
        for row in ws_t.iter_rows():
            for c in row:
                r_o = c.row + (shift if tot is not None and c.row >= tot else 0)
                oc = ws_o.cell(r_o, c.column)
                if name == summ["sheet"] and c.coordinate in allow_summary:
                    continue
                if name == mapping["assumptions"]["sheet"] and c.column == ci(mapping["assumptions"]["column"]) and c.row >= mapping["assumptions"]["first_row"]:
                    continue
                if name == sm["sheet"] and c.row >= sm["first_row"]:
                    continue
                if first_input is not None and first_input <= c.row < tot:
                    continue
                if tot is not None and c.row == tot:
                    continue  # totals row formulas are re-ranged by design
                if _cell_sig(c) != _cell_sig(oc):
                    diffs.append(f"{name}!{c.coordinate}: template {c.value!r} -> output {oc.value!r}")
    return diffs


# ---------------------------------------------------------------------------
# Independent recalculation (the "penny match")
# ---------------------------------------------------------------------------

def recalc_totals(xlsx_path: str | Path, mapping: dict, soffice: str = "soffice") -> Optional[Dict[str, Decimal]]:
    """Recalculate in LibreOffice and read the SUMMARY totals. None if LibreOffice is absent."""
    exe = shutil.which(soffice)
    if not exe:
        return None
    tmp = tempfile.mkdtemp(prefix="recalc_")
    try:
        profile = Path(tmp) / "profile"
        subprocess.run(
            [exe, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", tmp, str(xlsx_path)],
            check=True,
            capture_output=True,
            timeout=180,
        )
        out = Path(tmp) / Path(xlsx_path).name
        wb = openpyxl.load_workbook(str(out), data_only=True)
        ws = wb[mapping["summary"]["sheet"]]
        res: Dict[str, Decimal] = {}
        for p, cells in mapping["summary"]["clin_cells"].items():
            for k, c in cells.items():
                v = ws[c].value
                res[f"{p}.{k}"] = D(v) if v is not None else Decimal(0)
        res["grand_total"] = D(ws[mapping["summary"]["grand_total"]].value)
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def penny_match(result: PriceResult, workbook_totals: Dict[str, Decimal]) -> List[str]:
    """Compare the workbook's recalculated totals with Python's, cent by cent."""
    bad = []
    for p, ffp in result.ffp.items():
        for task, key in ((TASK_LABOR, "labor"), (TASK_OPT1, "opt1"), (TASK_OPT2, "opt2")):
            py, wb_ = round_cents(ffp[task]), round_cents(workbook_totals[f"{p}.{key}"])
            if py != wb_:
                bad.append(f"{p}.{key}: python {py} vs workbook {wb_}")
        if round_cents(result.period_total[p]) != round_cents(workbook_totals[f"{p}.total"]):
            bad.append(f"{p}.total: python {round_cents(result.period_total[p])} vs workbook {round_cents(workbook_totals[f'{p}.total'])}")
    if round_cents(result.matrix_total) != round_cents(workbook_totals["grand_total"]):
        bad.append(f"grand total: python {round_cents(result.matrix_total)} vs workbook {round_cents(workbook_totals['grand_total'])}")
    return bad
