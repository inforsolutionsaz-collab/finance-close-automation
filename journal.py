"""Turn proposed adjustments into balanced journal entries in a NetSuite
CSV-import layout. Entries are proposals: they post only after reviewer approval."""
from __future__ import annotations

import pandas as pd

from config import CloseConfig

AUTO_REVERSE = {"CARD-ACCR"}


def build_journal_entries(lines: list, cfg: CloseConfig) -> pd.DataFrame:
    """lines: (group, purpose, account, signed_amount[+debit/-credit], memo)."""
    rows, je_no = [], {}
    for group, purpose, account, amount, memo in lines:
        key = (group, purpose)
        if key not in je_no:
            je_no[key] = f"{group}-{cfg.period_end:%Y%m}-{len(je_no) + 1:03d}"
        amount = round(float(amount), 2)
        if amount == 0:
            continue
        rows.append({
            "external_id": je_no[key], "tran_date": cfg.period_end, "subsidiary": cfg.subsidiary,
            "currency": cfg.currency, "account": str(account),
            "account_name": cfg.accounts.get(str(account), "UNMAPPED"),
            "debit": amount if amount > 0 else 0.0, "credit": -amount if amount < 0 else 0.0,
            "line_memo": memo, "je_purpose": purpose,
            "reversal_date": cfg.reversal_date if group in AUTO_REVERSE else "",
            "approval_status": "Pending Approval",
        })
    je = pd.DataFrame(rows).sort_values("external_id", kind="stable").reset_index(drop=True)
    validate_balanced(je)
    return je


def validate_balanced(je: pd.DataFrame) -> None:
    if je.empty:
        return
    totals = je.groupby("external_id")[["debit", "credit"]].sum().round(2)
    bad = totals[(totals["debit"] - totals["credit"]).abs() > 0.005]
    if not bad.empty:
        raise ValueError(f"Unbalanced journal entries:\n{bad}")
    if (je["account_name"] == "UNMAPPED").any():
        raise ValueError("Journal lines reference accounts missing from the chart of accounts")
