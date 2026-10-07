"""Command line: one command per phase step (Game Plan section 14: a non-developer runs one command).

    python -m patriot_engine new  <bid_dir> [--case rcc]
    python -m patriot_engine intake <bid_dir>
    python -m patriot_engine init <bid_dir> [--calibrate | --default-burden 0.09]
    python -m patriot_engine approve <bid_dir> --by "Name" --phase 2
    python -m patriot_engine price <bid_dir> [--target 28000000] [--allow-unapproved]
    python -m patriot_engine diff <bid_dir>
    python -m patriot_engine questions <bid_dir>
    python -m patriot_engine export <bid_dir> --scenario Base
    python -m patriot_engine replay <bid_dir>
    python -m patriot_engine record <bid_dir> --outcome Won --rating Good ...
    python -m patriot_engine step1 <bid_dir> --piid <incumbent PIID>
"""

from __future__ import annotations

import argparse
import warnings
import json
import sys
from decimal import Decimal
from pathlib import Path

from . import __version__
from . import assumptions as A
from . import checks as C
from . import export as X
from . import intake as I
from . import workspace as W
from .calibrate import calibrate_from_matrix
from .delta import delta_summary
from .differ import diff_documents, diff_te4
from .extract.price_matrix import read_matrix
from .init_assumptions import base_scenario, load_levers, write_assumptions
from .pricing.model import Scenario, price_case
from .pricing.money import D, round_cents
from .pricing.scenarios import price_to_win_report, run_all, sensitivity
from .pricing.tep import extension_sensitivity
from .questions import build_questions
from .second_brain import Outcome, record_bid

warnings.filterwarnings("ignore", message="Unknown extension is not supported")



def _ws(args, need_assumptions=True):
    return W.load(args.bid_dir, need_assumptions)


def cmd_new(a):
    p = W.new_bid(a.bid_dir, a.case)
    print(f"created {p}; edit {p / 'bid.yaml'} to point at the input files")


def cmd_intake(a):
    root = Path(a.bid_dir)
    entries = I.intake(root / "00-inputs", archive_dir=(root / "_archive") if a.archive else None)
    out = root / "10-working" / "register.csv"
    I.write_register(entries, out)
    q = [e for e in entries if e.quarantine]
    c = [e for e in entries if e.cui]
    d = [e for e in entries if e.duplicate_of]
    print(f"{len(entries)} files registered -> {out}")
    print(f"  duplicates: {len(d)}   CUI (AI off): {len(c)}   quarantined: {len(q)}")
    for e in q:
        print(f"  QUARANTINE {e.name}: {'; '.join(e.reasons)}")
    W.log(root, "intake", files=len(entries), quarantined=len(q))


def cmd_init(a):
    root = Path(a.bid_dir)
    # a fresh calibration must start from an uncalibrated case, or re-running `init --calibrate` would not be repeatable
    ws = W.load(root, need_assumptions=False, use_calibration=not (a.calibrate or a.default_burden is not None))
    cal = None
    base = base_scenario(ws.spec)
    prior = ws.bid["inputs"].get("prior_bid_matrix")
    if a.calibrate:
        if not prior:
            sys.exit("--calibrate needs inputs.prior_bid_matrix in bid.yaml (a submitted Vol II, ideally from a comparable bid)")
        print("NOTE: opening a finished price workbook. For a live bid this stays closed until phase 5; for a closed bid it is the truth we test against.")
        m = read_matrix(W._p(root, prior), ws.mapping)
        cal = calibrate_from_matrix(ws.case, m, base)
        W.save_calibration(cal, root / "10-working" / "calibration.json")
        for k, v in cal.residuals.items():
            print(f"  {k}: {v}")
        ws = W.load(root, need_assumptions=False)
    elif a.default_burden is not None:
        from .calibrate import Calibration

        states = {l.state for l in ws.case.lines if l.classification == "SCA" and l.state}
        cal = Calibration(state_burden={s: D(a.default_burden) for s in states})
        W.save_calibration(cal, root / "10-working" / "calibration.json")
        print(f"seeded {len(states)} states with a default employer burden of {a.default_burden} (Low confidence: replace with real values)")
        ws = W.load(root, need_assumptions=False)
    rows = write_assumptions(ws.assumptions_path, ws.case, ws.spec, cal, phase=a.phase)
    probs = A.validate(ws.assumptions_path)
    print(f"{len(rows)} assumption rows -> {ws.assumptions_path}  (problems: {len(probs)})")
    for p in probs:
        print("  ", p)
    r = ws.report
    for lst, label in ((r.fuzzy_matches, "fuzzy site matches"), (r.unmatched_sites, "UNMATCHED sites"), (r.assumed, "assumed"), (r.split_mismatch, "split mismatch")):
        for x in lst:
            print(f"  {label}: {x}")
    W.log(root, "init", rows=len(rows))


