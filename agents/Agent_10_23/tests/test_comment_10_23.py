# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_23_Comment, on stub states (no simulator).

    python agents/Agent_10_23/tests/test_comment_10_23.py

C01 the order cap holds over the minimum order, C02-C03 a replacement is planned before its quote is cancelled,
C04 quoting time counts only while a quote rests, C05-C06 the token-bucket pacing, C07 the centered fundamental,
C08 invariants over random rounds (cross guard, minimum and cap, one order per side).
"""
import math
import os
import random
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                                      # agents/Agent_10_23
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))   # repo

import MyAgent10_23_Comment as cmod                              # noqa: E402
from taos.im.protocol.models import OrderDirection as D          # noqa: E402

N = types.SimpleNamespace
BUY, SELL = D.BUY, D.SELL
SEC = 1_000_000_000
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


# ── stubs ────────────────────────────────────────────────────────────────────────────────────────────────────

CLASSES = [N(name="simulation_0", books=list(range(96))), N(name="simulation_1", books=list(range(96, 128)))]


class Resp:
    def __init__(self):
        self.calls = []

    def limit_order(self, **k):
        self.calls.append(("place", k["book_id"], int(k["direction"]), round(k["quantity"], 6), round(k["price"], 6)))

    def cancel_orders(self, book_id, ids):
        self.calls.append(("cancel", book_id, tuple(ids)))


def make(min_order=0.0, wealth=50_000.0, **params):
    agent = object.__new__(cmod.MyAgent10_23_Comment)
    agent.config = N(**params)
    agent.initialize()
    agent.uid = 1
    agent._exchange_mode = False
    agent.asset_classes = lambda: CLASSES
    agent.price_decimals = lambda b: 2
    agent.volume_decimals = lambda b: 4
    agent.min_order_size = lambda b: min_order
    agent.config_for = lambda b: N(miner_wealth=wealth)
    agent.make_response = lambda: Resp()
    agent.accounts = {}
    return agent


class Order:
    def __init__(self, oid, side, price, quantity):
        self.id, self.side, self.price, self.quantity = oid, side, price, quantity


def account(base, quote, initial=None, orders=(), fee=0.0):
    locked_quote = sum(o.quantity * o.price for o in orders if o.side == BUY)
    locked_base = sum(o.quantity for o in orders if o.side == SELL)
    return N(base_balance=N(total=base, free=base - locked_base, initial=base if initial is None else initial),
             quote_balance=N(total=quote, free=quote - locked_quote),
             orders=list(orders), fees=N(maker_fee_rate=fee, taker_fee_rate=0.0), traded_volume=0.0)


def book(mid, spread_ticks=3, fundamental=None, tick=0.01):
    bid = round(mid - spread_ticks * tick / 2, 2)
    ask = round(bid + spread_ticks * tick, 2)
    return N(bids=[N(price=round(bid - i * tick, 2), quantity=5.0) for i in range(5)],
             asks=[N(price=round(ask + i * tick, 2), quantity=5.0) for i in range(5)],
             events=[], fundamental=fundamental)


def run(agent, bk, acct, book_id=0, t=1000 * SEC):
    agent.accounts = {book_id: acct}
    return agent.respond(N(books={book_id: bk}, timestamp=t, dendrite=N(hotkey="v1"))).calls


def places(calls):
    return [c for c in calls if c[0] == "place"]


# ── tests ────────────────────────────────────────────────────────────────────────────────────────────────────

def c01_cap_over_minimum():
    # Book 100 (class 1, ~30): wealth 200 QUOTE caps an order at 0.02 x 200 / 30 = 0.13 BASE, below the 2.5364 minimum.
    small = make(min_order=2.5364, wealth=200.0)
    calls = run(small, book(30.0), account(3.3, 100.0), book_id=100)
    # The default book: 50k wealth caps an order at 1000 QUOTE, far above the ~300 QUOTE quote.
    big = make(min_order=0.25)
    big_calls = run(big, book(300.0), account(83.0, 25_000.0))
    worst = max((c[3] * c[4] for c in places(big_calls)), default=0.0)
    check("C01 minimum order above the cap: nothing placed; normal book stays under the cap",
          not places(calls) and places(big_calls) and worst <= 0.02 * 50_000 + 1e-6,
          f"small book {calls}, normal book largest {worst:.1f} QUOTE")


def c02_unaffordable_replacement_keeps_safe_quote():
    # A bid resting 3% under the market, far beyond `away`, so it is due for replacement; no QUOTE is free beyond
    # it, the replacement at ~299.9 affords 290/299.9 = 0.967 BASE, below the 0.98 minimum. The quote is safe (it
    # drifted away from the fair price), so it is kept and nothing is cancelled.
    agent = make(min_order=0.98, budget_sizing=0)
    resting = Order(11, BUY, 290.0, 1.0)
    calls = run(agent, book(300.0), account(100.0, 290.0, orders=[resting]))   # wealth ~30k: cap 2 BASE, above the minimum
    bid_calls = [c for c in calls if (c[0] == "cancel" and 11 in c[2]) or (c[0] == "place" and c[2] == int(BUY))]
    check("C02 replacement unaffordable: the safe resting bid is kept, not cancelled", not bid_calls, f"{calls}")


def c03_risky_quote_cancelled_and_affordable_replaced():
    # A bid drifted toward the fair price (just under the ask) and smaller than today's minimum order, so no legal
    # replacement fits its funds: it is cancelled, because that is the quote the market runs over. (A quote drifted
    # toward the fair price otherwise always affords its replacement: the bid is priced above its target.)
    agent = make(min_order=1.0, budget_sizing=0)
    risky = Order(12, BUY, 300.0, 0.99)
    calls = run(agent, book(300.0, spread_ticks=4), account(100.0, 297.0, orders=[risky]))
    cancelled = any(c[0] == "cancel" and 12 in c[2] for c in calls)
    bid_placed = [c for c in places(calls) if c[2] == int(BUY)]
    # With funds, the replacement is placed and sized down to what the funds pay for, at least the minimum.
    agent2 = make(min_order=0.25, budget_sizing=0)
    calls2 = run(agent2, book(300.0), account(100.0, 100.0, orders=[Order(13, BUY, 290.0, 0.3)]))
    bids2 = [c for c in places(calls2) if c[2] == int(BUY)]
    check("C03 risky unaffordable quote cancelled; affordable replacement sized to the funds",
          cancelled and not bid_placed and bids2 and bids2[0][3] * bids2[0][4] <= 100.0 + 87.0 + 1e-6,
          f"risky: {calls}; funded: {bids2}")


def c04_quoting_time_needs_a_resting_quote():
    agent = make(min_order=0.25)
    run(agent, book(300.0), account(0.0, 0.0))                     # nothing to quote with
    empty = agent.memory[("v1", 0)]["quoting"]
    run(agent, book(300.0), account(83.0, 25_000.0), t=1001 * SEC)  # funded
    funded = agent.memory[("v1", 0)]["quoting"]
    check("C04 quoting flag: false with no quote resting, true once one rests", empty is False and funded is True,
          f"empty {empty}, funded {funded}")


def c05_token_bucket_pauses_and_refills():
    agent = make()
    acct = N(traded_volume=0.0)
    memory = {"volume": {}, "timestamp": 0, "tokens": None, "token_time": None}
    rate = 0.9 * 10 * 50_000 / 86_400                     # QUOTE per second
    capacity = rate * 60
    paused0 = agent._over_volume_pace(0, acct, memory)
    memory["tokens"] -= capacity + 50                      # fills spend the whole bucket and a bit more
    memory["timestamp"] = 5 * SEC
    paused1 = agent._over_volume_pace(0, acct, memory)
    memory["timestamp"] = 20 * SEC                         # 15 s more refill: 15 x rate > 50 QUOTE overdraft
    paused2 = agent._over_volume_pace(0, acct, memory)
    memory["timestamp"] = 10_000 * SEC                     # a long wait refills only up to the bucket
    agent._over_volume_pace(0, acct, memory)
    check("C05 token bucket: full at start, pauses when spent, resumes after refill, holds at most 60 s",
          not paused0 and paused1 and not paused2 and abs(memory["tokens"] - capacity) < 1e-6,
          f"capacity {capacity:.1f} QUOTE, tokens {memory['tokens']:.1f}")


def c06_token_bucket_spreads_quoting():
    # A book that fills 3x faster than the allowance when quoting: over an hour the bucket keeps it quoting at the
    # allowance's average rate (0.9 / 3 of the time) in pauses of seconds; the five-minute period, which allows
    # `pace_burst` (2x) the average rate, quotes longer but pauses for over a minute at a time.
    def simulate(**params):
        agent = make(**params)
        acct = N(traded_volume=0.0)
        memory = {"volume": {}, "timestamp": 0, "tokens": None, "token_time": None}
        fill_rate = 3 * 0.9 * 10 * 50_000 / 86_400          # QUOTE per second while quoting
        quoting, longest_gap, gap = 0, 0, 0
        for s in range(3600):
            memory["timestamp"] = s * SEC
            if agent._over_volume_pace(0, acct, memory):
                gap += 1
                longest_gap = max(longest_gap, gap)
                continue
            gap = 0
            quoting += 1
            period = memory["timestamp"] // agent.pace_period
            memory["volume"][period] = memory["volume"].get(period, 0.0) + fill_rate
            if memory["tokens"] is not None:
                memory["tokens"] -= fill_rate
        return quoting, longest_gap
    tq, tg = simulate()
    pq, pg = simulate(pace_tokens=0)
    check("C06 token bucket quotes in short pauses, the period budget in long ones",
          tg <= 10 and pg > 60 and 0.25 * 3600 <= tq <= 0.4 * 3600,
          f"bucket: quoting {tq}s, longest pause {tg}s; period: quoting {pq}s, longest pause {pg}s")


def c07_centered_fundamental():
    # A book whose fundamental sits 1% above the mid for 20 minutes: the raw gap predicts +4 bps (0.04 x 100 bps) the
    # whole time, the centered gap decays toward 0 as its 600 s average catches up.
    def final_pred(**params):
        agent = make(min_order=0.25, **params)
        for s in range(0, 1200, 2):
            run(agent, book(300.0, fundamental=303.0), account(83.0, 25_000.0), t=(1000 + s) * SEC)
        return agent.memory[("v1", 0)]["pred"]
    raw, centered = final_pred(), final_pred(center_fundamental=1)
    check("C07 persistent fundamental offset: raw gap keeps leaning, centered gap does not",
          abs(raw - 4.0) < 0.6 and abs(centered) < 0.6, f"raw {raw:+.2f} bps, centered {centered:+.2f} bps")


def c08_invariants(rounds=400, seed=11):
    rng = random.Random(seed)
    agent = make(min_order=0.25)
    mids = {0: 300.0, 7: 301.0, 100: 30.0}
    violations, per_side, sizes = 0, 0, []
    for r in range(rounds):
        bks, accts = {}, {}
        for b in mids:
            mids[b] *= math.exp(rng.gauss(0, 0.0005))
            bks[b] = book(mids[b], rng.choice([1, 2, 3, 4]), fundamental=mids[b] * (1 + rng.gauss(0, 0.003)))
            accts[b] = account(25_000 / mids[b] * rng.uniform(0.6, 1.4), 25_000.0, initial=25_000 / mids[b])
        agent.accounts = accts
        calls = agent.respond(N(books=bks, timestamp=(1000 + r) * SEC, dendrite=N(hotkey="v1"))).calls
        for b in mids:
            guard = (3 if b < 96 else 1) * 0.01
            bp = [c for c in places(calls) if c[1] == b]
            per_side += sum(1 for side in (int(BUY), int(SELL)) if sum(c[2] == side for c in bp) > 1)
            for c in bp:
                best_bid, best_ask = bks[b].bids[0].price, bks[b].asks[0].price
                if (c[2] == int(BUY) and c[4] > best_ask - guard + 1e-6) or (c[2] == int(SELL) and c[4] < best_bid + guard - 1e-6):
                    violations += 1
                sizes.append(c[3])
                wealth = accts[b].base_balance.total * mids[b] + accts[b].quote_balance.total
                if c[3] < 0.25 or c[3] * mids[b] > 0.02 * wealth * 1.01:
                    violations += 1
    check("C08 400 random rounds: cross guard, minimum and cap hold, at most one order per side",
          violations == 0 and per_side == 0 and sizes, f"{len(sizes)} orders, violations {violations}")


if __name__ == "__main__":
    for test in (c01_cap_over_minimum, c02_unaffordable_replacement_keeps_safe_quote,
                 c03_risky_quote_cancelled_and_affordable_replaced, c04_quoting_time_needs_a_resting_quote,
                 c05_token_bucket_pauses_and_refills, c06_token_bucket_spreads_quoting, c07_centered_fundamental,
                 c08_invariants):
        try:
            test()
        except Exception as ex:
            import traceback
            traceback.print_exc()
            check(test.__name__, False, f"raised {ex!r}")
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
