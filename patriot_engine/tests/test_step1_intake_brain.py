import csv
import json
import os
from decimal import Decimal

from patriot_engine import intake as I
from patriot_engine.pricing.model import price_case
from patriot_engine.pricing.money import D, round_cents
from patriot_engine.second_brain import Outcome, record_bid
from patriot_engine.step1 import baseline, signals
from patriot_engine.step1.usaspending import UsaSpending, build_mod_ledger, classify_mod, parse_award, reconcile, summarise_ledger

from .synthetic import toy_case, toy_scenario


# --- USAspending parsing (offline; api.usaspending.gov is blocked in the build sandbox) ---
def _tx(n, date, amt, desc, kind="SUPPLEMENTAL AGREEMENT FOR WORK WITHIN SCOPE"):
    return {"modification_number": n, "action_date": date, "federal_action_obligation": amt, "description": desc, "action_type_description": kind}


def test_mod_ledger_classifies_and_runs_a_total():
    tx = [
        _tx("0", "2021-04-08", "4901000.00", "Initial award, base period"),
        _tx("P00005", "2021-09-30", "100.00", "Stop-work order"),
        _tx("P00010", "2022-04-01", "9100000.00", "Exercise of Option Year 1 (FAR 52.217-9)"),
        _tx("P00013", "2022-08-01", "250000.00", "Revised PWS and staffing matrix, FTE increase"),
        _tx("P00024", "2024-05-01", "983643.90", "Wage Determination 2014-0042 Rev 38 price adjustment under 52.222-43"),
    ]
    led = build_mod_ledger(tx)
    assert [m.kind for m in led] == ["unclassified", "admin", "option_exercise", "scope", "wage_determination"]
    assert led[-1].running_total == D("15234743.90")
    s = summarise_ledger(led)
    assert s["transactions"] == 5 and s["by_class"]["wage_determination"] == "983643.90"


def test_classifier_keeps_wage_adjustments_apart_from_escalation_and_scope():
    assert classify_mod("Equitable adjustment for new wage determination H&W") == "wage_determination"
    assert classify_mod("Change of address and POC") == "admin"


def test_award_parse_and_reconcile():
    api_award = {
        "piid": "TESTAWARD0001", "total_obligation": 12500000.50, "base_and_all_options": 12500000.50,
        "period_of_performance": {"start_date": "2021-04-08", "end_date": "2026-03-11"},
        "recipient": {"recipient_name": "EXAMPLE INCUMBENT LLC", "recipient_uei": "UEI000000001"},
        "parent_award": {"piid": "TESTIDV0001"},
        "latest_transaction_contract_data": {"number_of_offers_received": "6", "extent_competed_description": "FULL AND OPEN"},
    }
    a = parse_award("CONT_AWD_X", api_award)
    assert a.offers == 6 and a.parent_piid == "TESTIDV0001" and a.total_obligation == D("12500000.50")
    led = build_mod_ledger([_tx("0", "2021-04-08", "12430036.00", "award"), _tx("P1", "2022-01-01", "69964.50", "funding")])
    probs = reconcile(led, a, documents_in_hand=26)
    assert any("26 modification documents" in p for p in probs)
    assert any("potential value is understated" in p for p in probs)
    assert not any("ledger sums" in p for p in probs)


def test_raw_api_responses_are_saved_with_url_date_and_hash(tmp_path):
    calls = []

    def fake(method, url, body):
        calls.append((method, url))
        return {"results": [{"generated_internal_id": "G"}], "page_metadata": {"hasNext": False}}

    api = UsaSpending(http=fake, save_dir=tmp_path, pause=0)
    api.find_award("TESTAWARD0001")
    saved = list(tmp_path.glob("*.json"))
    assert len(saved) == 1
    rec = json.loads(saved[0].read_text())
    assert rec["url"].startswith("https://api.usaspending.gov") and rec["sha256"] and rec["retrieved"]


# --- signals and the first price range ---
def test_rfi_signal_found_in_letter_text():
    text = "The Government is considering a 25% reduction in services for the follow-on requirement. Responses are due 13 June."
    sig = signals.scan(text)
    assert any(s.kind == "reduction" and s.percent == 25 for s in sig)
    assert signals.fte_adjustment(sig, 97) == 72.75


def test_desk_estimate_is_labour_per_fte_times_expected_fte():
    est = baseline.desk_estimate(D("9700000.00"), D(97), D(72))
    assert round_cents(est.labor_per_fte) == D("100000.00")
    assert round_cents(est.labor_point) == D("7200000.00")
    assert est.labor_point < est.labor_high  # range widens upward for the role-mix effect


