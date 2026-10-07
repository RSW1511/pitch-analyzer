# Patriot Pricing Engine

A deterministic pricing engine for services-contract recompetes, built for the ASU W. P. Carey
capstone (Team 6) from *Patriot Pricing Engine Game Plan v2* and the interim presentation.

**Design rule: nothing in the price path is a language model.** An AI agent may read documents and
draft `assumptions.csv`; the pricing manager approves it; this package does every calculation and
writes the Excel files. If the AI tool is never approved, the engine still works.

```
collect -> register -> extract -> diff -> assumptions.csv -> APPROVE -> price -> check -> export -> record
  M1        M0          M2        M3          M4                          M5      M6      M8       M9
```

| Module | File | What it does |
|---|---|---|
| M0 intake & register | `intake.py` | hash, step, version, type; duplicates; CUI/bank-detail/protective-order quarantine; read-only archive |
| M1 Step 1 collectors | `step1/usaspending.py`, `step1/signals.py`, `step1/baseline.py` | USAspending award + modification ledger (saved with URL/date/hash); RFI signal scan; first Price-to-Win range on the TEP basis |
| M2 extractors | `extract/te4.py`, `bid_inputs.py`, `qa.py`, `price_matrix.py` | staffing table, wage/salary workbook, Q&A, filled price matrix, all parsed cell by cell |
| M3 differ | `differ.py`, `delta.py` | Word diff by heading and table cell (renumbering-aware), site-level headcount diff, delta re-price |
| M4 assumptions | `assumptions.py`, `init_assumptions.py` | one row per number: value, source, phase, confidence, status; scenarios as columns; approval bound to the file's SHA-256 |
| M5 pricing core | `pricing/` | SCA and exempt rate builds, hours, periods, Total Evaluated Price, scenarios, reverse solve, sensitivity |
| M6 checks | `checks.py` | the engine rules as code; BLOCK / REVIEW / INFO with dollar impact |
| M7 questions | `questions.py` | draft questions for the government and for the pricing manager, ranked by price impact, de-duplicated against the published Q&A |
| M8 outputs | `export.py` | fills a fresh copy of the government's template, builds Vol III, lock-check, hygiene check, independent LibreOffice recalculation |
| M9 second brain | `second_brain.py` | `bids-index.csv` + `lessons/<bid>.md` |

## Install and run

```bash
pip install -r patriot_engine/requirements.txt
python -m pytest patriot_engine/tests            # 50+ tests, synthetic data only
```

A bid lives in a workspace folder (`00-inputs/`, `10-working/`, `20-outputs/`, `_log/`) described by
`bid.yaml`. The packaged `cases/example/` shows the three files a new solicitation needs:
`case.yaml` (periods, government ODC lines, role map), `mapping.yaml` (engine -> template cells, Vol III
blank list, template traps) and `levers.yaml` (default scenario values, **illustrative**: replace them).

```bash
python -m patriot_engine new   bids/X --case example     # folders + bid.yaml; edit the input paths
python -m patriot_engine intake bids/X --archive          # register, hash, quarantine, read-only copies
python -m patriot_engine init  bids/X --calibrate         # or --default-burden 0.09 (Low confidence)
python -m patriot_engine approve bids/X --by "Name"       # nothing prices or exports before this
python -m patriot_engine price bids/X --target 28000000   # scenarios, Price-to-Win, sensitivity, checks
python -m patriot_engine diff  bids/X                     # change list + delta re-price between statement versions
python -m patriot_engine questions bids/X --as-of prior   # ranked draft questions
python -m patriot_engine export bids/X --scenario Base    # filled template + Vol III + lock/penny checks
python -m patriot_engine replay bids/X                    # compare to a closed bid's truth workbook
python -m patriot_engine record bids/X --outcome Won --rating Good ...
python -m patriot_engine step1 bids/X --piid <incumbent PIID>   # USAspending (needs network access)
```

Edit `assumptions.csv` after approval and the approval no longer matches the file: `export` refuses.

## How a price is made

* **SCA lines**: `rate = ROUND((wages on productive hours + vacation/sick/holiday + H&W x 2,080 + indirects) / productive hours, 2)`,
  held flat across years. Indirects = base cost x ((1 + employer burden by state) x (1+OH)(1+G&A)(1+fee) - 1).
* **Exempt lines**: `salary x (1+fringe) x (1+OH)(1+G&A)(1+fee) / 2,080 + adder`, escalated per year, billed on productive hours.
* 10-month base-period hours are per-FTE hours truncated to cents, then multiplied by FTE.
* **Total Evaluated Price** = every FFP CLIN + government ODC CLINs + the FAR 52.217-8 extension priced at the last option
  year's rates. The solicitation is silent on whether extension ODCs count; `case.yaml` has a flag and both readings are reported.
* All math is `Decimal`; only totals are rounded, as Excel does, so workbook totals match to the cent.

## Data policy (this repository is public)

Client documents and company-internal figures **must not be committed**. `bids/`, `cases/rcc/`, `local/` and the real-bid
regression test are git-ignored. The committed tests use synthetic fixtures with invented numbers. To run the real-bid
regression locally: `PATRIOT_RCC_BID=/path/to/workspace python -m pytest patriot_engine/tests`.
If the repository is ever made private, those paths can be committed deliberately.

## What is and is not done

Done and tested: everything in the table above. Not done / known limits:

* **Calibration is in-sample.** Employer burden by state, the exempt adder and the exempt escalation are reverse-engineered from a
  *submitted* bid because overhead, G&A, fee and payroll burden are in no file. Rebuilding a bid "from Phase 1 documents alone"
  needs the real internal rates from the pricing manager (deck ask 1) or the second brain.
* **USAspending collector is untested against the live API** (the build sandbox blocks the host). The parser is covered by
  tests on recorded-shape responses; run `step1` once from a networked machine and check `00-inputs/web/`.
* The staffing extractor and case builder are shaped around the RCC-style table (PM / site RCCs / TCCs / leads / support /
  optional tasks). Another solicitation needs its own `case.yaml` role map, and possibly an extractor for its table.
* Rules 3, 14 and 17-19 are implemented as differ / mod ledger / exporter behaviours; rule 2 compares priced FTE to flagged
  alternatives but does not itself read prose to find the conflict.
* Question generation and checks are rule-based drafts; a person edits before anything is sent.
* The SAM.gov, GAO, FOIA tracker and paid-aggregator collectors from Game Plan section 3.1 are not built.
* Export edits the template with openpyxl; unknown extensions (a few data-validation add-ins) are dropped on save. The lock-check
  and the independent recalculation catch cell-level drift, not that.
