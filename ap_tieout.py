"""AP subledger-to-GL control account tie-out, aging and duplicate-bill control."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from config import CloseConfig

AGING_BUCKETS = [(-10_000, 0, "Current"), (1, 30, "1-30"), (31, 60, "31-60"),
                 (61, 90, "61-90"), (91, 10_000, "90+")]


@dataclass
class APResult:
    aging: pd.DataFrame
    aging_summary: pd.DataFrame
    reconciling_items: pd.DataFrame
    duplicate_bills: pd.DataFrame
    summary: dict
    journal_lines: list = field(default_factory=list)


def _bucket(days_past_due: int) -> str:
    for lo, hi, name in AGING_BUCKETS:
        if lo <= days_past_due <= hi:
            return name
    return "90+"


def tie_out_ap(bills: pd.DataFrame, gl_ap: pd.DataFrame, cfg: CloseConfig) -> APResult:
    bills = bills.copy()
    for c in ("bill_date", "due_date"):
        bills[c] = pd.to_datetime(bills[c])
    end = pd.Timestamp(cfg.period_end)
    bills["days_past_due"] = (end - bills["due_date"]).dt.days
    bills["aging_bucket"] = bills["days_past_due"].apply(_bucket)

    order = [b[2] for b in AGING_BUCKETS]
    aging_summary = (bills.groupby("aging_bucket")["amount_open"].agg(["count", "sum"])
                     .reindex(order, fill_value=0).reset_index()
                     .rename(columns={"count": "bills", "sum": "amount_open"}))

    # Duplicate bill control: same vendor + same vendor invoice, or same vendor +
    # same amount within N days.
    dup_rows = []
    srt = bills.sort_values("bill_date")
    for _, grp in srt.groupby("vendor"):
        rows = grp.to_dict("records")
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                same_inv = a["vendor_invoice"] == b["vendor_invoice"]
                same_amt = abs(a["amount_open"] - b["amount_open"]) < 0.01 and \
                    abs((b["bill_date"] - a["bill_date"]).days) <= cfg.duplicate_bill_window_days
                if same_inv or same_amt:
                    dup_rows.append({"vendor": a["vendor"], "original_bill": a["bill_id"],
                                     "suspect_bill": b["bill_id"], "vendor_invoice": b["vendor_invoice"],
                                     "amount": b["amount_open"],
                                     "rule": "Same vendor invoice #" if same_inv else "Same amount within window",
                                     "action": "Hold payment; void suspect bill if confirmed"})
    duplicates = pd.DataFrame(dup_rows)

    gl_ap = gl_ap.copy()
    gl_ap["net_credit"] = (gl_ap["credit"].fillna(0) - gl_ap["debit"].fillna(0)).round(2)
    gl_balance = round(float(gl_ap["net_credit"].sum()), 2)
    subledger_total = round(float(bills["amount_open"].sum()), 2)

    # Entries that hit the control account without going through the subledger
    non_sub = gl_ap[gl_ap["source"].isin(["Manual JE", "Journal"])].copy()
    non_sub["classification"] = "Manual JE posted to AP control (bypasses subledger)"
    reconciling = non_sub[["posting_date", "document_number", "memo", "net_credit", "classification"]]
    explained = round(float(reconciling["net_credit"].sum()), 2)
    difference = round(gl_balance - subledger_total, 2)
    unexplained = round(difference - explained, 2)

    jl = []
    for _, r in non_sub.iterrows():
        jl.append(("AP-RECLASS", "Reclass manual accrual out of AP control", cfg.ap_control_account,
                   r["net_credit"], f"Reclass {r['document_number']}: {r['memo']}"))
        jl.append(("AP-RECLASS", "Reclass manual accrual out of AP control", cfg.ap_reclass_account,
                   -r["net_credit"], f"Reclass {r['document_number']}"))

    summary = {
        "gl_ap_balance": gl_balance, "subledger_open_total": subledger_total,
        "difference": difference, "explained_by_manual_jes": explained,
        "unexplained_difference": unexplained,
        "open_bills": int(len(bills)),
        "past_due_amount": round(float(bills.loc[bills["days_past_due"] > 0, "amount_open"].sum()), 2),
        "duplicate_bills_flagged": int(len(duplicates)),
        "duplicate_exposure": round(float(duplicates["amount"].sum()), 2) if len(duplicates) else 0.0,
        "status": "TIED OUT" if abs(unexplained) < 0.01 else "UNEXPLAINED DIFFERENCE",
    }
    aging = bills.sort_values(["days_past_due"], ascending=False)
    return APResult(aging, aging_summary, reconciling, duplicates, summary, jl)
