"""The whole workflow on synthetic files, through the command line."""
import shutil
from pathlib import Path

import openpyxl
import pytest
import yaml

from patriot_engine import assumptions as A
from patriot_engine import cli

from .synthetic import CFG, make_te4_docx, make_template, toy_te4

EXAMPLE = Path(__file__).parents[1] / "cases" / "example"


@pytest.fixture
def bid(tmp_path):
    case = tmp_path / "toycase"
    case.mkdir()
    (case / "case.yaml").write_text(yaml.safe_dump(CFG))
    shutil.copy(EXAMPLE / "mapping.yaml", case / "mapping.yaml")
    shutil.copy(EXAMPLE / "levers.yaml", case / "levers.yaml")
    root = tmp_path / "bid"
    cli.main(["new", str(root), "--case", str(case)])
    inp = root / "00-inputs"
    sites_old = [dict(s) for s in toy_te4().sites]
    sites_new = [dict(s) for s in sites_old]
    sites_old[1]["rcc"], sites_old[2]["rcc"] = 2, 0  # old statement: two at Bravo, none at Charlie
    tot = {"pm": 1, "rcc": 4, "rcc_opt2": 1, "tcc": 2, "tsgli_opt1": 2, "rtl_sl": 2, "support": 2}
    make_te4_docx(inp / "pws_old.docx", sites_old, total_row=tot)
    make_te4_docx(inp / "pws_new.docx", sites_new, total_row=tot)
    make_template(inp / "template.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SCA VA Functional LCAT", "SCLS LCAT", "Installation", "City, State", "County", "Wage Determination", "WD Revision No.", "Wage Rate", "Holidays", "H&W"])
    for s, w in (("Alpha AFB", 40), ("Bravo AFB", 42), ("Charlie AFB", 38.5)):
        ws.append(["Recovery Care Coordinator (RCC)", "Advocate", s, "", "", "2014-0042", 39, w, 11, 4.93])
    ws.append([])
    ws.append(["OCONUS VA LCAT", "Location", "FTE", "Priced Salary", "Wage Rate"])
    ws.append(["RCC", "Tokyo AB", 1, 90000, 43.27])
    ws.append([])
    ws.append(["Exempt LCAT", "Location", "FTE", "Priced Salary"])
    for n, f, s in (("Program Manager", 1, 100000), ("Team Lead", 1, 95000), ("Support Lead", 1, 95000), ("Support Coordinator", 2, 60000)):
        ws.append([n, "Alpha AFB", f, s])
    wb.save(inp / "bid_salaries.xlsx")
    (root / "bid.yaml").write_text(
        yaml.safe_dump(
            {
                "case": str(case),
                "inputs": {"pws": "00-inputs/pws_new.docx", "pws_prior": "00-inputs/pws_old.docx", "bid_salaries": "00-inputs/bid_salaries.xlsx", "template": "00-inputs/template.xlsx", "qa": "", "prior_bid_matrix": ""},
                "offeror": {"name": "Toy Co, LLC", "short": "Toy Co", "cage": "TOY01", "version": "Initial", "date": "1.1.2026"},
            }
        )
    )
    return root


def test_full_workflow(bid, capsys):
    cli.main(["intake", str(bid), "--archive"])
    assert (bid / "10-working" / "register.csv").exists() and any((bid / "_archive").iterdir())

    # a state-burden default is required; the engine will not silently price without one
    cli.main(["init", str(bid), "--default-burden", "0.09"])
    ap = bid / "10-working" / "assumptions.csv"
    assert A.validate(ap) == []

    # nothing prices before sign-off, and an edit after sign-off un-signs it
    with pytest.raises(SystemExit):
        cli.main(["price", str(bid)])
    with pytest.raises(SystemExit):
        cli.main(["export", str(bid), "--scenario", "Base"])
    cli.main(["approve", str(bid), "--by", "Pricing Manager", "--phase", "2"])
    cli.main(["price", str(bid), "--target", "9000000"])
    assert "Price to Win" in (bid / "10-working" / "review_pack.md").read_text()

    cli.main(["diff", str(bid)])
    change = (bid / "10-working" / "change_list.md").read_text()
    assert "Bravo AFB" in change and "Rule 5" in change

    cli.main(["questions", str(bid), "--as-of", "current"])
    assert "government" in (bid / "10-working" / "questions.md").read_text()

    cli.main(["export", str(bid), "--scenario", "Base"])
    outs = sorted(p.name for p in (bid / "20-outputs").iterdir())
    assert len(outs) == 2 and outs[0].endswith(".xlsx") and "VolIII" in outs[1]
    assert "Base_v1_" in outs[0]  # scenario + version in the file name

    ap.write_text(ap.read_text().replace("0.02,0.015,0.04", "0.02,0.015,0.05", 1))
    with pytest.raises(SystemExit):
        cli.main(["export", str(bid), "--scenario", "Base"])  # file changed after approval

    log = (bid / "_log" / "runs.jsonl").read_text().splitlines()
    assert any('"event": "export"' in l for l in log)


def test_export_without_libreoffice_warns_but_succeeds_unless_strict(bid, monkeypatch, capsys):
    from patriot_engine import export as X

    cli.main(["init", str(bid), "--default-burden", "0.09"])
    cli.main(["approve", str(bid), "--by", "Pricing Manager"])
    monkeypatch.setattr(X.shutil, "which", lambda *_: None)  # simulate a machine with no LibreOffice
    cli.main(["export", str(bid), "--scenario", "Base"])  # must not raise
    out = capsys.readouterr().out
    assert "WARNING" in out and "NOT CONFIRMED" in out
    with pytest.raises(SystemExit):
        cli.main(["export", str(bid), "--scenario", "Base", "--require-recalc"])


def test_init_is_repeatable(bid):
    cli.main(["init", str(bid), "--default-burden", "0.09"])
    first = (bid / "10-working" / "assumptions.csv").read_text()
    cli.main(["init", str(bid), "--default-burden", "0.09"])
    assert (bid / "10-working" / "assumptions.csv").read_text() == first


def test_record_outcome_into_second_brain(bid, tmp_path):
    cli.main(["init", str(bid), "--default-burden", "0.09"])
    cli.main(["approve", str(bid), "--by", "Pricing Manager"])
    brain = tmp_path / "brain"
    cli.main(["record", str(bid), "--outcome", "Won", "--rating", "Good", "--strength", "PM credentials", "--offerors", "6", "--brain", str(brain)])
    assert (brain / "bids-index.csv").exists() and (brain / "lessons" / "TOY.md").exists()
