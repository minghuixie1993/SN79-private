# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_23_2Gpt, on stub states (no simulator).

    python agents/Agent_10_23/tests/test_10_23_2Gpt.py

Runs every MyAgent10_23_Gpt and MyAgent10_23_1Gpt test against MyAgent10_23_2Gpt, comparing with the MyAgentV3
embedded in the 2Gpt file (T00: with every switch off, exactly V3's instructions; also checked against
agents/MyAgentV3.py itself), then the checks for what 2Gpt adds (V01-V09).
"""
import decimal
import math
import os
import subprocess
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_gpt_10_23 as base                                     # noqa: E402
import test_10_23_1Gpt as onegpt                                  # noqa: E402  (its module rebinds base; rebound below)
import MyAgent10_23_2Gpt as tmod                                  # noqa: E402

NEUTRAL = dict(max_order_fraction=0, inventory_sizing=0, post_fill_band=0, keep_size_tolerance=0, grid_nudge=0,
               fee_weight=1, max_fee_rate=0.002, pace_period_s=3600, expiry_s=60, expiry_s_c1=60,
               max_lean=float("inf"), book_adapt_rate=0)
Agent = tmod.MyAgent10_23_2Gpt
base.v3mod = tmod                      # the V3 embedded in the 2Gpt file is the parent to compare with
base.gmod = types.SimpleNamespace(MyAgent10_23_Gpt=Agent, _REQUEST_ACCOUNTS=tmod._REQUEST_ACCOUNTS)
base.neutral_gpt = lambda: base.make(Agent, **NEUTRAL)
onegpt.nmod = types.SimpleNamespace(MyAgent10_23_1Gpt=Agent)
onegpt.Agent = Agent
check, make, N, SEC = base.check, base.make, base.N, base.SEC
BUY, SELL = base.BUY, base.SELL


class RawResp:
    """Keeps the arguments exactly as sent."""
    def __init__(self):
        self.orders, self.cancels = [], []

    def limit_order(self, **k):
        self.orders.append(k)

    def cancel_orders(self, book_id, ids):
        self.cancels.append((book_id, tuple(ids)))


def engine_grid(price, decimals):
    """What the engine keeps: the double as an exact decimal, truncated to 8 places (double2decimal), then to the
    price grid (util::round, also a truncation)."""
    d = decimal.Decimal(price).quantize(decimal.Decimal(1).scaleb(-8), rounding=decimal.ROUND_DOWN)
    return d.quantize(decimal.Decimal(1).scaleb(-decimals), rounding=decimal.ROUND_DOWN)


def t00_against_repo_v3():
    """The embedded V3 must still be agents/MyAgentV3.py: same regression against the repo's own copy."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))     # agents/
    import MyAgentV3 as repo_v3
    saved = base.v3mod
    base.v3mod = repo_v3
    try:
        _, _, mismatches, _, _ = base.regression(rounds=200, seed=17)
    finally:
        base.v3mod = saved
    check("T00b neutral 2Gpt reproduces agents/MyAgentV3.py itself over 200 rounds", mismatches == 0,
          f"{mismatches} rounds differ")


def v01_grid_nudge():
    g = make(Agent)
    worst = {0: 0, 100: 0}
    for book_id, lo, hi in ((0, 25000, 35000), (100, 2500, 3500)):
        for cents in range(lo, hi):
            intended = round(cents / 100, 2)
            r = RawResp()
            g._place(r, book_id, BUY, 1.0, intended)
            kept = engine_grid(r.orders[0]["price"], 2)
            if kept != decimal.Decimal(cents).scaleb(-2):
                worst[book_id] += 1
    plain = sum(engine_grid(round(c / 100, 2), 2) != decimal.Decimal(c).scaleb(-2) for c in range(25000, 35000))
    check("V01 every nudged price survives the engine's truncation on its intended tick (10k + 1k prices)",
          worst[0] == 0 and worst[100] == 0, f"misses {worst}; without the nudge {plain}/10000 class 0 prices drop a tick")