def test_a_headcount_cut_is_not_the_same_price_cut():
    old = {"coordinator": (D(43), D(120000)), "support": (D(21), D(70000)), "lead": (D(8), D(110000))}
    new = {"coordinator": D(43), "support": D(9), "lead": D(4)}
    r = baseline.role_level_estimate(old, new)
    fte_cut = 1 - (43 + 9 + 4) / D(72)
    cost_cut = 1 - r["total_new"] / sum(f * c for f, c in old.values())
    assert cost_cut < fte_cut  # keeping the expensive roles means price falls by less than headcount
    assert r["avg_cost_new"] > r["avg_cost_old"]


def test_price_range_is_on_the_tep_basis():
    out = baseline.price_range(D("9700000.00"), D(97), D(70), D(76), D("0"), D("0.03"), D(200000))
    assert D(out["low"]["tep"]) < D(out["high"]["tep"])
    assert D(out["low"]["tep"]) > D(out["low"]["matrix"])


# --- intake ---
def test_intake_registers_hashes_duplicates_cui_and_quarantine(tmp_path):
    root = tmp_path / "00-inputs" / "Step 4 - Amendment 2"
    root.mkdir(parents=True)
    (root / "Attachment_0001_PWS_16SEP2025.txt").write_text("PWS text. Technical Exhibit 4.")
    (root / "copy_of_pws.txt").write_text("PWS text. Technical Exhibit 4.")
    (root / "WD_2015_9999_Rev27.txt").write_text("Wage Determination No.: 2015-9999 Revision No.: 27")
    (root / "Mod_P00013_Notice_of_Assignment.txt").write_text("Remit to: Routing number 000000000 account number 000000000000")
    (root / "TOEP.txt").write_text("CUI ATTENTION Source Selection Information - See FAR 2.101 and 3.104")
    es = {e.name: e for e in I.intake(tmp_path / "00-inputs", archive_dir=tmp_path / "_archive")}
    assert es["copy_of_pws.txt"].duplicate_of.endswith("Attachment_0001_PWS_16SEP2025.txt")
    assert es["Attachment_0001_PWS_16SEP2025.txt"].step == "4" and es["Attachment_0001_PWS_16SEP2025.txt"].kind == "pws"
    assert es["WD_2015_9999_Rev27.txt"].kind == "wage_determination" and es["WD_2015_9999_Rev27.txt"].version == "27"
    assert es["Mod_P00013_Notice_of_Assignment.txt"].quarantine and "bank details" in " ".join(es["Mod_P00013_Notice_of_Assignment.txt"].reasons)
    assert es["TOEP.txt"].cui and not es["TOEP.txt"].ai_ok  # CUI: no AI service until cleared
    assert es["Attachment_0001_PWS_16SEP2025.txt"].ai_ok
    arch = list((tmp_path / "_archive").iterdir())
    assert len(arch) == 5 and all(not os.access(a, os.W_OK) or os.geteuid() == 0 for a in arch)
    assert "REDACTED" in I.redact_bank_details("Routing number 000000000")


# --- second brain ---
def test_second_brain_index_and_lessons(tmp_path):
    case, _, _ = toy_case()
    res = price_case(case, toy_scenario())
    out = Outcome("TOY", "Won", "Good", strengths=["Cash in lieu of H&W (significant strength)"], offerors=6, winning_price="n/a", judgements=["Price was not the discriminator"])
    p = record_bid(tmp_path, "TOY", res, {"agency": "USAF", "vehicle": "PLS", "solicitation": "TOY-001", "basis": "best value"}, {"rate.fee": "0.02"}, out, ["Cash in lieu of health benefits"])
    rows = list(csv.DictReader(open(tmp_path / "bids-index.csv")))
    assert rows[0]["bid"] == "TOY" and rows[0]["outcome"] == "Won" and rows[0]["tep"] == str(round_cents(res.tep))
    text = p.read_text()
    assert "## Sourced facts" in text and "## Judgements" in text and "> Cash in lieu" in text and "| Alpha AFB |" in text
    # re-recording updates the row instead of duplicating it
    record_bid(tmp_path, "TOY", res, {"agency": "USAF"}, {}, Outcome("TOY", "Won", "Good"), None)
    assert len(list(csv.DictReader(open(tmp_path / "bids-index.csv")))) == 1
