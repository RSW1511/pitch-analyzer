from decimal import Decimal

from patriot_engine import checks as C
from patriot_engine.pricing.model import price_case
from patriot_engine.pricing.money import D
from patriot_engine.questions import build_questions

from .synthetic import CFG, toy_case, toy_inputs, toy_scenario, toy_te4


def _ctx(**kw):
    case, _, te4 = toy_case()
    s = toy_scenario()
    return C.Context(case, s, price_case(case, s), te4=te4, **kw)


def test_base_optional_split_checked_against_te4():
    f = C.rule6_base_vs_optional(_ctx())
    assert f and f[0].severity == C.INFO
    ctx = _ctx()
    ctx.te4.declared_total["support"] = 5  # TE4 says 5 support, case has 2
    assert C.rule6_base_vs_optional(ctx)[0].severity == C.REVIEW


def test_conflicting_table_and_text_priced_to_table_with_dollar_impact():
    rows = [{"key": "fte.rtl", "status": "Conflict flagged", "rationale": "4 vs 5 leads", "source": "TE4"}]
    f = C.rule2_conflicts_priced_to_table(_ctx(assumptions=rows))
    assert f and f[0].impact > 0 and "Priced to the table" in f[0].detail


def test_exempt_hours_basis_mismatch_has_a_dollar_figure():
    f = C.rule9_hours_vs_rate_basis(_ctx())
    assert f and f[0].impact > 0 and "2080" in f[0].title and "1912" in f[0].title


def test_second_wage_determination_in_package_but_unused_is_flagged():
    reg = [{"name": "Attachment_0011_WD_2015_9999_Rev27.pdf", "path": "Step 4"}, {"name": "WD_2014_0042_rev_39.pdf", "path": "Step 2B"}]
    f = C.rule7_wage_determinations(_ctx(register=reg))
    msgs = " ".join(x.title for x in f)
    assert "2015-9999" in msgs and "2014-0042" not in msgs


def test_rate_below_wage_floor_blocks_export():
    ctx = _ctx()
    lp = next(x for x in ctx.result.lines if x.line.classification == "SCA" and x.period == "base")
    lp.rate = D("1.00")
    f = C.rule15_price_every_site(ctx)
    assert f and f[0].severity == C.BLOCK and C.has_blockers(f)


def test_overseas_and_state_load_and_zero_cost_discriminator_surface():
    ctx = _ctx()
    assert C.rule11_overseas(ctx)
    assert C.rule16_zero_cost_discriminators(ctx)[0].severity == C.INFO
    f = C.rule13_annualise(ctx)
    assert f and "annualised" in f[0].detail


def test_tep_rule_reports_the_extension_gap():
    f = C.rule12_tep_basis(_ctx())
    assert f[0].impact > 0


def test_run_checks_orders_blockers_first_then_dollars():
    out = C.run_checks(_ctx())
    sev = [f.severity for f in out]
    assert sev == sorted(sev, key=lambda s: {"BLOCK": 0, "REVIEW": 1, "INFO": 2}[s])


# --- questions ---
def test_questions_rank_by_price_impact_and_split_audiences():
    case, _, te4 = toy_case()
    rows = [{"key": "esc.exempt.oy1", "status": "Conflict flagged", "confidence": "Low", "description": "Escalation", "rationale": "0.5% vs 1.4% (open question)", "source": "v"}]
    qs = build_questions(case, toy_scenario(), assumptions=rows)
    gov = [q for q in qs if q.audience == "government"]
    assert gov == sorted(gov, key=lambda q: -(q.impact or 0))
    assert any("no assigned location" in q.text for q in gov)  # Optional Task 2 RCC
    assert any("OCONUS" in q.text for q in gov)
    assert qs[-1].audience == "internal" and qs[-1].impact is None
    assert [q.rank for q in qs] == list(range(1, len(qs) + 1))


def test_te4_te5_disagreement_becomes_a_priced_question():
    case, _, te4 = toy_case()
    te5 = ["Alpha AFB", "Bravo AFB", "Delta AFB", "Tokyo AB"]  # Charlie missing, Delta appears
    inputs = toy_inputs()
    inputs.sca.append({**inputs.sca[0], "site": "Delta AFB", "wage": "50.00"})
    case2 = case
    case2.state_burden["TX"] = D("0.09")
    qs = build_questions(case2, toy_scenario(), te4=te4, te5_places=te5, bid=inputs, roles=CFG["roles"])
    q = next(q for q in qs if "Technical Exhibits 4 and 5" in q.text)
    assert "Charlie AFB" in q.text and "Delta AFB" in q.text and q.impact and q.impact > 0


def test_questions_are_deduplicated_against_published_qa_and_patriot():
    case, _, _ = toy_case()
    qa = [{"number": "31", "question": "Optional Task 2 adds a Counselor Advocate with no assigned location; can the Government give a planning location?", "answer": ""}]
    qs = build_questions(case, toy_scenario(), qa_rows=qa)
    q = next(q for q in qs if "no assigned location" in q.text)
    assert q.status == "already asked (Q&A #31)"
    qs2 = build_questions(case, toy_scenario(), patriot_asked=["Which OCONUS wage determination or allowance schedule applies to the OCONUS places of performance?"])
    assert next(q for q in qs2 if "OCONUS" in q.text).status == "asked by us"
