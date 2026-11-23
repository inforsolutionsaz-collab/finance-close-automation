"""Audit-ready Excel reconciliation workpaper.

The Close Summary is built with live SUMIFS formulas that point at the detail
tabs, so a reviewer or auditor can trace every number to supporting lines.
"""
from __future__ import annotations

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

NAVY = "1F3A5F"
HEAD_FILL = PatternFill("solid", fgColor=NAVY)
SECTION_FILL = PatternFill("solid", fgColor="DCE6F1")
INPUT_FONT = Font(color="0000FF")          # hard-coded source figures in blue
OK_FILL = PatternFill("solid", fgColor="E2F0D9")
BAD_FILL = PatternFill("solid", fgColor="FBE5D6")
NUM = '#,##0.00;(#,##0.00);"-"'
THIN = Side(style="thin", color="999999")


def _write_table(wb, title, df, widths=None):
    ws = wb.create_sheet(title)
    cols = list(df.columns) if not df.empty else ["(no items)"]
    for c, name in enumerate(cols, 1):
        cell = ws.cell(row=1, column=c, value=name.replace("_", " ").title())
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for r, row in enumerate(df.itertuples(index=False), 2):
        for c, val in enumerate(row, 1):
            if isinstance(val, pd.Timestamp):
                val = val.date()
            if isinstance(val, float) and pd.isna(val):
                val = None
            cell = ws.cell(row=r, column=c, value=val)
            if isinstance(val, float):
                cell.number_format = NUM
            elif hasattr(val, "year"):
                cell.number_format = "yyyy-mm-dd"
    for c, name in enumerate(cols, 1):
        w = max([len(str(name))] + [len(str(v)) for v in (df[name].head(200) if not df.empty else [])])
        ws.column_dimensions[get_column_letter(c)].width = min(max(10, w + 2), 48)
    ws.freeze_panes = "A2"
    if not df.empty:
        ws.auto_filter.ref = ws.dimensions
    letters = {name: get_column_letter(i) for i, name in enumerate(cols, 1)}
    return ws, letters


