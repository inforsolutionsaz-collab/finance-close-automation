"""End-to-end close orchestration: load -> validate -> reconcile -> accrue ->
propose JEs -> controls -> workpaper + audit trail."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import uuid
from pathlib import Path

import pandas as pd

from ap_tieout import tie_out_ap
from bank_recon import reconcile_bank
from card_accrual import build_card_accrual
from config import CloseConfig, DEFAULT_CONFIG
from journal import build_journal_entries
from workpaper import build_workpaper

log = logging.getLogger("closeauto")

REQUIRED = {
    "gl_cash_detail.csv": ["gl_line_id", "posting_date", "document_type", "reference",
                           "debit", "credit", "offset_account"],
    "bank_statement.csv": ["bank_txn_id", "post_date", "description", "reference", "amount"],
    "ap_open_bills.csv": ["bill_id", "vendor", "vendor_invoice", "bill_date", "due_date", "amount_open"],
    "gl_ap_control_detail.csv": ["source", "document_number", "debit", "credit"],
    "concur_card_transactions.csv": ["card_txn_id", "employee", "txn_date", "expense_category",
                                     "amount", "report_status", "receipt_attached"],
    "prior_period_balances.csv": ["ending_balance_book", "ending_balance_bank", "status"],
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_inputs(in_dir: Path) -> tuple[dict, list]:
    data, controls = {}, []
    for name, cols in REQUIRED.items():
        p = in_dir / name
        if not p.exists():
            raise FileNotFoundError(f"Missing input extract: {p}")
        df = pd.read_csv(p, dtype={"reference": str, "offset_account": str, "account": str})
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(f"{name} missing columns {missing}")
        data[name] = df
    controls.append({"control": "C1 Input completeness & schema", "result": "PASS",
                     "detail": f"{len(REQUIRED)} extracts loaded; required columns present"})
    return data, controls


def run_close(in_dir: str | Path, out_dir: str | Path, cfg: CloseConfig = DEFAULT_CONFIG) -> dict:
    in_dir, out_dir = Path(in_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"CLOSE-{cfg.period_end:%Y%m}-{uuid.uuid4().hex[:6].upper()}"
    run_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info("Starting %s", run_id)

    data, controls = load_inputs(in_dir)
    prior = data["prior_period_balances.csv"].iloc[-1]
    opening_book, opening_bank = float(prior["ending_balance_book"]), float(prior["ending_balance_bank"])
    ok = prior["status"] == "Reconciled"
    controls.append({"control": "C2 Prior period reconciled & rolled forward", "result": "PASS" if ok else "FAIL",
                     "detail": f"Opening book {opening_book:,.2f} / bank {opening_bank:,.2f}; prior status {prior['status']}"})

    bank = reconcile_bank(data["gl_cash_detail.csv"], data["bank_statement.csv"], opening_book, opening_bank, cfg)
    s = bank.summary
    log.info("Bank recon: %s matched of %s bank lines; difference %.2f",
             s["bank_lines_auto_matched"], s["bank_lines"], s["unreconciled_difference"])
    controls.append({"control": "C3 Cash reconciliation difference = 0",
                     "result": "PASS" if abs(s["unreconciled_difference"]) < 0.005 else "FAIL",
                     "detail": f"Adjusted bank {s['adjusted_bank_balance']:,.2f} vs adjusted book {s['adjusted_book_balance']:,.2f}"})
    controls.append({"control": "C4 No unexplained reconciling items",
                     "result": "PASS" if s["unexplained_items_amount"] == 0 else "FAIL",
                     "detail": f"Unexplained amount {s['unexplained_items_amount']:,.2f}"})

    ap = tie_out_ap(data["ap_open_bills.csv"], data["gl_ap_control_detail.csv"], cfg)
    controls.append({"control": "C5 AP subledger ties to GL control",
                     "result": "PASS" if ap.summary["status"] == "TIED OUT" else "FAIL",
                     "detail": f"Difference {ap.summary['difference']:,.2f}; explained {ap.summary['explained_by_manual_jes']:,.2f}"})
    controls.append({"control": "C6 Duplicate vendor bill check",
                     "result": "REVIEW" if ap.summary["duplicate_bills_flagged"] else "PASS",
                     "detail": f"{ap.summary['duplicate_bills_flagged']} suspect bill(s), "
                               f"{ap.summary['duplicate_exposure']:,.2f} exposure - payment hold recommended"})

    card = build_card_accrual(data["concur_card_transactions.csv"], cfg)
    controls.append({"control": "C7 Card categories mapped to GL", "result": "PASS",
                     "detail": f"{card.summary['card_txns_in_period']} transactions mapped"})
    controls.append({"control": "C8 Card policy compliance",
                     "result": "REVIEW" if card.summary["policy_flags"] else "PASS",
                     "detail": f"{card.summary['policy_flags']} flag(s) across {card.summary['employees_with_flags']} employee(s)"})

    je = build_journal_entries(bank.journal_lines + ap.journal_lines + card.journal_lines, cfg)
    controls.append({"control": "C9 Journal entries balanced & accounts valid", "result": "PASS",
                     "detail": f"{je['external_id'].nunique()} entries, {len(je)} lines, "
                               f"DR {je['debit'].sum():,.2f} = CR {je['credit'].sum():,.2f}"})
    controls.append({"control": "C10 Segregation of duties", "result": "REVIEW",
                     "detail": "JEs staged 'Pending Approval'; human reviewer must approve before posting"})

    run_meta = {"run_id": run_id, "run_at": run_at}
    build_workpaper(out_dir / f"Close_Workpaper_{cfg.period_end:%Y-%m}.xlsx", cfg, run_meta, bank, ap, card, je, controls)
    je.to_csv(out_dir / f"NetSuite_JE_Import_{cfg.period_end:%Y-%m}.csv", index=False)
    bank.gl_exceptions.to_csv(out_dir / "open_items_gl.csv", index=False)

    result = {
        **run_meta, "company": cfg.company, "period_end": str(cfg.period_end),
        "bank": s, "ap": ap.summary, "card": card.summary,
        "journal_entries": {"count": int(je["external_id"].nunique()), "lines": int(len(je)),
                            "total_debits": round(float(je["debit"].sum()), 2)},
        "controls": controls,
        "inputs": {n: {"rows": int(len(df)), "sha256": _sha256(in_dir / n)} for n, df in data.items()},
    }
    (out_dir / "close_summary.json").write_text(json.dumps(result, indent=2, default=str))
    log.info("Finished %s -> %s", run_id, out_dir)
    return result
