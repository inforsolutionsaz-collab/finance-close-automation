"""Synthetic, seeded month-end data for a fictional company.

Produces ERP-style extracts (NetSuite GL detail, bank statement, AP open-bill
aging, Concur card feed). Realistic close issues are planted so the engine has
something to find: timing items, bank-only charges, an NSF check, a transposed
amount, a duplicate posting, a lockbox batch deposit, a manual JE to the AP
control account, a duplicate vendor bill, and unreported card spend.

No real company or person data is used.
"""
from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import pandas as pd

from config import CloseConfig, DEFAULT_CONFIG

OPENING_CASH = 1_250_000.00

CUSTOMERS = ["Alpine Retail", "Cascade Sports", "Redwood Outfitters", "Trailhead Supply",
             "Bluewater Marine", "Granite Peak Gear", "Northwind Cycles", "Sierra Goods"]
VENDORS = ["Pacific Fabrics", "Evergreen Logistics", "Summit Packaging", "Coastal Freight",
           "Ironwood Metals", "Brightline Software", "Harbor Print Co", "Valley Electric",
           "Meridian Plastics", "Oakridge Facilities", "Clearview Staffing", "Northstar Insurance"]
EMPLOYEES = ["J. Alvarez", "P. Chen", "R. Okafor", "S. Patel", "L. Novak", "D. Kim", "A. Romero"]
CARD_MERCHANTS = {
    "Airfare": ["United Airlines", "Alaska Air", "Delta"],
    "Ground Transport": ["Uber", "Lyft", "Hertz"],
    "Lodging": ["Marriott", "Hilton", "Hyatt"],
    "Meals": ["Starbucks", "Chipotle", "Local Bistro", "Airport Grill"],
    "Software": ["Zoom", "Adobe", "Atlassian"],
    "Office Supplies": ["Staples", "Amazon Business"],
}
CARD_RANGES = {"Airfare": (180, 650), "Ground Transport": (15, 90), "Lodging": (160, 480),
               "Meals": (8, 140), "Software": (15, 300), "Office Supplies": (20, 260)}


def _d(cfg: CloseConfig, day: int) -> dt.date:
    return cfg.period_start + dt.timedelta(days=day - 1)


def _money(rng: random.Random, lo: float, hi: float) -> float:
    return round(rng.uniform(lo, hi), 2)