def v02_fees():
    bk = base.book(300.0, 3)
    acct_fee = base.account(83.33, 25_000.0, 83.33)
    acct_fee.fees.maker_fee_rate = 0.015
    g = make(Agent)
    with_fee = base.places(base.quote_once(g, bk, acct_fee))
    g0 = make(Agent)
    without = base.places(base.quote_once(g0, bk, base.account(83.33, 25_000.0, 83.33)))
    v3 = make(tmod.MyAgentV3)
    acct_v3 = base.account(83.33, 25_000.0, 83.33)
    acct_v3.fees.maker_fee_rate = 0.015
    v3_calls = base.quote_once(v3, bk, acct_v3)
    widened = max(abs(a[4] - b[4]) for a, b in zip(sorted(with_fee), sorted(without))) if with_fee and without else None
    # fee_weight 0.005 x 1.5% = 0.75 bps of half spread = 0.0225 at 300, plus up to a tick of grid rounding.
    check("V02 a 1.5% maker fee: still quoting, each quote only ~0.75 bps (2-3 ticks) wider; V3 pauses",
          len(with_fee) == 2 and widened is not None and 0.01 <= widened <= 0.0325 + 1e-9 and not base.places(v3_calls),
          f"2Gpt {with_fee} vs no fee {without}; V3 {v3_calls}")


def v03_pacing():
    g = make(Agent)
    bk = base.book(300.0, 3)
    t0 = 3_000 * SEC
    base.quote_once(g, bk, base.account(83.33, 25_000.0, 83.33), t=t0)
    # 2 x the even 5-minute share of a 500k daily cap is ~3472 QUOTE: fill 3600 of it.
    for i in range(12):
        g.onTrade(N(makerAgentId=1, takerAgentId=-3, bookId=0, timestamp=t0 + i * SEC, price=300.0, quantity=1.0,
                    side=SELL, makerFee=0.0, makerOrderId=1000 + i), "v1")
    paused = base.quote_once(g, bk, base.account(83.33, 25_000.0, 83.33), t=t0 + 20 * SEC)
    next_period = (t0 // (300 * SEC) + 1) * 300 * SEC + SEC
    resumed = base.quote_once(g, bk, base.account(83.33, 25_000.0, 83.33), t=next_period)
    check("V03 a 5-minute period over its pace pauses the book, and the next period quotes again",
          not base.places(paused) and base.places(resumed), f"paused {paused}; next period {resumed}")


def v04_expiry():
    g = make(Agent)
    r0, r1 = RawResp(), RawResp()
    g._place(r0, 0, BUY, 1.0, 299.9)
    g._place(r1, 100, BUY, 3.0, 29.9)
    check("V04 expiry 60 s on class 0 and 300 s on class 1",
          r0.orders[0]["expiryPeriod"] == 60 * SEC and r1.orders[0]["expiryPeriod"] == 300 * SEC,
          f"{r0.orders[0]['expiryPeriod']}, {r1.orders[0]['expiryPeriod']}")


def v05_bounded_lean():
    worst = -1.0
    for gap, skewed in ((0.10, 0.0), (-0.10, 0.0), (0.10, 0.2), (-0.10, -0.2)):
        g = make(Agent, max_pred_bps=50, lean=20)
        mid, wealth = 300.0, 50_000.0
        initial = 0.3 * wealth / mid
        units = initial - skewed * wealth / mid           # a short inventory leans the skew up as well
        bk = base.book(mid, 30, fundamental=mid * (1 + gap))
        calls = base.places(base.quote_once(g, bk, base.account(units, wealth - units * mid, initial)))
        book_mid = (bk.bids[0].price + bk.asks[0].price) / 2
        for c in calls:
            across = (c[4] - book_mid) if c[2] == 0 else (book_mid - c[4])
            worst = max(worst, across / book_mid * 1e4)
    check("V05 a huge predicted move plus skew: no quote is placed beyond the mid (by more than rounding)",
          worst <= 0.34, f"worst bid above / ask below mid: {worst:.2f} bps")


def v06_book_multiplier():
    g = make(Agent)
    for book_id in (0, 7):
        base.quote_once(g, base.book(300.0, 3), base.account(83.33, 25_000.0, 83.33), book_id=book_id, t=1000 * SEC)
    # Book 0's buys keep marking out -10 bps; book 7's +5 bps.
    for i in range(60):
        t = (1000 + i) * SEC
        for book_id, after in ((0, 299.7), (7, 300.15)):
            g.pending_markouts.setdefault("v1", []).append((t, book_id, 1, 300.0, 300.0, 300.0))
            g.memory[("v1", book_id)]["mid"] = after
            g.memory[("v1", book_id)]["timestamp"] = t + 21 * SEC
        g._settle_markouts("v1", t + 21 * SEC)
    m0 = g.memory[("v1", 0)].get("book_mult", 1.0)
    m7 = g.memory[("v1", 7)].get("book_mult", 1.0)
    check("V06 a book marking out badly widens on its own, a good one stays at x1",
          1.05 < m0 <= 2.0 and m7 == 1.0, f"book 0 x{m0:.3f}, book 7 x{m7:.3f}")


def v07_counterparties():
    g = make(Agent)
    base.quote_once(g, base.book(300.0, 3), base.account(83.33, 25_000.0, 83.33), t=1000 * SEC)
    fills = [(-4, 100.0)] + [(7, 100.0)] * 9            # 100 background, 900 from miners
    for i, (taker, notional) in enumerate(fills):
        g.onTrade(N(makerAgentId=1, takerAgentId=taker, bookId=0, timestamp=(1000 + i) * SEC, price=100.0,
                    quantity=notional / 100.0, side=SELL, makerFee=0.0, makerOrderId=500 + i), "v1")
    background, miners, s3 = g._counterparty_mix("v1", 0, 1010 * SEC)
    card = g._plan_card("v1")
    check("V07 counterparty mix: 10% background, S3 = (100 + 400) / 1000 = 0.50, shown on the card",
          background == 100 and miners == 900 and abs(s3 - 0.5) < 1e-9 and "S3 ~0.50" in card, card[-160:])


def v08_self_contained():
    with open(tmod.__file__) as fh:
        source = fh.read()
    imports_siblings = any(line.startswith(("from MyAgent", "import MyAgent")) for line in source.splitlines())
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE)))))
    proc = subprocess.run([sys.executable, "-c", "import importlib.util,sys;"
                           f"s=importlib.util.spec_from_file_location('x',{tmod.__file__!r});"
                           "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.MyAgent10_23_2Gpt.__mro__[1].__name__)"],
                          capture_output=True, text=True, cwd="/", env=env)
    check("V08 self-contained: no imports of sibling agent files, loads from any directory",
          not imports_siblings and proc.returncode == 0 and "MyAgent10_23_1Gpt" in proc.stdout,
          (proc.stdout.strip() or proc.stderr.strip()[-200:]))


