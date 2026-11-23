"""Bank-to-GL cash reconciliation with a multi-pass matching engine.

Pass 1  Reference match (check number / remittance ref); flags amount variances
Pass 2  Amount + date window (unique closest candidate)
Pass 3  Many-to-one batch match (lockbox deposits = sum of several GL receipts)
Then    Rule-based classification of whatever is left (timing, bank-only,
        book errors, unexplained) and the standard two-sided reconciliation.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

import pandas as pd

from config import CloseConfig


@dataclass
class BankReconResult:
    matches: pd.DataFrame
    gl_exceptions: pd.DataFrame
    bank_exceptions: pd.DataFrame
    variances: pd.DataFrame
    summary: dict
    journal_lines: list = field(default_factory=list)


def _prep(gl: pd.DataFrame, bank: pd.DataFrame):
    gl = gl.copy()
    bank = bank.copy()
    gl["posting_date"] = pd.to_datetime(gl["posting_date"])
    bank["post_date"] = pd.to_datetime(bank["post_date"])
    gl["reference"] = gl["reference"].fillna("").astype(str)
    bank["reference"] = bank["reference"].fillna("").astype(str)
    gl["amount"] = (gl["debit"].fillna(0) - gl["credit"].fillna(0)).round(2)
    bank["amount"] = bank["amount"].round(2)
    gl["matched"] = False
    bank["matched"] = False
    return gl, bank


def reconcile_bank(gl_raw: pd.DataFrame, bank_raw: pd.DataFrame, opening_book: float,
                   opening_bank: float, cfg: CloseConfig) -> BankReconResult:
    gl, bank = _prep(gl_raw, bank_raw)
    tol = cfg.amount_tolerance
    matches, variances = [], []

    def record(bank_idx, gl_idxs, rule):
        bank.loc[bank_idx, "matched"] = True
        gl.loc[gl_idxs, "matched"] = True
        for gi in gl_idxs:
            matches.append({"match_rule": rule, "bank_txn_id": bank.at[bank_idx, "bank_txn_id"],
                            "bank_date": bank.at[bank_idx, "post_date"].date(),
                            "bank_amount": bank.at[bank_idx, "amount"],
                            "gl_line_id": gl.at[gi, "gl_line_id"],
                            "gl_date": gl.at[gi, "posting_date"].date(),
                            "gl_amount": gl.at[gi, "amount"],
                            "reference": gl.at[gi, "reference"],
                            "description": gl.at[gi, "description"]})

    # ---- Pass 1: reference ----
    for bi, b in bank[bank["reference"] != ""].iterrows():
        cands = gl[(~gl["matched"]) & (gl["reference"] == b["reference"])]
        if cands.empty:
            continue
        exact = cands[(cands["amount"] - b["amount"]).abs() <= tol]
        if not exact.empty:
            record(bi, [exact.index[0]], "1-Reference + amount")
        elif len(cands) == 1:
            gi = cands.index[0]
            record(bi, [gi], "1-Reference, amount variance")
            diff = round(b["amount"] - gl.at[gi, "amount"], 2)
            variances.append({
                "bank_txn_id": b["bank_txn_id"], "gl_line_id": gl.at[gi, "gl_line_id"],
                "reference": b["reference"], "bank_amount": b["amount"],
                "gl_amount": gl.at[gi, "amount"], "difference": diff,
                "likely_cause": "Transposition (difference divisible by 9)"
                if round(abs(diff) * 100) % 900 == 0 else "Keying / amount error",
                "offset_account": gl.at[gi, "offset_account"],
                "description": gl.at[gi, "description"]})

    # ---- Pass 2: amount + date window ----
    for bi, b in bank[~bank["matched"]].iterrows():
        open_gl = gl[~gl["matched"]]
        cands = open_gl[((open_gl["amount"] - b["amount"]).abs() <= tol) &
                        ((open_gl["posting_date"] - b["post_date"]).dt.days.abs()
                         <= cfg.date_window_days)]
        if not cands.empty:
            gi = (cands["posting_date"] - b["post_date"]).dt.days.abs().idxmin()
            record(bi, [gi], "2-Amount + date window")

    # ---- Pass 3: many-to-one batch deposits ----
    for bi, b in bank[(~bank["matched"]) & (bank["amount"] > 0)].iterrows():
        open_gl = gl[(~gl["matched"]) & (gl["amount"] > 0)]
        lag = (b["post_date"] - open_gl["posting_date"]).dt.days
        cands = open_gl[(lag >= 0) & (lag <= cfg.batch_window_days)]
        if len(cands) < 2 or len(cands) > 18:
            continue
        found = None
        for size in range(2, min(cfg.max_batch_size, len(cands)) + 1):
            for combo in itertools.combinations(cands.index, size):
                if abs(round(gl.loc[list(combo), "amount"].sum(), 2) - b["amount"]) <= tol:
                    found = list(combo)
                    break
            if found:
                break
        if found:
            record(bi, found, "3-Batch deposit (many-to-one)")

    # ---- Classify unmatched GL lines ----
    matched_keys = set(zip(gl.loc[gl["matched"], "reference"], gl.loc[gl["matched"], "amount"]))
    gl_exc = []
    for gi, g in gl[~gl["matched"]].iterrows():
        days_to_end = (pd.Timestamp(cfg.period_end) - g["posting_date"]).days
        if g["reference"] and (g["reference"], g["amount"]) in matched_keys:
            cls, action = "Book error - duplicate posting", "Reverse duplicate entry"
        elif g["amount"] < 0 and g["document_type"] == "Check" and \
                days_to_end <= cfg.outstanding_check_window_days:
            cls, action = "Outstanding check", "Timing - confirm clears next period"
        elif g["amount"] > 0 and days_to_end <= cfg.deposit_in_transit_window_days:
            cls, action = "Deposit in transit", "Timing - confirm credit next period"
        else:
            cls, action = "Unexplained - GL only", "Investigate"
        gl_exc.append({**g.drop(labels=["matched"]).to_dict(), "classification": cls,
                       "action": action, "days_outstanding": days_to_end})
    gl_exc = pd.DataFrame(gl_exc)

    # ---- Classify unmatched bank lines ----
    bank_exc = []
    for _, b in bank[~bank["matched"]].iterrows():
        cls, acct = "Unexplained - bank only", None
        for pattern, label, account in cfg.bank_rules:
            if re.search(pattern, b["description"], re.I):
                cls, acct = label, account
                break
        bank_exc.append({**b.drop(labels=["matched"]).to_dict(), "classification": cls,
                         "je_account": acct})
    bank_exc = pd.DataFrame(bank_exc)
    variances = pd.DataFrame(variances)

    # ---- Two-sided reconciliation ----
    def s(df, cls=None, col="amount"):
        if df.empty:
            return 0.0
        d = df if cls is None else df[df["classification"] == cls]
        return round(float(d[col].sum()), 2)

    book_end = round(opening_book + gl["amount"].sum(), 2)
    bank_end = round(opening_bank + bank["amount"].sum(), 2)
    dit = s(gl_exc, "Deposit in transit")
    osc = s(gl_exc, "Outstanding check")            # negative number
    dup = s(gl_exc, "Book error - duplicate posting")
    var_total = s(variances, col="difference")
    bank_only_explained = round(s(bank_exc) - s(bank_exc, "Unexplained - bank only"), 2)

    adj_bank = round(bank_end + dit + osc, 2)
    adj_book = round(book_end + bank_only_explained + var_total - dup, 2)
    unexplained = round(s(gl_exc, "Unexplained - GL only") + s(bank_exc, "Unexplained - bank only"), 2)
    diff = round(adj_bank - adj_book, 2)

    summary = {
        "opening_balance_book": opening_book, "opening_balance_bank": opening_bank,
        "balance_per_bank": bank_end, "deposits_in_transit": dit,
        "outstanding_checks": osc, "adjusted_bank_balance": adj_bank,
        "balance_per_books": book_end,
        "bank_fees_not_recorded": s(bank_exc, "Bank fee not recorded"),
        "interest_not_recorded": s(bank_exc, "Interest not recorded"),
        "nsf_returned_items": s(bank_exc, "NSF / returned customer check"),
        "book_error_amount_variances": var_total,
        "book_error_duplicates_reversed": round(-dup, 2),
        "adjusted_book_balance": adj_book, "unreconciled_difference": diff,
        "unexplained_items_amount": unexplained,
        "gl_lines": int(len(gl)), "bank_lines": int(len(bank)),
        "bank_lines_auto_matched": int(bank["matched"].sum()),
        "gl_lines_auto_matched": int(gl["matched"].sum()),
        "auto_match_rate_bank": round(float(bank["matched"].mean()), 4),
        "status": "RECONCILED" if abs(diff) <= cfg.amount_tolerance and unexplained == 0
        else "EXCEPTIONS - REVIEW REQUIRED",
    }

    # ---- Proposed adjusting entries (book side only) ----
    jl = []
    cash = cfg.cash_account
    for _, b in bank_exc[bank_exc["je_account"].notna()].iterrows() if not bank_exc.empty else []:
        amt = b["amount"]
        jl.append(("BANK-ADJ", b["classification"], cash, amt, f"{b['description']} ({b['bank_txn_id']})"))
        jl.append(("BANK-ADJ", b["classification"], b["je_account"], -amt,
                   f"{b['description']} ({b['bank_txn_id']})"))
    for _, v in variances.iterrows() if not variances.empty else []:
        jl.append(("BANK-ERR", "Correct amount variance", cash, v["difference"],
                   f"Correct {v['reference']}: book {v['gl_amount']:,.2f} vs bank {v['bank_amount']:,.2f}"))
        jl.append(("BANK-ERR", "Correct amount variance", v["offset_account"], -v["difference"],
                   f"Correct {v['reference']}"))
    dups = gl_exc[gl_exc["classification"] == "Book error - duplicate posting"] if not gl_exc.empty else []
    for _, d in (dups.iterrows() if len(dups) else []):
        jl.append(("BANK-DUP", "Reverse duplicate posting", cash, -d["amount"],
                   f"Reverse duplicate {d['document_number']} ({d['gl_line_id']})"))
        jl.append(("BANK-DUP", "Reverse duplicate posting", d["offset_account"], d["amount"],
                   f"Reverse duplicate {d['document_number']}"))

    return BankReconResult(pd.DataFrame(matches), gl_exc, bank_exc, variances, summary, jl)
