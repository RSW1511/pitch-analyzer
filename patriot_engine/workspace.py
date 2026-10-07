"""A bid workspace: the folder layout of Game Plan section 11 plus ``bid.yaml``.

    <bid>/00-inputs/   AI reads only (never edited; pristine copies live in _archive)
    <bid>/10-working/  register, assumptions.csv, calibration.json, diffs, review packs
    <bid>/20-outputs/  human-approved versions only
    <bid>/_log/        what every run did

``bid.yaml`` names the input files and the packaged case definition::

    case: rcc                       # packaged under patriot_engine/cases/, or a path
    inputs:
      pws: 00-inputs/4-amd2-qa/Attachment_0001_..._16SEP2025.docx
      pws_prior: 00-inputs/2A-solicitation/Attachment_0001_..._18AUG2025.docx
      bid_salaries: 00-inputs/6-pricing-plan/Bid Salaries - Wage Rates.xlsx
      template: 00-inputs/2A-solicitation/Attachment_0004_..._18AUG2025.xlsx
      qa: 00-inputs/4-amd2-qa/Attachment_0012_..._19_SEPT_25.xlsx
      prior_bid_matrix: 00-inputs/7-submission/..._Vol_II_....xlsx   # optional calibration source
    offeror: {name: "...", cage: "...", date: "..."}
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from . import assumptions as A
from .build_case import BuildReport, build_case, load_case_config
from .calibrate import Calibration, calibrate_from_matrix
from .extract.bid_inputs import BidInputs, extract_bid_inputs
from .extract.price_matrix import read_matrix
from .extract.qa import extract_qa
from .extract.te4 import Te4, extract_te4, extract_te5_places
from .pricing.model import Case, Scenario
from .pricing.money import D

PKG = Path(__file__).parent
FOLDERS = ("00-inputs", "10-working", "20-outputs", "_log")


def case_dir(name: str) -> Path:
    p = Path(name)
    return p if p.exists() else PKG / "cases" / name


def new_bid(root: str | Path, case: str = "rcc") -> Path:
    root = Path(root)
    for f in FOLDERS:
        (root / f).mkdir(parents=True, exist_ok=True)
    cfg = root / "bid.yaml"
    if not cfg.exists():
        cfg.write_text(
            yaml.safe_dump(
                {
                    "case": case,
                    "inputs": {"pws": "", "pws_prior": "", "bid_salaries": "", "template": "", "qa": "", "prior_bid_matrix": ""},
                    "offeror": {"name": "Your Company, LLC", "cage": "", "version": "Initial", "date": ""},
                },
                sort_keys=False,
            )
        )
    return root


def log(root: Path, event: str, **kw) -> None:
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **{k: str(v) for k, v in kw.items()}}
    with open(root / "_log" / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# --- calibration persistence --------------------------------------------------------
def save_calibration(cal: Calibration, path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "site_burden": {k: str(v) for k, v in cal.site_burden.items()},
                "state_burden": {k: str(v) for k, v in cal.state_burden.items()},
                "exempt_adder": str(cal.exempt_adder),
                "esc_oy1": str(cal.esc_oy1),
                "esc_oy2": str(cal.esc_oy2),
                "implied_salaries": {k: str(v) for k, v in cal.implied_salaries.items()},
                "indirect_dollars": {k: str(v) for k, v in cal.indirect_dollars.items()},
                "observed_rates": {f"{k[0]}||{k[1]}": str(v) for k, v in cal.observed_rates.items()},
                "residuals": cal.residuals,
                "notes": cal.notes,
            },
            indent=1,
        )
    )


def load_calibration(path: Path) -> Calibration:
    d = json.loads(path.read_text())
    return Calibration(
        site_burden={k: D(v) for k, v in d["site_burden"].items()},
        state_burden={k: D(v) for k, v in d["state_burden"].items()},
        exempt_adder=D(d["exempt_adder"]),
        esc_oy1=D(d["esc_oy1"]),
        esc_oy2=D(d["esc_oy2"]),
        implied_salaries={k: D(v) for k, v in d["implied_salaries"].items()},
        indirect_dollars={k: D(v) for k, v in d["indirect_dollars"].items()},
        observed_rates={tuple(k.split("||")): D(v) for k, v in d["observed_rates"].items()},
        residuals=d.get("residuals", {}),
        notes=d.get("notes", []),
    )


@dataclass
class Workspace:
    root: Path
    bid: dict
    cfg: dict
    mapping: dict
    te4: Te4
    te4_prior: Optional[Te4]
    te5: List[str]
    inputs: BidInputs
    qa: List[dict]
    template: Path
    case: Case
    report: BuildReport
    cal: Optional[Calibration]
    assumptions_path: Path
    spec: dict = field(default_factory=dict)

    @property
    def scenarios(self) -> Dict[str, Scenario]:
        return A.load_scenarios(self.assumptions_path)


def _p(root: Path, rel: str) -> Optional[Path]:
    if not rel:
        return None
    p = Path(rel)
    return p if p.is_absolute() else root / rel


def load(root: str | Path, need_assumptions: bool = True, use_calibration: bool = True) -> Workspace:
    root = Path(root)
    bid = yaml.safe_load((root / "bid.yaml").read_text())
    cdir = case_dir(bid["case"])
    cfg = load_case_config(cdir / "case.yaml")
    mapping = yaml.safe_load((cdir / "mapping.yaml").read_text())
    inp = {k: _p(root, v) for k, v in bid["inputs"].items()}
    te4 = extract_te4(inp["pws"])
    te4_prior = extract_te4(inp["pws_prior"]) if inp.get("pws_prior") else None
    te5 = extract_te5_places(inp["pws"])
    inputs = extract_bid_inputs(inp["bid_salaries"])
    qa = extract_qa(inp["qa"]) if inp.get("qa") and inp["qa"].exists() else []
    cal_path = root / "10-working" / "calibration.json"
    cal = load_calibration(cal_path) if (use_calibration and cal_path.exists()) else None
    case, report = build_case(
        cfg,
        te4,
        inputs,
        salaries=cal.implied_salaries if cal else None,
        site_burden=cal.site_burden if cal else None,
        state_burden=cal.state_burden if cal else None,
    )
    lev = cdir / "levers.yaml"
    spec = yaml.safe_load(lev.read_text(encoding="utf-8")) if lev.exists() else {"gov": [], "levers": []}
    return Workspace(root, bid, cfg, mapping, te4, te4_prior, te5, inputs, qa, inp["template"], case, report, cal, root / "10-working" / "assumptions.csv", spec)