def v09_default_limits_hold():
    # Cap, post-fill band and cross guard on 300 random inventories with the 2Gpt defaults.
    import random
    rng = random.Random(9)
    g = make(Agent)
    worst_cap, worst_band, guard_bad = 0.0, -1.0, 0
    for r in range(300):
        mid = 300 * math.exp(rng.gauss(0, 0.01))
        wealth = 50_000.0
        gap = rng.uniform(-0.3, 0.3)
        initial = 0.3 * wealth / mid
        units = initial + gap * wealth / mid
        bk = base.book(mid, rng.choice([1, 2, 3, 4]), fundamental=mid * (1 + rng.gauss(0, 0.01)))
        calls = base.quote_once(g, bk, base.account(units, wealth - units * mid, initial), t=(1000 + r) * SEC)
        book_mid = (bk.bids[0].price + bk.asks[0].price) / 2
        w = units * book_mid + (wealth - units * mid)
        agent_gap = (units - initial) * book_mid / w
        for c in base.places(calls):
            worst_cap = max(worst_cap, c[3] * c[4] / (g.max_order_fraction * w))
            worst_band = max(worst_band, (agent_gap if c[2] == 0 else -agent_gap) + c[3] * c[4] / w)
            if (c[2] == 0 and c[4] > bk.asks[0].price - 0.03 + 1e-9) or (c[2] == 1 and c[4] < bk.bids[0].price + 0.03 - 1e-9):
                guard_bad += 1
    check("V09 2Gpt defaults, 300 random states: cap, post-fill band and cross guard hold",
          worst_cap <= 1 + 1e-9 and worst_band <= 0.25 + 1e-9 and guard_bad == 0,
          f"cap x{worst_cap:.4f}, gap after fill {worst_band:.4f}, guard violations {guard_bad}")


if __name__ == "__main__":
    base.t00_t07_t09_t13()
    t00_against_repo_v3()
    tests = (base.t01, base.t02, base.t03, base.t04, base.t05, base.t06, base.t08, base.t09_extreme, base.t10,
             base.t11, base.t12, base.t14, base.default_regression_limits,
             onegpt.u01_oversize_replaced, onegpt.u02_smaller_kept, onegpt.u03_nan_prediction,
             onegpt.u04_rejection_attributed, onegpt.u05_fast_fill_counted, onegpt.u06_default_limits_hold,
             v01_grid_nudge, v02_fees, v03_pacing, v04_expiry, v05_bounded_lean, v06_book_multiplier,
             v07_counterparties, v08_self_contained, v09_default_limits_hold)
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
