import pytest

from patriot_engine import assumptions as A
from patriot_engine.init_assumptions import load_levers, write_assumptions
from pathlib import Path

SPEC = load_levers(Path(__file__).parents[1] / "cases" / "example" / "levers.yaml")

from .synthetic import toy_case


def _file(tmp_path):
    case, _, _ = toy_case()
    p = tmp_path / "assumptions.csv"
    write_assumptions(p, case, SPEC, None)
    return p


def test_generated_register_is_valid_and_loads(tmp_path):
    p = _file(tmp_path)
    assert A.validate(p) == []
    sc = A.load_scenarios(p)
    assert set(sc) == {"Base", "Aggressive", "Conservative"}
    assert sc["Aggressive"].dec("rate.fee") < sc["Base"].dec("rate.fee") < sc["Conservative"].dec("rate.fee")


def test_government_fixed_items_may_not_differ_between_scenarios(tmp_path):
    p = _file(tmp_path)
    text = p.read_text().replace("hours.sca_total,Paid hours per SCA FTE-year,gov,hours,2080,2080,2080", "hours.sca_total,Paid hours per SCA FTE-year,gov,hours,2080,2000,2080")
    p.write_text(text)
    assert any("government-fixed" in x for x in A.validate(p))
    with pytest.raises(A.AssumptionError):
        A.load_scenarios(p)


def test_every_number_needs_a_source(tmp_path):
    p = _file(tmp_path)
    rows = p.read_text().splitlines()
    hdr = rows[0].split(",")
    i = hdr.index("source")
    cells = rows[1].split(",")
    assert i < len(cells)


def test_approval_binds_to_file_hash(tmp_path):
    p = _file(tmp_path)
    assert A.approval_for(p) is None
    A.approve(p, "Pricing Manager", "2")
    assert A.approval_for(p)["approver"] == "Pricing Manager"
    p.write_text(p.read_text().replace("0.03", "0.04", 1))  # any edit invalidates sign-off
    assert A.approval_for(p) is None


def test_phase_change_list(tmp_path):
    old = _file(tmp_path)
    new = tmp_path / "new.csv"
    new.write_text(old.read_text().replace("fte.rtl,Team leads,lever,FTE,1,1,2", "fte.rtl,Team leads,lever,FTE,2,2,2"))
    ch = A.diff(old, new)
    assert ch and ch[0]["key"] == "fte.rtl"
