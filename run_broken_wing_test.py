"""
Completes the broken-wing (call side widened to $9) SPY iron condor backtest.

Reads spy_condor_backtest.json (36 weeks, base condor data + confirmed
instrument IDs for the widened long call). For any week marked
status == "NEEDS_PULL", pulls that leg's hourly price history via the
Robinhood MCP get_option_historicals tool, fills it in, and writes the
completed file back out. Then runs the exit-rule comparison: original
$6-wide symmetric condor vs. $9-wide call wing, both under
hold-to-expiry + 2x-stop + Friday-3pm-close.

Usage (from Claude Code, with the Robinhood Agent MCP connector active):
    python run_broken_wing_test.py

The get_option_historicals calls are left as clearly marked TODOs since
Code calls MCP tools natively rather than importing them as a Python
library -- have Code fill in each call using its own Robinhood MCP
tool, exactly as documented in the docstring below, then re-run the
comparison section.
"""

import json
import sys

IN_PATH = "spy_condor_backtest.json"
OUT_PATH = "spy_condor_backtest_complete.json"

SLIPPAGE = 0.03  # per leg, matches original backtest assumption


def load(path):
    with open(path) as f:
        return json.load(f)


def weeks_needing_pull(data):
    """Return [(week_label, instrument_id, expiry, entry_date), ...] for
    every week whose broken-wing long-call leg hasn't been pulled yet."""
    out = []
    for wk, w in data["weeks"].items():
        bw = w["broken_wing_call9_extension"]
        if bw["status"] == "NEEDS_PULL":
            out.append((wk, bw["new_long_call_instrument_id"], w["expiry"], w["entry_date"]))
    return out


def print_pull_plan(data):
    """
    For each week needing a pull, call (via your Robinhood MCP tool):

        get_option_historicals(
            instrument_ids=[instrument_id],
            interval="hour",
            start_time=f"{entry_date}T14:00:00Z",   # ~Monday 9am ET; adjust
                                                       # to 15:00Z pre-DST
                                                       # (before ~Mar 8) if needed
            end_time=f"{expiry}T22:00:00Z",
        )

    Then take the returned bars' close_price in order and store:

        data["weeks"][wk]["broken_wing_call9_extension"]["entry_price"] = <first bar's open_price>
        data["weeks"][wk]["broken_wing_call9_extension"]["hourly_closes"] = [<close_price for each bar>]
        data["weeks"][wk]["broken_wing_call9_extension"]["status"] = "COMPLETE"

    Note: for short weeks (a Monday holiday, or a Thursday expiry), the
    number of bars will be fewer -- that's expected, same as the
    original dataset's varying bar counts.
    """
    plan = weeks_needing_pull(data)
    print(f"{len(plan)} weeks need a hourly-historicals pull:\n")
    for wk, iid, exp, ed in plan:
        print(f"  {wk}: instrument_id={iid}  entry={ed}  expiry={exp}")
    print(
        "\nFor each, call get_option_historicals with that instrument_id, "
        "interval='hour', start_time = entry_date ~14:00-15:00Z, "
        "end_time = expiry ~22:00Z. See this function's docstring for exact "
        "field mapping."
    )


def sim_week(entry, closes, settle_intrinsic_fn, stop_k=2.0, fri_close=True):
    """
    entry: net credit at entry (float)
    closes: list of per-hour net-package prices (cost to close), in order
    settle_intrinsic_fn: function(settle_price) -> intrinsic cost to close at expiry
    Returns weekly P&L in dollars for 1 contract (x100 multiplier).
    """
    n = len(closes)
    for i, mark in enumerate(closes):
        if fri_close and i == n - 2:  # second-to-last bar ~ Friday 3pm proxy
            return (entry - mark) * 100
        if stop_k and mark >= (1 + stop_k) * entry:
            return (entry - mark) * 100
    return None  # caller falls back to settle-based intrinsic close


def compare_structures(data):
    """
    Runs both structures under: hold to expiry, 2x-stop, close by Friday
    3pm-equivalent bar. Requires all 36 weeks' broken-wing legs to be
    status == COMPLETE.
    """
    incomplete = weeks_needing_pull(data)
    if incomplete:
        print(f"Cannot run comparison yet -- {len(incomplete)} weeks still NEEDS_PULL.")
        print_pull_plan(data)
        sys.exit(1)

    orig_total = 0.0
    bw_total = 0.0
    rows = []

    for wk, w in sorted(data["weeks"].items(), key=lambda kv: kv[0]):
        os_ = w["original_structure"]
        bw = w["broken_wing_call9_extension"]

        sp_e, lp_e, sc_e, lc_e = (
            os_["entry_prices"]["short_put"],
            os_["entry_prices"]["long_put"],
            os_["entry_prices"]["short_call"],
            os_["entry_prices"]["long_call"],
        )
        bw_lc_e = bw["entry_price"]

        orig_credit = sp_e - lp_e + sc_e - lc_e - SLIPPAGE
        bw_credit = sp_e - lp_e + sc_e - bw_lc_e - SLIPPAGE  # smaller credit, wider long call

        sp_h, lp_h, sc_h, lc_h = (
            os_["hourly_closes"]["short_put"],
            os_["hourly_closes"]["long_put"],
            os_["hourly_closes"]["short_call"],
            os_["hourly_closes"]["long_call"],
        )
        bw_lc_h = bw["hourly_closes"]

        n = min(len(sp_h), len(bw_lc_h))  # should match; guard just in case
        orig_marks = [a - b + c - d for a, b, c, d in zip(sp_h, lp_h, sc_h, lc_h)][:n]
        bw_marks = [a - b + c - d for a, b, c, d in zip(sp_h, lp_h, sc_h, bw_lc_h)][:n]

        K = w["original_structure"]["strikes"]
        settle = w["settle"]

        def intrinsic(strikes, S):
            return (
                max(0, strikes["short_put"] - S)
                - max(0, strikes["long_put"] - S)
                + max(0, S - strikes["short_call"])
                - max(0, S - strikes["long_call"])
            )

        orig_pnl = sim_week(orig_credit, orig_marks, None)
        if orig_pnl is None:
            orig_pnl = (orig_credit - intrinsic(K, settle)) * 100

        bw_K = dict(K)
        bw_K["long_call"] = bw["new_long_call_strike"]
        bw_pnl = sim_week(bw_credit, bw_marks, None)
        if bw_pnl is None:
            bw_pnl = (bw_credit - intrinsic(bw_K, settle)) * 100

        orig_total += orig_pnl
        bw_total += bw_pnl
        rows.append((wk, orig_credit * 100, orig_pnl, bw_credit * 100, bw_pnl))

    print(f"{'week':6}{'orig_cr':>9}{'orig_pnl':>10}{'bw_cr':>9}{'bw_pnl':>10}")
    for wk, oc, op, bc, bp in rows:
        print(f"{wk:6}{oc:9.0f}{op:10.0f}{bc:9.0f}{bp:10.0f}")

    print(f"\nOriginal ($6/$6 wings) total: {orig_total:+.0f}")
    print(f"Broken-wing ($6 put / $9 call) total: {bw_total:+.0f}")
    print(f"Difference: {bw_total - orig_total:+.0f}")


if __name__ == "__main__":
    data = load(IN_PATH)
    incomplete = weeks_needing_pull(data)
    if incomplete:
        print_pull_plan(data)
    else:
        compare_structures(data)