def generate(out_dir: str | Path, seed: int = 42, cfg: CloseConfig = DEFAULT_CONFIG) -> dict:
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gl, bank = [], []
    gl_seq, bank_seq = [1000], [5000]

    def add_gl(date, doc_type, doc_no, ref, desc, amount, offset, source="Subledger"):
        gl_seq[0] += 1
        gl.append({
            "gl_line_id": f"GL{gl_seq[0]}", "posting_date": date, "account": cfg.cash_account,
            "document_type": doc_type, "document_number": doc_no, "reference": ref,
            "description": desc, "debit": round(max(amount, 0), 2),
            "credit": round(max(-amount, 0), 2), "offset_account": offset, "source": source,
        })

    def add_bank(date, desc, ref, amount):
        bank_seq[0] += 1
        bank.append({"bank_txn_id": f"BK{bank_seq[0]}", "post_date": date, "description": desc,
                     "reference": ref, "amount": round(amount, 2)})

    # 1) Customer ACH/wire receipts — bank posts same day; ~half carry our reference
    for i in range(36):
        day = rng.randint(1, 28)
        amt = _money(rng, 2_000, 38_000)
        cust = rng.choice(CUSTOMERS)
        ref = f"RCPT-{7000 + i}"
        add_gl(_d(cfg, day), "Customer Payment", f"PYMT{3100 + i}", ref,
               f"Receipt - {cust}", amt, "1200")
        add_bank(_d(cfg, day + rng.choice([0, 0, 1])), f"ACH CREDIT {cust.upper()}",
                 ref if rng.random() < 0.5 else "", amt)

    # 2) Lockbox batches: several customer checks deposited as one bank line
    for b, day in enumerate([7, 15, 22]):
        amts = [_money(rng, 800, 9_000) for _ in range(rng.randint(3, 4))]
        for j, amt in enumerate(amts):
            add_gl(_d(cfg, day), "Customer Payment", f"PYMT{3200 + b * 10 + j}",
                   f"LBX-{b + 1}-{j + 1}", f"Lockbox receipt - {rng.choice(CUSTOMERS)}",
                   amt, "1200")
        add_bank(_d(cfg, day + 1), f"LOCKBOX DEPOSIT BATCH {b + 1}", "", sum(amts))

    # 3) Vendor checks — clear 2-9 days later; late-month checks stay outstanding
    for i in range(26):
        day = rng.randint(1, 30)
        amt = _money(rng, 400, 18_000)
        chk = str(10_001 + i)
        add_gl(_d(cfg, day), "Check", f"CHK{chk}", chk, f"Check - {rng.choice(VENDORS)}",
               -amt, "2000")
        clear = _d(cfg, day) + dt.timedelta(days=rng.randint(2, 9))
        if clear <= cfg.period_end:
            add_bank(clear, f"CHECK {chk}", chk, -amt)

    # 4) Vendor ACH payments — bank same/next day, reference carried
    for i in range(28):
        day = rng.randint(1, 29)
        amt = _money(rng, 900, 25_000)
        ref = f"ACH-{8800 + i}"
        add_gl(_d(cfg, day), "Bill Payment", f"BPMT{4400 + i}", ref,
               f"ACH - {rng.choice(VENDORS)}", -amt, "2000")
        add_bank(_d(cfg, day + rng.choice([0, 1])), "ACH DEBIT VENDOR PMT", ref, -amt)

    # 5) Payroll funding
    for day in (14, 29):
        amt = _money(rng, 142_000, 151_000)
        add_gl(_d(cfg, day), "Journal", f"JE-PR-{day}", f"PR-{day}", "Payroll funding",
               -amt, "2300", source="Payroll")
        add_bank(_d(cfg, day), "ADP PAYROLL", "", -amt)

    # ---------------- Planted close issues ----------------
    # a) Deposits in transit: recorded 10/31, bank credits 11/1 (not on statement)
    for k in range(2):
        add_gl(cfg.period_end, "Customer Payment", f"PYMT39{k}", f"RCPT-79{k}",
               f"Receipt - {rng.choice(CUSTOMERS)}", _money(rng, 4_000, 16_000), "1200")
    # b) Bank-only items
    add_bank(cfg.period_end, "MONTHLY SERVICE CHARGE", "", -45.00)
    add_bank(_d(cfg, 9), "OUTGOING WIRE FEE", "", -25.00)
    add_bank(_d(cfg, 23), "OUTGOING WIRE FEE", "", -25.00)
    add_bank(cfg.period_end, "INTEREST PAID", "", 18.37)
    add_bank(_d(cfg, 19), "RETURNED ITEM NSF - TRAILHEAD SUPPLY", "", -3_275.50)
    # c) Transposition: GL keyed 15,320.00; bank correctly shows 13,520.00
    add_gl(_d(cfg, 12), "Customer Payment", "PYMT3301", "RCPT-7301",
           "Receipt - Granite Peak Gear", 15_320.00, "1200")
    add_bank(_d(cfg, 12), "ACH CREDIT GRANITE PEAK GEAR", "RCPT-7301", 13_520.00)
    # d) Duplicate: utility ACH posted twice in GL, debited once by bank
    for doc in ("JE-UT-1017", "JE-UT-1017A"):
        add_gl(_d(cfg, 17), "Journal", doc, "UTIL-1017", "Valley Electric - Oct utilities",
               -6_412.88, "6600", source="Manual JE")
    add_bank(_d(cfg, 17), "ACH DEBIT VALLEY ELECTRIC", "UTIL-1017", -6_412.88)

    gl_df = pd.DataFrame(gl).sort_values(["posting_date", "gl_line_id"])
    bank_df = pd.DataFrame(bank).sort_values(["post_date", "bank_txn_id"])
    gl_df.to_csv(out / "gl_cash_detail.csv", index=False)
    bank_df.to_csv(out / "bank_statement.csv", index=False)
    pd.DataFrame([{"account": cfg.cash_account, "period": "2026-09",
                   "ending_balance_book": OPENING_CASH, "ending_balance_bank": OPENING_CASH,
                   "status": "Reconciled"}]).to_csv(out / "prior_period_balances.csv", index=False)

    _generate_ap(out, rng, cfg)
    _generate_card(out, rng, cfg)
    return {"gl_lines": len(gl_df), "bank_lines": len(bank_df)}


