import datetime as dt

import pandas as pd
import pytest

from ap_tieout import tie_out_ap
from bank_recon import reconcile_bank
from card_accrual import build_card_accrual
from config import DEFAULT_CONFIG as CFG
from data_gen import generate
from journal import build_journal_entries, validate_balanced
from pipeline import run_close


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    d = tmp_path_factory.mktemp("close")
    generate(d / "in", seed=42)
    return run_close(d / "in", d / "out"), d


def gl_line(i, date, ref, amount, doc="Customer Payment", offset="1200"):
    return {"gl_line_id": f"G{i}", "posting_date": date, "document_type": doc, "document_number": f"D{i}",
            "reference": ref, "description": "x", "debit": max(amount, 0), "credit": max(-amount, 0),
            "offset_account": offset, "source": "Subledger"}


def bank_line(i, date, desc, ref, amount):
    return {"bank_txn_id": f"B{i}", "post_date": date, "description": desc, "reference": ref, "amount": amount}


# ---------- end-to-end ----------
def test_cash_reconciles_to_zero(result):
    r, _ = result
    assert r["bank"]["unreconciled_difference"] == 0
    assert r["bank"]["unexplained_items_amount"] == 0
    assert r["bank"]["status"] == "RECONCILED"


def test_no_control_fails(result):
    r, _ = result
    assert not [c for c in r["controls"] if c["result"] == "FAIL"]


def test_ap_ties_out_after_manual_je(result):
    r, _ = result
    assert r["ap"]["difference"] == pytest.approx(12_500.00)
    assert r["ap"]["unexplained_difference"] == 0


def test_outputs_written(result):
    _, d = result
    out = d / "out"
    assert (out / "Close_Workpaper_2026-10.xlsx").exists()
    je = pd.read_csv(out / "NetSuite_JE_Import_2026-10.csv")
    validate_balanced(je.assign(account_name="ok"))


def test_deterministic_with_seed(tmp_path):
    generate(tmp_path / "a", seed=7)
    generate(tmp_path / "b", seed=7)
    assert (tmp_path / "a/bank_statement.csv").read_text() == (tmp_path / "b/bank_statement.csv").read_text()


# ---------- matching rules ----------
D = dt.date(2026, 10, 15)


def test_transposition_detected_and_corrected():
    gl = pd.DataFrame([gl_line(1, D, "R1", 15_320.00)])
    bank = pd.DataFrame([bank_line(1, D, "ACH CREDIT", "R1", 13_520.00)])
    r = reconcile_bank(gl, bank, 0, 0, CFG)
    assert r.variances.iloc[0]["likely_cause"].startswith("Transposition")
    assert r.summary["unreconciled_difference"] == 0


def test_batch_deposit_many_to_one():
    gl = pd.DataFrame([gl_line(i, D, f"L{i}", a) for i, a in enumerate([100.10, 250.25, 649.65])])
    bank = pd.DataFrame([bank_line(1, D + dt.timedelta(days=1), "LOCKBOX DEPOSIT", "", 1000.00)])
    r = reconcile_bank(gl, bank, 0, 0, CFG)
    assert set(r.matches["match_rule"]) == {"3-Batch deposit (many-to-one)"}
    assert len(r.matches) == 3


def test_duplicate_posting_flagged():
    gl = pd.DataFrame([gl_line(1, D, "U1", -500.0, "Journal", "6600"),
                       gl_line(2, D, "U1", -500.0, "Journal", "6600")])
    bank = pd.DataFrame([bank_line(1, D, "ACH DEBIT", "U1", -500.0)])
    r = reconcile_bank(gl, bank, 0, 0, CFG)
    assert r.gl_exceptions.iloc[0]["classification"] == "Book error - duplicate posting"
    assert r.summary["status"] == "RECONCILED"


def test_old_unmatched_item_is_not_called_timing():
    gl = pd.DataFrame([gl_line(1, dt.date(2026, 10, 2), "X9", -900.0, "Check", "2000")])
    r = reconcile_bank(gl, pd.DataFrame([bank_line(1, D, "CHECK 1", "1", -1.0)]), 0, 0, CFG)
    assert "Unexplained" in r.gl_exceptions.iloc[0]["classification"]
    assert r.summary["status"].startswith("EXCEPTIONS")


def test_bank_fee_rule_generates_je():
    gl = pd.DataFrame([gl_line(1, D, "R1", 10.0)])
    bank = pd.DataFrame([bank_line(1, D, "ACH", "R1", 10.0), bank_line(2, D, "WIRE FEE", "", -25.0)])
    r = reconcile_bank(gl, bank, 0, 0, CFG)
    je = build_journal_entries(r.journal_lines, CFG)
    assert je.loc[je["account"] == "6900", "debit"].sum() == 25.0


# ---------- AP & card ----------
def test_duplicate_bill_same_invoice():
    bills = pd.DataFrame([
        {"bill_id": "B1", "vendor": "V", "vendor_invoice": "INV-1", "bill_date": "2026-10-01", "due_date": "2026-10-31", "amount_open": 100.0},
        {"bill_id": "B2", "vendor": "V", "vendor_invoice": "INV-1", "bill_date": "2026-10-03", "due_date": "2026-11-02", "amount_open": 100.0},
    ])
    gl = pd.DataFrame([{"source": "AP Bill", "document_number": "S", "memo": "", "debit": 0, "credit": 200.0,
                        "posting_date": "2026-10-31"}])
    r = tie_out_ap(bills, gl, CFG)
    assert r.summary["duplicate_bills_flagged"] == 1
    assert r.summary["status"] == "TIED OUT"


def test_card_accrual_excludes_approved_and_future():
    card = pd.DataFrame([
        {"card_txn_id": "1", "employee": "E", "txn_date": "2026-10-20", "merchant": "M", "expense_category": "Meals",
         "amount": 50.0, "report_status": "Approved", "receipt_attached": True},
        {"card_txn_id": "2", "employee": "E", "txn_date": "2026-10-28", "merchant": "M", "expense_category": "Lodging",
         "amount": 300.0, "report_status": "Not Submitted", "receipt_attached": False},
        {"card_txn_id": "3", "employee": "E", "txn_date": "2026-11-02", "merchant": "M", "expense_category": "Meals",
         "amount": 40.0, "report_status": "Not Submitted", "receipt_attached": True},
    ])
    r = build_card_accrual(card, CFG)
    assert r.summary["accrual_total"] == 300.0
    assert "Missing receipt" in r.policy_flags.iloc[0]["flag"]


def test_unbalanced_je_rejected():
    with pytest.raises(ValueError):
        build_journal_entries([("X", "p", "1010", 10.0, "m"), ("X", "p", "6900", -9.0, "m")], CFG)
