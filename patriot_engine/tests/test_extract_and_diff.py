from pathlib import Path

import openpyxl

from patriot_engine.build_case import same_place
from patriot_engine.delta import delta_summary
from patriot_engine.differ import diff_documents, diff_te4
from patriot_engine.extract.bid_inputs import extract_bid_inputs
from patriot_engine.extract.qa import extract_qa
from patriot_engine.extract.te4 import extract_te4

from .synthetic import make_te4_docx, toy_te4, toy_case, toy_scenario, toy_inputs, CFG
from patriot_engine.build_case import build_case
from patriot_engine.pricing.money import D


def _sites(rcc_bravo=1, rcc_charlie=1):
    s = toy_te4().sites
    s = [dict(x) for x in s]
    s[1]["rcc"], s[2]["rcc"] = rcc_bravo, rcc_charlie
    return s


def test_te4_extraction_and_unlocated_position(tmp_path):
    p = make_te4_docx(tmp_path / "a.docx", toy_te4().sites, total_row={"pm": 1, "rcc": 4, "rcc_opt2": 1, "tcc": 2, "tsgli_opt1": 2, "rtl_sl": 2, "support": 2})
    t = extract_te4(p)
    assert len(t.sites) == 4
    assert t.declared_total["rcc"] == 4 and t.computed_total["rcc"] == 4
    assert t.unlocated() == {"rcc_opt2": 1}  # counted in TOTAL, no site row carries it (RCC Optional Task 2)


def test_te4_table_vs_total_disagreement_is_visible(tmp_path):
    p = make_te4_docx(tmp_path / "b.docx", toy_te4().sites, total_row={"pm": 1, "rcc": 5, "rcc_opt2": 0, "tcc": 2, "tsgli_opt1": 2, "rtl_sl": 3, "support": 2})
    t = extract_te4(p)
    assert t.unlocated()["rcc"] == 1 and t.unlocated()["rtl_sl"] == 1


def test_site_move_with_same_totals_is_reported_and_repriced(tmp_path):
    old = extract_te4(make_te4_docx(tmp_path / "old.docx", _sites(2, 0)))
    new = extract_te4(make_te4_docx(tmp_path / "new.docx", _sites(1, 1)))
    ch = diff_te4(old, new)
    assert any("Rule 5" in c.note for c in ch)  # totals unchanged, site mix moved
    inputs = toy_inputs()
    burden = {s: D("0.09") for s in ("TX", "NM", "FL", "UNK")}
    c_old, _ = build_case(CFG, old, inputs, salaries={"tsgli_adj": D(1), "tsgli_cns": D(1)}, state_burden=burden)
    c_new, _ = build_case(CFG, new, inputs, salaries={"tsgli_adj": D(1), "tsgli_cns": D(1)}, state_burden=burden)
    ds = delta_summary(c_old, c_new, toy_scenario())
    assert ds["fte_old"] == ds["fte_new"]
    assert ds["matrix_delta"] != 0  # same headcount, different money
    assert {l["place"] for l in ds["lines"]} >= {"Bravo AFB", "Charlie AFB"}


def test_differ_finds_renumbered_section_and_number_change(tmp_path):
    sites = toy_te4().sites
    a = make_te4_docx(tmp_path / "a.docx", sites, heading_text="Hours of operation are 0730 to 1700.")
    b = make_te4_docx(tmp_path / "b.docx", sites, renumber=True, heading_text="Hours of operation are 0730 to 1800.")
    ch = diff_documents(a, b)
    kinds = [c.kind for c in ch]
    assert "renumbered" in kinds  # not reported as a delete plus an add
    assert not any(c.kind in ("added", "removed") and "Hours" in c.location for c in ch)
    mod = [c for c in ch if c.kind == "modified"]
    assert mod and mod[0].relevance >= 2  # a number changed: ranked first


def test_bid_inputs_found_by_header_text_not_address(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["junk row"])
    ws.append(["SCA VA Functional LCAT", "SCLS LCAT", "Installation", "City, State", "County", "Wage Determination", "WD Revision No.", "Wage Rate", "Holidays", "H&W"])
    ws.append(["RCC", "Advocate", "Alpha AFB", "Alpha, TX", "X", "2014-0042", 39, "$42.46", 11, "$4.93"])
    ws.append([])
    ws.append(["OCONUS VA LCAT", "Location", "FTE", "Priced Salary", "Wage Rate"])
    ws.append(["RCC", "Tokyo AB", 1, "$92,206.40", "$44.33"])
    ws.append([])
    ws.append(["Exempt LCAT", "Location", "FTE", "Priced Salary"])
    ws.append(["Program Manager", "Alpha AFB", 1, 106932])
    wb.save(tmp_path / "bs.xlsx")
    b = extract_bid_inputs(tmp_path / "bs.xlsx")
    assert b.sca[0]["wage"] == "42.46" and b.sca[0]["hw"] == "4.93"
    assert b.oconus[0]["salary"] == "92206.40"
    assert b.exempt[0]["salary"] == "106932"


def test_qa_rows_parsed_across_repeated_headers(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["", "RFP Questions and Answers"])
    ws.append(["", "Question #", "PWS", "Reference location & Question", "Answer"])
    ws.append(["", 3, "1.11.4", "How many FTEs?", "Nine (9) not including Support Team Lead."])
    ws.append(["", "Question #", "TOEP", "Reference location & Question", "Answer"])
    ws.append(["", 36, "TOEP 2.1.2", "Exclude plan from page limit?", "No."])
    wb.save(tmp_path / "qa.xlsx")
    rows = extract_qa(tmp_path / "qa.xlsx")
    assert [r["number"] for r in rows] == ["3", "36"]
    assert "Nine (9)" in rows[0]["answer"]


def test_place_names_match_despite_typos():
    assert same_place("Kandena AB", "Kadena AB, JA", {"kandenaab": "kadenaabja"})
    assert same_place("JB Alpha AFB", "JB Alpha")
    assert same_place("Wright-Patterson AFB", "Wright-Pattesonafb")
    assert not same_place("Delta AFB", "Alpha AFB")
