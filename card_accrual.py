"""Corporate card (Concur) accrual and policy-compliance checks.

Card spend on approved expense reports is already expensed through the Concur
integration. Anything incurred by period end that is not yet approved is a
liability without an expense: accrue it by GL account, auto-reversing next period.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from config import CloseConfig


@dataclass
class CardResult:
    accrual_detail: pd.DataFrame
    accrual_by_account: pd.DataFrame
    policy_flags: pd.DataFrame
    summary: dict
    journal_lines: list = field(default_factory=list)


def build_card_accrual(card: pd.DataFrame, cfg: CloseConfig) -> CardResult:
    card = card.copy()
    card["txn_date"] = pd.to_datetime(card["txn_date"])
    card["receipt_attached"] = card["receipt_attached"].astype(str).str.lower().isin(["true", "1", "yes"])
    end = pd.Timestamp(cfg.period_end)
    in_period = card[card["txn_date"] <= end].copy()
    in_period["gl_account"] = in_period["expense_category"].map(cfg.card_category_accounts)

    unmapped = in_period[in_period["gl_account"].isna()]
    if not unmapped.empty:
        raise ValueError(f"Unmapped expense categories: {sorted(unmapped['expense_category'].unique())}")

    to_accrue = in_period[in_period["report_status"] != "Approved"].copy()
    to_accrue["age_days"] = (end - to_accrue["txn_date"]).dt.days
    by_acct = (to_accrue.groupby("gl_account")["amount"].agg(["count", "sum"]).reset_index()
               .rename(columns={"count": "transactions", "sum": "accrual_amount"}))
    by_acct["account_name"] = by_acct["gl_account"].map(cfg.accounts)
    by_acct["accrual_amount"] = by_acct["accrual_amount"].round(2)

    # ---- Policy / control flags ----
    flags = []
    for _, r in in_period.iterrows():
        if r["amount"] > cfg.receipt_required_over and not r["receipt_attached"]:
            flags.append({**_brief(r), "flag": f"Missing receipt (> ${cfg.receipt_required_over:,.0f})"})
        if r["report_status"] == "Not Submitted" and (end - r["txn_date"]).days > cfg.aged_unsubmitted_days:
            flags.append({**_brief(r), "flag": f"Unsubmitted > {cfg.aged_unsubmitted_days} days"})
    grp = in_period.groupby(["employee", "merchant", in_period["txn_date"].dt.date])
    for _, g in grp:
        if len(g) > 1 and g["amount"].sum() > cfg.single_txn_limit and \
                (g["amount"] <= cfg.single_txn_limit).all():
            for _, r in g.iterrows():
                flags.append({**_brief(r), "flag": f"Possible split purchase (total ${g['amount'].sum():,.2f})"})
    flags = pd.DataFrame(flags)

    jl = []
    for _, a in by_acct.iterrows():
        jl.append(("CARD-ACCR", "Accrue unreported corporate card spend", a["gl_account"],
                   a["accrual_amount"], f"Card spend not on approved report ({a['transactions']} txns)"))
    total = round(float(by_acct["accrual_amount"].sum()), 2)
    jl.append(("CARD-ACCR", "Accrue unreported corporate card spend", cfg.accrual_account, -total,
               "Accrued corporate card liability - auto-reverse"))

    summary = {
        "card_txns_in_period": int(len(in_period)),
        "card_spend_in_period": round(float(in_period["amount"].sum()), 2),
        "already_expensed_via_approved_reports": round(
            float(in_period.loc[in_period["report_status"] == "Approved", "amount"].sum()), 2),
        "txns_accrued": int(len(to_accrue)), "accrual_total": total,
        "policy_flags": int(len(flags)),
        "employees_with_flags": int(flags["employee"].nunique()) if len(flags) else 0,
    }
    return CardResult(to_accrue.sort_values("txn_date"), by_acct, flags, summary, jl)


def _brief(r) -> dict:
    return {"card_txn_id": r["card_txn_id"], "employee": r["employee"], "txn_date": r["txn_date"].date(),
            "merchant": r["merchant"], "amount": r["amount"], "report_status": r["report_status"]}
