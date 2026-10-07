from decimal import Decimal

from patriot_engine.pricing.model import Line, Period, SCA, Case, price_case, sca_breakout
from patriot_engine.pricing.money import D, round_cents, trunc_cents
from patriot_engine.pricing.scenarios import feasible_region, sensitivity, solve_lever
from patriot_engine.pricing.tep import extension_sensitivity

from .synthetic import toy_case, toy_scenario


def test_money_rounding():
    assert round_cents(D("2.675")) == D("2.68")  # Excel ROUND, not banker's
    assert trunc_cents(D("1546.6667")) == D("1546.66")
    assert D("$1,234.50") == D("1234.50")


def test_government_template_example_row_reproduces():
    """The government's own example row: wage 22.72, H&W 4.93, 96 holiday hours, indirects 26,530.25 -> $45.48."""
    line = Line("x", "Administrative Assistant", "Fort Example", "labor", SCA, D(1), wage=D("22.72"), hw=D("4.93"), holiday_hours=D(96), state="XX")
    case = Case("t", [Period("base", 12)], [line], {}, {})
    e = sca_breakout(line, case, toy_scenario(), indirect_override=D("26530.25"))
    assert e["productive"] == D(1848)
    assert e["rate"] == D("45.48")
    assert round_cents(e["wages"]) == D("41986.56")


def test_ten_month_hours_truncate_per_fte_then_multiply():
    case, _, _ = toy_case()
    r = price_case(case, toy_scenario())
    sca = next(lp for lp in r.lines if lp.line.role == "tcc" and lp.period == "base")
    assert sca.hours == D("1546.66") * 2  # 2 TCC FTE
    ex = next(lp for lp in r.lines if lp.line.role == "pm" and lp.period == "base")
    assert ex.hours == D("1593.33")


def test_sca_rate_flat_exempt_rate_escalates():
    case, _, _ = toy_case()
    r = price_case(case, toy_scenario())
    rates = lambda role: [next(lp.rate for lp in r.lines if lp.line.role == role and lp.period == p) for p in ("base", "oy1", "oy2")]
    assert len(set(rates("tcc"))) == 1  # TOEP IV.2.2.3: SCA rates are not escalated
    a, b, c = rates("pm")
    assert a < b < c


def test_tep_adds_half_of_last_year_services_not_odc():
    case, _, _ = toy_case()
    r = price_case(case, toy_scenario())
    last = sum(r.ffp["oy2"].values(), D(0))
    assert r.tep == r.matrix_total + last / 2
    sens = extension_sensitivity(case, r)
    assert sens["services_and_odc"] - sens["services_only"] == (r.odc["oy2"] + r.odc_optional["oy2"]) / 2


def test_fte_totals_and_optional_split():
    case, rep, te4 = toy_case()
    r = price_case(case, toy_scenario())
    assert r.fte_base == 11  # 1 PM + 2 leads + 2 support + 4 site RCCs + 2 TCCs
    assert r.fte_optional == 2 + 1  # TSGLI 2 + the unlocated Optional Task 2 RCC
    assert rep.unlocated == {"rcc_opt2": 1}
    assert any("highest WD wage" in a for a in rep.assumed)


def test_fee_lever_is_monotone_and_solvable():
    case, _, _ = toy_case()
    s = toy_scenario()
    base = price_case(case, s).tep
    target = base - D(2000)
    v = solve_lever(case, s, "rate.fee", target)
    assert v is not None and v < D("0.06")
    assert price_case(case, s.set("rate.fee", str(v))).tep <= target  # stays under the target
    assert price_case(case, s.set("rate.fee", str(v + D("0.00001")))).tep > target  # and is the boundary
    assert solve_lever(case, s, "rate.fee", D(1)) is None  # unreachable target


def test_sensitivity_sorted_and_signed():
    case, _, _ = toy_case()
    rows = sensitivity(case, toy_scenario(), extra={"fte.rtl": D(1)})
    assert rows == sorted(rows, key=lambda r: r["swing"], reverse=True)
    assert all(r["tep_up"] > 0 > r["tep_down"] for r in rows)


def test_feasible_region_ranked_by_fee():
    case, _, _ = toy_case()
    s = toy_scenario()
    tgt = price_case(case, s).tep
    grid = {"rate.fee": [D("0.03"), D("0.06")], "rate.overhead": [D("0.01"), D("0.02")]}
    out = feasible_region(case, s, grid, tgt)
    assert out and out[0]["rate.fee"] >= out[-1]["rate.fee"]
    assert all(o["headroom"] >= 0 for o in out)


def test_missing_burden_is_an_error_not_a_zero():
    import pytest

    case, _, _ = toy_case()
    case.state_burden.clear()
    with pytest.raises(KeyError):
        price_case(case, toy_scenario())
