"""Close configuration: period, chart of accounts, matching tolerances and rules.

Everything a controller would want to tune lives here, so policy changes do not
require code changes.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CloseConfig:
    company: str = "Summit Outdoor Co. (fictional)"
    subsidiary: str = "Summit Outdoor US"
    period_start: dt.date = dt.date(2026, 10, 1)
    period_end: dt.date = dt.date(2026, 10, 31)
    currency: str = "USD"

    # --- Chart of accounts (NetSuite-style numbering) ---
    accounts: dict = field(default_factory=lambda: {
        "1010": "Cash - Operating (First National)",
        "1200": "Accounts Receivable",
        "2000": "Accounts Payable",
        "2100": "Accrued Liabilities",
        "2300": "Payroll Liabilities",
        "6100": "Travel - Airfare & Ground",
        "6200": "Meals & Entertainment",
        "6300": "Software & Subscriptions",
        "6400": "Office Supplies",
        "6500": "Travel - Lodging",
        "6600": "Utilities",
        "6900": "Bank Fees",
        "7100": "Interest Income",
    })
    cash_account: str = "1010"
    ap_control_account: str = "2000"
    accrual_account: str = "2100"

    # --- Bank matching tolerances ---
    date_window_days: int = 3          # amount+date match window
    batch_window_days: int = 2         # lockbox / batch deposit window
    max_batch_size: int = 5            # max GL receipts combined into one deposit
    outstanding_check_window_days: int = 10
    deposit_in_transit_window_days: int = 3
    amount_tolerance: float = 0.005    # rounding tolerance

    # Bank-only items: (keyword regex, classification, GL account for the JE)
    bank_rules: tuple = (
        (r"SERVICE CHARGE|WIRE FEE|ANALYSIS FEE", "Bank fee not recorded", "6900"),
        (r"INTEREST", "Interest not recorded", "7100"),
        (r"RETURNED ITEM|NSF", "NSF / returned customer check", "1200"),
    )

    # --- Corporate card (Concur) ---
    card_category_accounts: dict = field(default_factory=lambda: {
        "Airfare": "6100",
        "Ground Transport": "6100",
        "Lodging": "6500",
        "Meals": "6200",
        "Software": "6300",
        "Office Supplies": "6400",
    })
    receipt_required_over: float = 75.00
    single_txn_limit: float = 500.00
    aged_unsubmitted_days: int = 30

    # --- AP ---
    ap_reclass_account: str = "2100"   # where manual accruals hitting AP belong
    duplicate_bill_window_days: int = 7

    @property
    def reversal_date(self) -> dt.date:
        return self.period_end + dt.timedelta(days=1)


DEFAULT_CONFIG = CloseConfig()
