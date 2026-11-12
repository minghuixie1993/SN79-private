# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_23_1Gpt, on stub states (no simulator).

    python agents/Agent_10_23/tests/test_10_23_1Gpt.py

Runs every MyAgent10_23_Gpt test against MyAgent10_23_1Gpt (T00 with the new size check off must still reproduce
MyAgentV3 exactly), then the checks for what 10_23_1Gpt adds (U01-U06).
"""
import math
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_gpt_10_23 as base                                     # noqa: E402
import MyAgent10_23_1Gpt as nmod                                # noqa: E402

# The 10_23 suite, pointed at the 10_23_1Gpt class.
base.gmod = types.SimpleNamespace(MyAgent10_23_Gpt=nmod.MyAgent10_23_1Gpt,
                                  _REQUEST_ACCOUNTS=base.gmod._REQUEST_ACCOUNTS)
base.neutral_gpt = lambda: base.make(nmod.MyAgent10_23_1Gpt, max_order_fraction=0, inventory_sizing=0,
                                     post_fill_band=0, keep_size_tolerance=0)
check, make, N, SEC = base.check, base.make, base.N, base.SEC
BUY, SELL = base.BUY, base.SELL
Agent = nmod.MyAgent10_23_1Gpt


def u01_oversize_replaced():
    # Inventory gap +0.15: bids shrink to 1 - 0.8 x 0.6 = 52% of V3's size. A bid resting exactly at the planned
    # price but at V3's full size is too big for the plan and must be replaced; within 30% it is kept.
    mid, wealth = 300.0, 50_000.0
    initial = 0.3 * wealth / mid
    units = initial + 0.15 * wealth / mid
    bk = base.book(mid, 3)
    probe = make(Agent)
    planned = [c for c in base.places(base.quote_once(probe, bk, base.account(units, wealth - units * mid, initial)))
               if c[2] == 0][0]
    results = {}
    for label, size in (("oversize", planned[3] / 0.52), ("close", planned[3] * 1.2)):
        g = make(Agent)
        resting = base.Order(5, BUY, planned[4], round(size, 4), 900 * SEC)
        calls = base.quote_once(g, bk, base.account(units, wealth - units * mid, initial, [resting]))
        results[label] = (calls, g.plan_stats["v1"].get("replaced_oversize", 0))
    over_calls, over_n = results["oversize"]
    close_calls, close_n = results["close"]
    replaced = any(c[0] == "cancel" and 5 in c[2] for c in over_calls) and \
        any(c[0] == "place" and c[2] == 0 and abs(c[3] - planned[3]) < 1e-9 for c in over_calls)
    kept = not any(c[0] == "cancel" and 5 in c[2] for c in close_calls)
    check("U01 a resting bid 1.9x the planned size is replaced; one 1.2x the size is kept",
          replaced and over_n == 1 and kept and close_n == 0,
          f"planned {planned[3]}; oversize -> {over_calls} ({over_n}); 1.2x -> {close_calls} ({close_n})")


def u02_smaller_kept():
    # A resting bid at the planned price but half the size (what is left after a partial fill) is kept.
    g = make(Agent)
    bk = base.book(300.0, 3)
    first = base.quote_once(g, bk, base.account(83.33, 25_000.0, 83.33))
    bid = [c for c in base.places(first) if c[2] == 0][0]
    ask = [c for c in base.places(first) if c[2] == 1][0]
    resting = [base.Order(1, BUY, bid[4], bid[3] / 2, 1000 * SEC), base.Order(2, SELL, ask[4], ask[3], 1000 * SEC)]
    second = base.quote_once(g, bk, base.account(83.33, 25_000.0, 83.33, resting), t=1001 * SEC)
    check("U02 a partly filled (smaller) resting quote keeps its queue place", second == [], str(second))


def u03_nan_prediction():
    g = make(Agent)
    bk = base.book(300.0, 3)
    resting = [base.Order(1, BUY, 299.8, 0.5, 900 * SEC), base.Order(2, SELL, 300.2, 0.5, 900 * SEC)]
    g.accounts = {0: base.account(83.33, 25_000.0, 83.33, resting)}
    base.gmod._REQUEST_ACCOUNTS.set(None)
    memory = g._remember("v1", 0, bk, 1000 * SEC)
    memory["variance"] = float("nan")
    memory["ofi"] = float("inf")
    resp = base.Resp()
    g._quote_book(resp, "v1", 0, bk, memory)
    ok_reset = memory["variance"] is None and memory["ofi"] == 0.0
    check("U03 non-finite memory inputs are reset, and the round runs normally (resting quotes kept)",
          ok_reset and g.plan_stats["v1"].get("non_finite", 0) == 0 and g.plan_stats["v1"].get("keep") == 2,
          f"{resp.calls}, stats {g.plan_stats['v1']}")
    # A fair price that cannot be put on the grid (infinite fundamental gap) cancels instead of raising.
    g2 = make(Agent, max_pred_bps=float("inf"))
    g2.accounts = {0: base.account(83.33, 25_000.0, 83.33, resting)}
    bk2 = base.book(300.0, 3, fundamental=float("inf"))
    memory2 = g2._remember("v1", 0, bk2, 1000 * SEC)
    resp2 = base.Resp()
    raised = None
    try:
        g2._quote_book(resp2, "v1", 0, bk2, memory2)
    except Exception as ex:
        raised = ex
    finite = all(math.isfinite(c[4]) and math.isfinite(c[3]) for c in base.places(resp2.calls))
    check("U03 an infinite fundamental: no exception, prediction neutralised, every price finite",
          raised is None and memory2["pred"] == 0.0 and finite, f"raised {raised!r}, calls {resp2.calls}")


def u04_rejection_attributed():
    g = make(Agent)
    bk = base.book(300.0, 3)
    calls = base.quote_once(g, bk, base.account(83.33, 25_000.0, 83.33), validator="vA")
    ask = [c for c in base.places(calls) if c[2] == 1][0]
    g.onOrderRejected(N(bookId=0, side=1, price=ask[4], quantity=ask[3], message="CONTRACT_VIOLATION",
                        type="RL", clientOrderId=None))
    stats = g.plan_stats.get("vA", {})
    check("U04 a rejection is booked against the validator that placed the order",
          stats.get("rejected") == 1 and stats.get("rejected:CONTRACT_VIOLATION:sell") == 1, str(stats))


def u05_fast_fill_counted():
    g = make(Agent)
    bk = base.book(300.0, 3)
    calls = base.quote_once(g, bk, base.account(83.33, 25_000.0, 83.33), t=1000 * SEC)
    bid = [c for c in base.places(calls) if c[2] == 0][0]
    # Filled in full 0.4 s later, before any state shows it resting.
    g.onTrade(N(makerAgentId=1, takerAgentId=9, bookId=0, timestamp=1000 * SEC + 400_000_000, price=bid[4],
                quantity=bid[3], side=SELL, makerFee=0.0, makerOrderId=777), "v1")
    base.quote_once(g, bk, base.account(83.33 + bid[3], 25_000.0 - bid[3] * bid[4], 83.33), t=1001 * SEC)
    s = g.fill_stats.get(("v1", 0, "buy"), {})
    check("U05 an order filled before it is ever seen resting counts as filled, with its time to fill",
          s.get("ended") == 1 and s.get("ended_filled") == 1 and s.get("time_to_fill") == [0.4], str(s))


def u06_default_limits_hold():
    # On the 10_23 regression market with defaults: cap, band and guard still hold, oversize replacements happen.
    import random
    rng = random.Random(5)
    g = make(Agent)
    worst_cap, worst_band = 0.0, -1.0
    for r in range(400):
        mid = 300 * math.exp(rng.gauss(0, 0.01))
        wealth = 50_000.0
        gap = rng.uniform(-0.3, 0.3)
        initial = 0.3 * wealth / mid
        units = initial + gap * wealth / mid
        resting = [base.Order(r * 2 + 1, BUY, round(mid * 0.9995, 2), rng.uniform(0.2, 2.0), 900 * SEC)]
        acct = base.account(units, wealth - units * mid, initial, resting)
        bk = base.book(mid, 3)
        calls = base.quote_once(g, bk, acct, t=(1000 + r) * SEC)
        book_mid = (bk.bids[0].price + bk.asks[0].price) / 2
        w = units * book_mid + (wealth - units * mid)
        agent_gap = (units - initial) * book_mid / w
        for c in base.places(calls):
            worst_cap = max(worst_cap, c[3] * c[4] / (g.max_order_fraction * w))
            adds = agent_gap if c[2] == 0 else -agent_gap
            worst_band = max(worst_band, adds + c[3] * c[4] / w)
    check("U06 defaults with random resting bids: cap and post-fill band hold",
          worst_cap <= 1 + 1e-9 and worst_band <= 0.25 + 1e-9,
          f"cap x{worst_cap:.4f}, gap after fill {worst_band:.4f}, oversize replaced {g.plan_stats['v1'].get('replaced_oversize', 0)}")


if __name__ == "__main__":
    base.t00_t07_t09_t13()
    tests = (base.t01, base.t02, base.t03, base.t04, base.t05, base.t06, base.t08, base.t09_extreme, base.t10,
             base.t11, base.t12, base.t14, base.default_regression_limits,
             u01_oversize_replaced, u02_smaller_kept, u03_nan_prediction, u04_rejection_attributed,
             u05_fast_fill_counted, u06_default_limits_hold)
    for test in tests:
        try:
            test()
        except Exception as ex:
            import traceback
            traceback.print_exc()
            check(test.__name__, False, f"raised {ex!r}")
    failed = [n for n, ok, _ in base.RESULTS if not ok]
    print(f"\n{len(base.RESULTS) - len(failed)}/{len(base.RESULTS)} passed")
    sys.exit(1 if failed else 0)