def cmd_approve(a):
    root = Path(a.bid_dir)
    e = A.approve(root / "10-working" / "assumptions.csv", a.by, str(a.phase), a.note or "")
    print(f"approved {e['file']} sha256 {e['sha256'][:12]} by {a.by}")
    W.log(root, "approve", by=a.by, sha=e["sha256"][:12])


def _scenario_with_calibration(ws, s: Scenario) -> Scenario:
    return s


def _context(ws, scen: Scenario, res):
    import csv

    rows = []
    with open(ws.assumptions_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    reg_path = ws.root / "10-working" / "register.csv"
    reg = list(csv.DictReader(open(reg_path, newline="", encoding="utf-8"))) if reg_path.exists() else None
    return C.Context(
        ws.case, scen, res, te4=ws.te4, te4_prior=ws.te4_prior, te5_places=ws.te5, qa_rows=ws.qa or None, register=reg, assumptions=rows
    )


def cmd_price(a):
    root = Path(a.bid_dir)
    ws = _ws(a)
    appr = A.approval_for(ws.assumptions_path)
    if not appr and not a.allow_unapproved:
        sys.exit("assumptions.csv is not approved (or changed since approval). Nothing is priced before sign-off: run `approve`, or pass --allow-unapproved for a draft.")
    scen = ws.scenarios
    results = run_all(ws.case, scen)
    out = ["# Review pack", "", f"assumptions {A.version_label(ws.assumptions_path)} - " + (f"approved by {appr['approver']} {appr['time']}" if appr else "**DRAFT, NOT APPROVED**"), ""]
    out += ["## Scenario comparison (Total Evaluated Price basis)", "", "| Scenario | Matrix total | TEP | FTE base+opt |", "|---|---:|---:|---|"]
    for n, r in results.items():
        out.append(f"| {n} | {round_cents(r.matrix_total):,} | {round_cents(r.tep):,} | {r.fte_base}+{r.fte_optional} |")
    base_name = "Base" if "Base" in scen else next(iter(scen))
    r0 = results[base_name]
    es = extension_sensitivity(ws.case, r0)
    out += ["", f"Extension reading: services only {round_cents(es['services_only']):,}; services + ODC {round_cents(es['services_and_odc']):,} (TOEP is silent; see case.yaml).", ""]
    if a.target:
        t = D(a.target)
        out += ["## Price to Win", "", f"Target TEP {t:,}."]
        for n, s in scen.items():
            rep = price_to_win_report(ws.case, s, t)
            out.append(f"- {n}: TEP {rep['scenario_tep']:,} (gap {rep['gap']:,}); single-lever solves {rep['single_lever_solves']}")
        out.append("")
    out += ["## Sensitivity (Base, one step each way)", "", "| Lever | Base | Step | TEP +step | TEP -step |", "|---|---|---|---:|---:|"]
    for row in sensitivity(ws.case, scen[base_name], extra={"fte.rtl": D(1)}):
        out.append(f"| {row['lever']} | {row['base_value']} | {row['step']} | {row['tep_up']:,} | {row['tep_down']:,} |")
    findings = C.run_checks(_context(ws, scen[base_name], r0))
    out += ["", "## Checks", ""]
    for f in findings:
        imp = f" ({round_cents(f.impact):,})" if f.impact is not None else ""
        out.append(f"- **{f.severity}** rule {f.rule}: {f.title}{imp}. {f.detail}")
    pack = root / "10-working" / "review_pack.md"
    pack.write_text("\n".join(out) + "\n", encoding="utf-8")
    (root / "10-working" / "scenario_summary.json").write_text(json.dumps({n: r.summary() for n, r in results.items()}, indent=1))
    print("\n".join(out))
    print(f"\n-> {pack}")
    W.log(root, "price", approved=bool(appr), scenarios=",".join(results))


def cmd_diff(a):
    root = Path(a.bid_dir)
    ws = _ws(a)
    if not ws.te4_prior:
        sys.exit("bid.yaml has no inputs.pws_prior")
    prior = W._p(root, ws.bid["inputs"]["pws_prior"])
    cur = W._p(root, ws.bid["inputs"]["pws"])
    ch = diff_documents(prior, cur)
    te = diff_te4(ws.te4_prior, ws.te4)
    lines = [f"# Change list: {prior.name} -> {cur.name}", "", f"{len(ch)} document changes, {len(te)} site-level headcount changes.", "", "## Site-level (TE4)", ""]
    lines += [f"- {c.location}: {c.old} -> {c.new}  {c.note}" for c in te] or ["- none"]
    lines += ["", "## Document changes, most price-relevant first", ""]
    lines += [f"- [{c.relevance}] {c.kind} / {c.location}: {c.old[:140]!r} -> {c.new[:140]!r}" for c in ch]
    scen = ws.scenarios
    base = scen.get("Base") or next(iter(scen.values()))
    from .build_case import build_case

    old_case, _ = build_case(ws.cfg, ws.te4_prior, ws.inputs, salaries=ws.cal.implied_salaries if ws.cal else None, site_burden=ws.cal.site_burden if ws.cal else None, state_burden=ws.cal.state_burden if ws.cal else None)
    ds = delta_summary(old_case, ws.case, base)
    lines += ["", "## Delta re-price (same scenario, old vs new statement)", "", f"FTE {ds['fte_old']} -> {ds['fte_new']}; matrix {ds['matrix_old']:,} -> {ds['matrix_new']:,} ({ds['matrix_delta']:,}); TEP delta {ds['tep_delta']:,}"]
    lines += [f"- {l['change']} {l['line']}: {l['matrix_delta']:,}" for l in ds["lines"]]
    out = root / "10-working" / "change_list.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:30]))
    print(f"\n-> {out}")


def cmd_questions(a):
    root = Path(a.bid_dir)
    ws = _ws(a)
    scen = ws.scenarios
    base = scen.get("Base") or next(iter(scen.values()))
    import csv

    rows = list(csv.DictReader(open(ws.assumptions_path, newline="", encoding="utf-8")))
    asked = a.asked or []
    # --as-of prior: the statement as released (step 2A/3), before the answers; TE4 and TE5 must come from the same file
    from .build_case import build_case
    from .extract.te4 import extract_te5_places

    te4, case, te5 = ws.te4, ws.case, ws.te5
    if a.as_of == "prior" and ws.te4_prior:
        te4 = ws.te4_prior
        te5 = extract_te5_places(W._p(root, ws.bid["inputs"]["pws_prior"]))
        case, _ = build_case(ws.cfg, te4, ws.inputs, salaries=ws.cal.implied_salaries if ws.cal else None, site_burden=ws.cal.site_burden if ws.cal else None, state_burden=ws.cal.state_burden if ws.cal else None)
    qs = build_questions(case, base, te4=te4, te5_places=te5, bid=ws.inputs, roles=ws.cfg["roles"], qa_rows=ws.qa, patriot_asked=asked, assumptions=rows)
    lines = ["# Draft questions (a person edits before anything is sent)", "", "| # | To | Impact | Status | Question |", "|---|---|---:|---|---|"]
    for q in qs:
        imp = "" if q.impact is None else f"{round_cents(q.impact):,}"
        lines.append(f"| {q.rank} | {q.audience} | {imp} | {q.status} | {q.text} |")
    out = root / "10-working" / "questions.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def cmd_export(a):
    root = Path(a.bid_dir)
    ws = _ws(a)
    appr = A.approval_for(ws.assumptions_path)
    if not appr:
        sys.exit("Export reads the latest APPROVED assumptions.csv. It is not approved (or changed since approval).")
    scen = ws.scenarios
    if a.scenario not in scen:
        sys.exit(f"unknown scenario {a.scenario}; have {list(scen)}")
    s = scen[a.scenario]
    res = price_case(ws.case, s)
    findings = C.run_checks(_context(ws, s, res))
    if C.has_blockers(findings):
        for f in findings:
            if f.severity == C.BLOCK:
                print(f"BLOCK rule {f.rule}: {f.title}: {f.detail}")
        sys.exit("export stopped: fix the blockers above")
    ver = A.version_label(ws.assumptions_path)
    out_dir = root / "20-outputs"
    existing = list(out_dir.glob(f"{ws.cfg['name']}_submission_{a.scenario}_*"))
    n = len({p.name.split("_VolIII")[0] for p in existing}) + 1
    out = out_dir / f"{ws.cfg['name']}_submission_{a.scenario}_v{n}_{ver}.xlsx"
    rep = X.export_submission(ws.case, res, s, ws.template, ws.mapping, out, ws.bid["offeror"], pricing_assumptions=a.assumption_text or [], assumptions_version=ver)
    v3 = out.with_name(out.stem + "_VolIII.xlsx")
    X.make_vol3(out, ws.mapping, v3, ws.bid["offeror"]["name"])
    problems = []
    lock = X.lock_check(ws.template, out, ws.mapping, rep)
    problems += [f"lock-check: {d}" for d in lock]
    problems += [f"hygiene (Vol II): {d}" for d in X.hygiene_check(out)]
    problems += [f"hygiene (Vol III): {d}" for d in X.hygiene_check(v3)]
    problems += [f"Vol III leaks a price: {d}" for d in X.vol3_leaks(v3, res)]
    tot = X.recalc_totals(out, ws.mapping)
    if tot is None:
        problems.append("penny check SKIPPED: LibreOffice not found")
    else:
        problems += [f"penny mismatch: {d}" for d in X.penny_match(res, tot)]
    print(f"wrote {out.name} and {v3.name}")
    print(f"  matrix {round_cents(res.matrix_total):,}  TEP {round_cents(res.tep):,}  assumptions {ver} approved by {appr['approver']}")
    print(f"  workbook recalculated independently: " + ("matches Python to the penny" if tot is not None and not [p for p in problems if p.startswith('penny')] else "NOT CONFIRMED"))
    for p in problems:
        print("  PROBLEM", p)
    for n_ in rep.notes:
        print("  note", n_)
    W.log(root, "export", file=out.name, scenario=a.scenario, problems=len(problems))
    if problems:
        sys.exit(1)


def cmd_replay(a):
    """Compare to truth: price from phase inputs, then against the submitted bid."""
    root = Path(a.bid_dir)
    ws = _ws(a)
    prior = ws.bid["inputs"].get("prior_bid_matrix")
    if not prior or not ws.cal:
        sys.exit("replay needs inputs.prior_bid_matrix and a calibration (run `init --calibrate`)")
    m = read_matrix(W._p(root, prior), ws.mapping)
    truth = m["summary"]["grand_total"]
    scen = ws.scenarios
    base = scen.get("Base") or next(iter(scen.values()))
    exact = price_case(ws.case, base, rate_overrides=ws.cal.observed_rates, indirect_overrides=ws.cal.indirect_dollars)
    fwd = price_case(ws.case, base)
    print(f"submitted matrix total   {round_cents(truth):,}")
    print(f"exact replay             {round_cents(exact.matrix_total):,}   diff {round_cents(exact.matrix_total - truth) + 0:,}")
    print(f"engine (levers only)     {round_cents(fwd.matrix_total):,}   diff {round_cents(fwd.matrix_total - truth):,} ({(fwd.matrix_total / truth - 1) * 100:.3f}%)")
    print(f"engine TEP               {round_cents(fwd.tep):,}  (TEP, not the matrix, is what the government scores)")
    ok = round_cents(exact.matrix_total) == round_cents(truth)
    print("exact replay: " + ("MATCH to the penny" if ok else "MISMATCH"))
    sys.exit(0 if ok else 1)


def cmd_record(a):
    root = Path(a.bid_dir)
    ws = _ws(a)
    scen = ws.scenarios
    s = scen[a.scenario]
    res = price_case(ws.case, s)
    out = Outcome(
        bid=ws.cfg["name"],
        outcome=a.outcome,
        technical_rating=a.rating or "",
        strengths=a.strength or [],
        weaknesses=a.weakness or [],
        offerors=a.offerors,
        winning_price=a.winning_price or "",
    )
    path = record_bid(
        a.brain,
        ws.cfg["name"],
        res,
        {"solicitation": ws.cfg.get("solicitation", ""), "agency": a.agency or "", "vehicle": a.vehicle or "", "basis": "best value"},
        {k: s.get(k) for k in ("rate.fringe", "rate.overhead", "rate.ga", "rate.fee")},
        out,
        assumptions_version=A.version_label(ws.assumptions_path),
    )
    print(f"recorded -> {path}")


def cmd_step1(a):
    from .step1.usaspending import UsaSpending, build_mod_ledger, reconcile, summarise_ledger

    root = Path(a.bid_dir)
    api = UsaSpending(save_dir=root / "00-inputs" / "web")
    hits = api.find_award(a.piid)
    if not hits:
        sys.exit(f"no award found for {a.piid}")
    gid = hits[0].get("generated_internal_id") or hits[0].get("generated_unique_award_id")
    award = api.award(gid)
    ledger = build_mod_ledger(api.transactions(gid))
    print(json.dumps(award.to_dict(), indent=1))
    print(json.dumps(summarise_ledger(ledger), indent=1))
    for p in reconcile(ledger, award, a.documents):
        print("  CHECK", p)
    import csv

    with open(root / "10-working" / "mod_ledger.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["number", "date", "kind", "amount", "running_total", "description", "action_type"])
        w.writeheader()
        for m in ledger:
            w.writerow(m.to_dict())


def main(argv=None):
    p = argparse.ArgumentParser(prog="patriot_engine", description="Patriot Pricing Engine " + __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        sp = sub.add_parser(name, **kw)
        sp.add_argument("bid_dir")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("new", cmd_new); sp.add_argument("--case", default="rcc")
    sp = add("intake", cmd_intake); sp.add_argument("--archive", action="store_true", help="copy every file read-only into _archive/")
    sp = add("init", cmd_init); sp.add_argument("--calibrate", action="store_true"); sp.add_argument("--default-burden", type=float); sp.add_argument("--phase", type=int, default=2)
    sp = add("approve", cmd_approve); sp.add_argument("--by", required=True); sp.add_argument("--phase", default="2"); sp.add_argument("--note")
    sp = add("price", cmd_price); sp.add_argument("--target"); sp.add_argument("--allow-unapproved", action="store_true")
    add("diff", cmd_diff)
    sp = add("questions", cmd_questions); sp.add_argument("--asked", action="append", help="a question we already asked (repeatable)"); sp.add_argument("--as-of", choices=["current", "prior"], default="current", help="which statement version to question")
    sp = add("export", cmd_export); sp.add_argument("--scenario", required=True); sp.add_argument("--assumption-text", action="append")
    add("replay", cmd_replay)
    sp = add("record", cmd_record)
    sp.add_argument("--scenario", default="Base"); sp.add_argument("--outcome", required=True, choices=["Won", "Lost", "No bid"])
    sp.add_argument("--rating"); sp.add_argument("--strength", action="append"); sp.add_argument("--weakness", action="append")
    sp.add_argument("--offerors", type=int); sp.add_argument("--winning-price"); sp.add_argument("--agency"); sp.add_argument("--vehicle")
    sp.add_argument("--brain", default="Second-Brain")
    sp = add("step1", cmd_step1); sp.add_argument("--piid", required=True); sp.add_argument("--documents", type=int, help="modification documents in hand")

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
