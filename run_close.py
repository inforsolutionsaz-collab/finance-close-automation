"""CLI: python run_close.py [--generate] [--input data/input] [--output output]"""
import argparse
import logging
import sys

from data_gen import generate
from pipeline import run_close


def main() -> int:
    ap = argparse.ArgumentParser(description="Automated month-end close: bank rec, AP tie-out, card accrual")
    ap.add_argument("--generate", action="store_true", help="(Re)generate the synthetic sample extracts first")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--input", default=".")
    ap.add_argument("--output", default=".")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler("close_run.log", mode="w")])
    if args.generate:
        generate(args.input, seed=args.seed)
    r = run_close(args.input, args.output)
    b, a, c = r["bank"], r["ap"], r["card"]
    print(f"\n{r['company']} | period end {r['period_end']} | {r['run_id']}")
    print(f"  Cash rec : {b['status']}  diff {b['unreconciled_difference']:,.2f}  "
          f"auto-match {b['auto_match_rate_bank']:.1%} of {b['bank_lines']} bank lines")
    print(f"  AP tie   : {a['status']}  GL {a['gl_ap_balance']:,.2f} vs subledger {a['subledger_open_total']:,.2f}")
    print(f"  Card     : accrual {c['accrual_total']:,.2f} on {c['txns_accrued']} txns, "
          f"{c['policy_flags']} policy flags")
    print(f"  JEs      : {r['journal_entries']['count']} proposed, pending approval")
    for ctl in r["controls"]:
        print(f"  [{ctl['result']:^6}] {ctl['control']}")
    return 0 if all(x["result"] != "FAIL" for x in r["controls"]) else 1


if __name__ == "__main__":
    sys.exit(main())