def _generate_ap(out: Path, rng: random.Random, cfg: CloseConfig) -> None:
    bills, seq = [], 600
    for _ in range(40):
        seq += 1
        bill_date = cfg.period_end - dt.timedelta(days=rng.randint(0, 120))
        terms = rng.choice([30, 30, 45, 60])
        bills.append({"bill_id": f"VB{seq}", "vendor": rng.choice(VENDORS),
                      "vendor_invoice": f"INV-{rng.randint(10000, 99999)}",
                      "bill_date": bill_date, "due_date": bill_date + dt.timedelta(days=terms),
                      "amount_open": _money(rng, 300, 22_000), "currency": cfg.currency})
    # Planted: same vendor invoice entered twice (re-keyed by a different clerk)
    dup = dict(bills[5])
    dup["bill_id"] = f"VB{seq + 1}"
    dup["bill_date"] = dup["bill_date"] + dt.timedelta(days=2)
    bills.append(dup)
    ap = pd.DataFrame(bills)
    ap.to_csv(out / "ap_open_bills.csv", index=False)

    # GL detail of the AP control account. Subledger-sourced activity ties to the
    # aging; one manual journal (audit-fee accrual) was posted directly to 2000.
    subledger_total = round(ap["amount_open"].sum(), 2)
    oct_bills = round(ap.loc[ap["bill_date"] >= cfg.period_start, "amount_open"].sum() + 186_420.55, 2)
    oct_payments = 211_904.12
    opening = round(subledger_total - oct_bills + oct_payments, 2)
    lines = [
        {"posting_date": cfg.period_start, "source": "Opening Balance", "document_number": "OB-2026-10",
         "memo": "Opening balance", "debit": 0.0, "credit": opening},
        {"posting_date": cfg.period_end, "source": "AP Bill", "document_number": "SUM-BILLS",
         "memo": "Vendor bills posted (summarized)", "debit": 0.0, "credit": oct_bills},
        {"posting_date": cfg.period_end, "source": "AP Payment", "document_number": "SUM-PMTS",
         "memo": "Bill payments applied (summarized)", "debit": oct_payments, "credit": 0.0},
        {"posting_date": cfg.period_end - dt.timedelta(days=3), "source": "Manual JE",
         "document_number": "JE-1188", "memo": "Accrue Q3 audit fees - Lindgren CPAs (fictional)",
         "debit": 0.0, "credit": 12_500.00},
    ]
    pd.DataFrame(lines).assign(account=cfg.ap_control_account).to_csv(
        out / "gl_ap_control_detail.csv", index=False)


def _generate_card(out: Path, rng: random.Random, cfg: CloseConfig) -> None:
    rows, seq = [], 0
    for _ in range(140):
        seq += 1
        cat = rng.choices(list(CARD_RANGES), weights=[2, 4, 3, 6, 1, 2])[0]
        txn_date = cfg.period_start + dt.timedelta(days=rng.randint(-5, 30))
        amt = _money(rng, *CARD_RANGES[cat])
        # Most spend is on approved reports (already posted to GL via the Concur
        # integration); late-month spend is often not yet submitted.
        late = txn_date >= cfg.period_end - dt.timedelta(days=8)
        status = rng.choices(["Approved", "Submitted", "Not Submitted"],
                             weights=[3, 2, 4] if late else [12, 1, 1])[0]
        rows.append({
            "card_txn_id": f"CC{9000 + seq}", "employee": rng.choice(EMPLOYEES),
            "card_last4": f"****{rng.randint(1000, 9999)}", "txn_date": txn_date,
            "posted_date": txn_date + dt.timedelta(days=rng.randint(1, 2)),
            "merchant": rng.choice(CARD_MERCHANTS[cat]), "expense_category": cat, "amount": amt,
            "expense_report_id": f"ER{400 + rng.randint(1, 60)}" if status != "Not Submitted" else "",
            "report_status": status,
            "receipt_attached": rng.random() < (0.93 if status == "Approved" else 0.6),
        })
    # Planted: aged unsubmitted items from September
    for k, days in enumerate((41, 52)):
        seq += 1
        d = cfg.period_end - dt.timedelta(days=days)
        rows.append({"card_txn_id": f"CC{9000 + seq}", "employee": "R. Okafor", "card_last4": "****4417",
                     "txn_date": d, "posted_date": d + dt.timedelta(days=1), "merchant": "Hilton",
                     "expense_category": "Lodging", "amount": 412.60 + k * 37, "expense_report_id": "",
                     "report_status": "Not Submitted", "receipt_attached": False})
    # Planted: possible split purchase to stay under the $500 single-transaction limit
    for amt in (389.00, 344.99):
        seq += 1
        d = cfg.period_end - dt.timedelta(days=6)
        rows.append({"card_txn_id": f"CC{9000 + seq}", "employee": "D. Kim", "card_last4": "****2290",
                     "txn_date": d, "posted_date": d + dt.timedelta(days=1), "merchant": "Amazon Business",
                     "expense_category": "Office Supplies", "amount": amt, "expense_report_id": "",
                     "report_status": "Not Submitted", "receipt_attached": True})
    pd.DataFrame(rows).sort_values("txn_date").to_csv(out / "concur_card_transactions.csv", index=False)