def build_workpaper(path, cfg, run_meta, bank, ap, card, je, controls):
    wb = Workbook()
    summary_ws = wb.active
    summary_ws.title = "Close Summary"

    _, m = _write_table(wb, "Bank Matches", bank.matches)
    _, glx = _write_table(wb, "GL Unmatched", bank.gl_exceptions)
    _, bkx = _write_table(wb, "Bank Unmatched", bank.bank_exceptions)
    _, var = _write_table(wb, "Amount Variances", bank.variances)
    _, aga = _write_table(wb, "AP Aging", ap.aging)
    _, apr = _write_table(wb, "AP Recon Items", ap.reconciling_items)
    _write_table(wb, "AP Duplicate Bills", ap.duplicate_bills)
    _write_table(wb, "Card Accrual Detail", card.accrual_detail)
    _, cba = _write_table(wb, "Card Accrual by Acct", card.accrual_by_account)
    _write_table(wb, "Card Policy Flags", card.policy_flags)
    _, jel = _write_table(wb, "Proposed JEs", je)
    _write_table(wb, "Control Log", pd.DataFrame(controls))

    ws = summary_ws
    ws.column_dimensions["A"].width = 4
    ws.column_dimensions["B"].width = 52
    ws.column_dimensions["C"].width = 20
    ws.column_dimensions["D"].width = 44
    ws["B1"] = f"{cfg.company} - Month-End Close Workpaper"
    ws["B1"].font = Font(bold=True, size=14, color=NAVY)
    ws["B2"] = f"Period ending {cfg.period_end:%B %d, %Y}  |  Run {run_meta['run_id']}  |  {run_meta['run_at']}"
    ws["B3"] = "Blue = figure from source extract; black = formula over detail tabs."
    ws["B3"].font = Font(italic=True, size=9, color="666666")
    row = 5

    def section(title):
        nonlocal row
        for col in "BCD":
            ws[f"{col}{row}"].fill = SECTION_FILL
        ws[f"B{row}"] = title
        ws[f"B{row}"].font = Font(bold=True, color=NAVY)
        row += 1

    def line(label, value, note="", bold=False, src=False, top=False):
        nonlocal row
        ws[f"B{row}"] = label
        c = ws[f"C{row}"]
        c.value = value
        c.number_format = NUM
        if src:
            c.font = INPUT_FONT
        if bold:
            ws[f"B{row}"].font = Font(bold=True)
            c.font = Font(bold=True, color="0000FF" if src else "000000")
        if top:
            c.border = Border(top=THIN)
        ws[f"D{row}"] = note
        ws[f"D{row}"].font = Font(size=9, color="666666")
        row += 1
        return f"C{row - 1}"

    def sumifs(sheet, letters, sum_col, crit_col, crit):
        return (f"=SUMIFS('{sheet}'!{letters[sum_col]}:{letters[sum_col]},"
                f"'{sheet}'!{letters[crit_col]}:{letters[crit_col]},\"{crit}\")")

    s = bank.summary
    section(f"1. Cash reconciliation - GL {cfg.cash_account} vs bank statement")
    b0 = line("Balance per bank statement", s["balance_per_bank"], "Bank statement ending balance", bold=True, src=True)
    b1 = line("Add: deposits in transit", sumifs("GL Unmatched", glx, "amount", "classification", "Deposit in transit"), "GL Unmatched tab")
    b2 = line("Less: outstanding checks", sumifs("GL Unmatched", glx, "amount", "classification", "Outstanding check"), "GL Unmatched tab")
    adj_bank = line("Adjusted bank balance", f"={b0}+{b1}+{b2}", bold=True, top=True)
    row += 1
    k0 = line("Balance per books (GL)", s["balance_per_books"], "NetSuite GL detail, account 1010", bold=True, src=True)
    k1 = line("Less: bank fees not recorded", sumifs("Bank Unmatched", bkx, "amount", "classification", "Bank fee not recorded"), "Bank Unmatched tab")
    k2 = line("Add: interest not recorded", sumifs("Bank Unmatched", bkx, "amount", "classification", "Interest not recorded"), "Bank Unmatched tab")
    k3 = line("Less: NSF / returned items", sumifs("Bank Unmatched", bkx, "amount", "classification", "NSF / returned customer check"), "Bank Unmatched tab")
    k4 = line("+/- Book errors: amount variances",
              f"=SUM('Amount Variances'!{var['difference']}:{var['difference']})" if "difference" in var else 0,
              "Amount Variances tab")
    k5 = line("+/- Book errors: duplicate postings reversed",
              "=-" + sumifs("GL Unmatched", glx, "amount", "classification", "Book error - duplicate posting")[1:],
              "GL Unmatched tab")
    adj_book = line("Adjusted book balance", f"=SUM({k0}:{k5})", bold=True, top=True)
    row += 1
    diff = line("Unreconciled difference", f"=ROUND({adj_bank}-{adj_book},2)", "Must be zero", bold=True)
    line("Auto-match rate (bank lines)", s["auto_match_rate_bank"], "Matched without manual work")
    ws[f"C{row - 1}"].number_format = "0.0%"
    row += 1

    a = ap.summary
    section(f"2. AP subledger tie-out - GL {cfg.ap_control_account}")
    g = line("GL AP control balance", a["gl_ap_balance"], "GL account 2000 detail", bold=True, src=True)
    sl = line("AP subledger open bills", f"=SUM('AP Aging'!{aga['amount_open']}:{aga['amount_open']})", "AP Aging tab")
    d1 = line("Difference", f"={g}-{sl}", top=True)
    d2 = line("Explained: manual JEs to AP control",
              f"=SUM('AP Recon Items'!{apr['net_credit']}:{apr['net_credit']})" if "net_credit" in apr else 0,
              "AP Recon Items tab - reclass to 2100")
    line("Unexplained AP difference", f"=ROUND({d1}-{d2},2)", "Must be zero", bold=True, top=True)
    line("Duplicate bill exposure (held)", a["duplicate_exposure"], "AP Duplicate Bills tab")
    row += 1

    c = card.summary
    section("3. Corporate card accrual (Concur)")
    line("Card spend incurred through period end", c["card_spend_in_period"], "Concur card feed", src=True)
    line("Already expensed (approved reports)", c["already_expensed_via_approved_reports"], "Posted via Concur integration", src=True)
    line("Accrual - not yet approved", f"=SUM('Card Accrual by Acct'!{cba['accrual_amount']}:{cba['accrual_amount']})",
         f"Auto-reverses {cfg.reversal_date:%m/%d/%Y}", bold=True)
    line("Policy exceptions flagged", c["policy_flags"], "Card Policy Flags tab")
    ws[f"C{row - 1}"].number_format = "0"
    row += 1

    section("4. Proposed journal entries")
    line("Total debits", f"=SUM('Proposed JEs'!{jel['debit']}:{jel['debit']})")
    line("Total credits", f"=SUM('Proposed JEs'!{jel['credit']}:{jel['credit']})")
    line("Entries", je["external_id"].nunique())
    ws[f"C{row - 1}"].number_format = "0"
    row += 1

    section("5. Automated controls")
    for ctl in controls:
        ws[f"B{row}"] = ctl["control"]
        ws[f"C{row}"] = ctl["result"]
        ws[f"C{row}"].fill = OK_FILL if ctl["result"] == "PASS" else BAD_FILL
        ws[f"D{row}"] = ctl["detail"]
        ws[f"D{row}"].font = Font(size=9, color="666666")
        row += 1
    row += 1

    section("6. Sign-off")
    for label in ("Prepared by (automated): closeauto pipeline", "Reviewed by:", "Review date:",
                  "Approved for posting:"):
        ws[f"B{row}"] = label
        ws[f"C{row}"].border = Border(bottom=THIN)
        row += 1
    ws.freeze_panes = "A5"
    wb.save(path)
    return diff
