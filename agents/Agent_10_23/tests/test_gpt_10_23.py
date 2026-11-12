# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_23_Gpt, on stub states (no simulator).

    python agents/Agent_10_23/tests/test_gpt_10_23.py

T00 runs MyAgentV3 and MyAgent10_23_Gpt with the Gpt limits switched off on the same 400 rounds of a random
market (resting orders, fills and markouts carried between rounds) and requires identical instructions, so any
difference seen later comes from the new limits. T01-T14 are the review's scenarios.
"""
import math
import os
import random
import sys
import threading
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                      # agents/Agent_10_23
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))     # agents (MyAgentV3.py, the reference)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))   # repo

import MyAgent10_23_Gpt as gmod                                  # noqa: E402
# MyAgent10_23_Gpt derives from FinanceAgent with MyAgentV3's strategy merged in, so the reference it must match
# with its limits off is agents/MyAgentV3.py itself.
import MyAgentV3 as v3mod                                        # noqa: E402
from taos.im.agents import FinanceAgent                          # noqa: E402
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


def make(cls, **params):
    agent = object.__new__(cls)
    agent.config = N(**params)
    agent.initialize()
    agent.uid = 1
    agent.asset_classes = lambda: CLASSES
    agent.price_decimals = lambda b: 2
    agent.volume_decimals = lambda b: 4
    agent.min_order_size = lambda b: 2.5364 if b >= 96 else 0.0
    agent.config_for = lambda b: N(miner_wealth=50_000.0)
    agent.make_response = lambda: Resp()
    agent.accounts = {}
    return agent


def neutral_gpt():
    return make(gmod.MyAgent10_23_Gpt, max_order_fraction=0, inventory_sizing=0, post_fill_band=0)


class Resp:
    def __init__(self):
        self.calls = []

    def limit_order(self, **k):
        self.calls.append(("place", k["book_id"], int(k["direction"]), round(k["quantity"], 6), round(k["price"], 6)))

    def cancel_orders(self, book_id, ids):
        self.calls.append(("cancel", book_id, tuple(ids)))


class Order:
    def __init__(self, oid, side, price, quantity, timestamp):
        self.id, self.side, self.price, self.quantity, self.timestamp = oid, side, price, quantity, timestamp


def account(base, quote, initial, orders=(), fee=0.0, traded=0.0):
    locked_quote = sum(o.quantity * o.price for o in orders if o.side == BUY)
    locked_base = sum(o.quantity for o in orders if o.side == SELL)
    return N(base_balance=N(total=base, free=base - locked_base, initial=initial),
             quote_balance=N(total=quote, free=quote - locked_quote),
             orders=list(orders), fees=N(maker_fee_rate=fee), traded_volume=traded)


def level(p, q):
    return N(price=p, quantity=q)


def book(mid, spread_ticks=3, fundamental=None, bid_q=5.0, ask_q=5.0, depth=5, tick=0.01):
    half = spread_ticks * tick / 2
    bid = round(mid - half, 2)
    ask = round(bid + spread_ticks * tick, 2)
    return N(bids=[level(round(bid - i * tick, 2), bid_q) for i in range(depth)],
             asks=[level(round(ask + i * tick, 2), ask_q) for i in range(depth)],
             events=[], fundamental=fundamental)


def state(books, timestamp, validator="v1"):
    return N(books=books, timestamp=timestamp, dendrite=N(hotkey=validator))


def quote_once(agent, bk, acct, book_id=0, t=1000 * SEC, validator="v1"):
    """One round on one book; returns the instruction list."""
    agent.accounts = {book_id: acct}
    gmod._REQUEST_ACCOUNTS.set(None)
    resp = agent.respond(state({book_id: bk}, t, validator))
    return resp.calls


# ── T00: regression against V3 ───────────────────────────────────────────────────────────────────────────────

def regression(rounds=400, seed=7):
    rng = random.Random(seed)
    v3, g = make(v3mod.MyAgentV3), neutral_gpt()
    books = {0: 300.0, 7: 301.5, 40: 299.0, 96: 29.6, 110: 30.2}
    wallets = {b: {"base": 50_000 / 2 / m, "quote": 25_000.0, "initial": 50_000 / 2 / m} for b, m in books.items()}
    resting = {b: [] for b in books}
    next_id = [1]
    mismatches, max_instr, guard_violations = 0, 0, 0
    for r in range(rounds):
        t = (700 + r) * SEC
        bks = {}
        for b in books:
            books[b] *= math.exp(rng.gauss(0, 0.0004))
            spread = rng.choice([1, 2, 3, 4]) if b < 96 else rng.choice([3, 4])
            bks[b] = book(books[b], spread, fundamental=books[b] * (1 + rng.gauss(0, 0.003)),
                          bid_q=rng.uniform(1, 20), ask_q=rng.uniform(1, 20))
        accts = {b: account(w["base"], w["quote"], w["initial"], resting[b]) for b, w in wallets.items()}
        v3.accounts = dict(accts)
        g.accounts = dict(accts)
        gmod._REQUEST_ACCOUNTS.set(None)
        rv3 = v3.respond(state(bks, t))
        rg = g.respond(state(bks, t))
        if rv3.calls != rg.calls:
            mismatches += 1
            if mismatches <= 3:
                print("   mismatch round", r, "\n   V3 :", rv3.calls, "\n   Gpt:", rg.calls)
        per_book = {}
        for c in rg.calls:
            per_book[c[1]] = per_book.get(c[1], 0) + 1
        max_instr = max([max_instr] + list(per_book.values()))
        for c in rg.calls:
            if c[0] == "place":
                bk = bks[c[1]]
                guard = (3 if c[1] < 96 else 1) * 0.01
                if (c[2] == 0 and c[4] > bk.asks[0].price - guard + 1e-9) or (c[2] == 1 and c[4] < bk.bids[0].price + guard - 1e-9):
                    guard_violations += 1
        # apply the instructions, then random fills on resting orders
        for c in rv3.calls:
            if c[0] == "cancel":
                resting[c[1]] = [o for o in resting[c[1]] if o.id not in c[2]]
            else:
                resting[c[1]].append(Order(next_id[0], c[2], c[4], c[3], t))
                next_id[0] += 1
        for b in books:
            for o in list(resting[b]):
                if rng.random() < 0.08:
                    w = wallets[b]
                    if o.side == BUY:
                        w["base"] += o.quantity; w["quote"] -= o.quantity * o.price
                    else:
                        w["base"] -= o.quantity; w["quote"] += o.quantity * o.price
                    resting[b].remove(o)
                    ev = N(makerAgentId=1, takerAgentId=99, bookId=b, timestamp=t, price=o.price, quantity=o.quantity,
                           side=SELL if o.side == BUY else BUY, makerFee=0.0, makerOrderId=o.id)
                    v3.onTrade(ev, "v1"); g.onTrade(ev, "v1")
    return v3, g, mismatches, max_instr, guard_violations


def t00_t07_t09_t13():
    v3, g, mismatches, max_instr, guard_violations = regression()
    check("T00 neutral Gpt reproduces V3's instructions over 400 rounds", mismatches == 0, f"{mismatches} rounds differ")
    check("T07 at most 5 instructions per book per response", max_instr <= 5, f"max {max_instr}")
    check("T09 every placed price keeps the cross guard from the opposite touch", guard_violations == 0,
          f"{guard_violations} violations")
    same_marks = v3.markout_stats == g.markout_stats and v3.spread_mult == g.spread_mult
    n = sum(s["n"] for s in v3.markout_stats.values())
    check("T13 V3's 20 s markout and spread adaptation unchanged", same_marks and n > 0, f"{n} fills marked")
    # The default (non-neutral) agent on the same market must also respect the limits it adds.
    return g


# ── T01-T14 ──────────────────────────────────────────────────────────────────────────────────────────────────

def places(calls):
    return [c for c in calls if c[0] == "place"]


def cancels(calls):
    return [c for c in calls if c[0] == "cancel"]


def t01():
    # Class 1: min order 2.5364 x ~30 = ~76 QUOTE; a 0.1% cap of 50k is 50 QUOTE, below it.
    g = make(gmod.MyAgent10_23_Gpt, max_order_fraction=0.001)
    calls = quote_once(g, book(30.0, 4), account(833.3, 25_000.0, 833.3), book_id=100)
    check("T01 minimum order above the hard cap: no new order", not places(calls), str(calls))
    v3 = make(v3mod.MyAgentV3)
    calls_v3 = quote_once(v3, book(30.0, 4), account(833.3, 25_000.0, 833.3), book_id=100)
    print("      (V3 on the same book places", [round(c[3] * c[4], 1) for c in places(calls_v3)], "QUOTE per order)")


def t02():
    # Class 1 (minimum 2.5364): a resting bid 2.6 @ 29.00, far below its ~29.98 target, holds all the QUOTE there is.
    # Replacing it at the target would need 2.5365 x 29.98 = 76.04 QUOTE, but cancelling frees only 75.40.
    g, v3 = make(gmod.MyAgent10_23_Gpt), make(v3mod.MyAgentV3)
    bk = book(30.0, 4)

    def acct():
        a = account(833.3, 75.4, 833.3, [Order(5, BUY, 29.0, 2.6, 900 * SEC)])
        a.quote_balance.free = 0.0
        return a
    calls_g = quote_once(g, bk, acct(), book_id=100)
    calls_v3 = quote_once(v3, bk, acct(), book_id=100)
    bid_cancel_g = any(c[0] == "cancel" and 5 in c[2] for c in calls_g)
    bid_cancel_v3 = any(c[0] == "cancel" and 5 in c[2] for c in calls_v3)
    bid_place_v3 = any(c[0] == "place" and c[2] == 0 for c in calls_v3)
    check("T02 no funds for a replacement: the safe resting bid is kept", not bid_cancel_g and bid_cancel_v3,
          f"Gpt {calls_g}; V3 cancelled={bid_cancel_v3} placed={bid_place_v3}")


def t03():
    g = make(gmod.MyAgent10_23_Gpt, inventory_sizing=0, max_order_fraction=0, size_fraction=0.05)
    # Inventory gap +0.24 (band 0.25): a bid of 5% of wealth would take it past the band.
    mid, wealth = 300.0, 50_000.0
    initial = 0.26 * wealth / mid
    base = initial + 0.24 * wealth / mid
    acct = account(base, wealth - base * mid, initial)
    calls = quote_once(g, book(mid, 3), acct)
    bids = [c for c in places(calls) if c[2] == 0]
    after = 0.24 + (bids[0][3] * bids[0][4] / wealth if bids else 0)
    check("T03 a full fill keeps the inventory gap inside the band", not bids or after <= 0.25 + 1e-9,
          f"bid {bids}, gap after fill {after:.4f}")


def t04():
    g = make(gmod.MyAgent10_23_Gpt)
    resting = [Order(1, BUY, 299.9, 1.0, 900 * SEC), Order(2, SELL, 300.1, 1.0, 900 * SEC)]
    acct = account(83.33, float("nan"), 83.33, resting)
    calls = quote_once(g, book(300.0, 3), acct)
    check("T04 non-finite wealth: everything cancelled, nothing placed",
          not places(calls) and {1, 2} <= {i for c in cancels(calls) for i in c[2]}, str(calls))


def t05():
    g = make(gmod.MyAgent10_23_Gpt)
    memory = {"fills": {BUY: 300.0, SELL: 0.0}}
    value = g._fill_balance(memory, 300.0, 3.0)
    check("T05 one maker fill moves the fill balance a quarter of the way, not all of it", abs(value - 0.25) < 1e-9,
          f"{value:.3f}")


def t06():
    g = make(gmod.MyAgent10_23_Gpt, max_order_fraction=0, inventory_sizing=0)
    # The framework's own refresh (FinanceAgent.update, which every agent here reaches through super()) replaced by
    # the part that matters: rebinding the shared self.accounts, as a concurrent request does.
    original_update = FinanceAgent.update

    def fake_update(self, st):
        self.accounts = st.accounts[self.uid]
    FinanceAgent.update = fake_update
    try:
        small = account(8.333, 2_500.0, 8.333)          # wealth ~5k  -> quotes ~0.1 BASE
        large = account(833.3, 250_000.0, 833.3)        # wealth ~500k -> quotes ~10 BASE
        bk = book(300.0, 3)
        st_a = N(books={0: bk}, timestamp=1000 * SEC, dendrite=N(hotkey="A"), accounts={1: {0: small}})
        st_b = N(books={0: bk}, timestamp=1000 * SEC, dendrite=N(hotkey="B"), accounts={1: {0: large}})
        barrier1, barrier2 = threading.Barrier(2), threading.Barrier(2)
        out = {}

        def run_a():
            g.update(st_a)
            barrier1.wait()        # B updates (rebinding self.accounts to its own) ...
            barrier2.wait()        # ... before A responds
            out["A"] = g.respond(st_a).calls

        def run_b():
            barrier1.wait()
            g.update(st_b)
            barrier2.wait()
            out["B"] = g.respond(st_b).calls
        ta, tb = threading.Thread(target=run_a), threading.Thread(target=run_b)
        ta.start(); tb.start(); ta.join(); tb.join()
    finally:
        FinanceAgent.update = original_update
    qa = [c[3] for c in places(out["A"])]
    qb = [c[3] for c in places(out["B"])]
    check("T06 two validators at once: each request sizes from its own account",
          qa and qb and max(qa) < 1 and min(qb) > 5, f"A sizes {qa}, B sizes {qb}")


def t08():
    g = make(gmod.MyAgent10_23_Gpt)
    resting = [Order(1, BUY, 299.9, 1.0, 900 * SEC)]
    acct = account(83.33, 25_000.0, 83.33, resting, traded=460_000.0)   # cap 500k, margin 0.9
    calls = quote_once(g, book(300.0, 3), acct)
    check("T08 near the turnover cap: only cancels", not places(calls) and cancels(calls), str(calls))


def t09_extreme():
    g = make(gmod.MyAgent10_23_Gpt, max_pred_bps=50, lean=20)
    bk = book(300.0, 2, fundamental=330.0)    # a huge upward lean pushes the bid toward the ask
    calls = quote_once(g, bk, account(83.33, 25_000.0, 83.33))
    bids = [c for c in places(calls) if c[2] == 0]
    ok = all(c[4] <= bk.asks[0].price - 0.03 + 1e-9 for c in bids)
    check("T09 extreme lean: the bid still keeps 3 ticks from the ask", ok, f"bids {bids}, best ask {bk.asks[0].price}")


def t10():
    g = make(gmod.MyAgent10_23_Gpt, max_order_fraction=0, inventory_sizing=0)
    bk = book(300.0, 3)
    resting = Order(9, BUY, 299.0, 0.9, 900 * SEC)            # far below target: to be replaced
    acct = account(83.33, 300.0, 83.33, [resting])            # QUOTE 300, 269.1 of it locked in the bid
    calls = quote_once(g, bk, acct)
    bids = [c for c in places(calls) if c[2] == 0]
    cancelled = any(c[0] == "cancel" and 9 in c[2] for c in calls)
    spend = bids[0][3] * bids[0][4] if bids else 0.0
    check("T10 the replacement may use the funds of the bid it cancels, and no more",
          cancelled and bids and spend <= 300.0 + 1e-6 and spend > 30.9, f"bid {bids}, spends {spend:.2f} of 300")


def t11():
    g = make(gmod.MyAgent10_23_Gpt)
    bk = book(300.0, 3)
    first = quote_once(g, bk, account(83.33, 25_000.0, 83.33))
    resting = [Order(i + 1, c[2], c[4], c[3], 1000 * SEC) for i, c in enumerate(places(first))]
    second = quote_once(g, bk, account(83.33, 25_000.0, 83.33, resting), t=1001 * SEC)
    check("T11 unchanged market: the resting quotes are kept, no instructions", second == [], str(second))


def t12():
    g = make(gmod.MyAgent10_23_Gpt)
    calls = quote_once(g, book(30.0, 4, fundamental=30.1), account(833.3, 25_000.0, 833.3), book_id=100)
    ok = bool(places(calls))
    for c in places(calls):
        ok &= abs(c[4] * 100 - round(c[4] * 100)) < 1e-6
        ok &= abs(c[3] * 1e4 - round(c[3] * 1e4)) < 1e-6
        ok &= c[3] >= 2.5365 - 1e-9
    check("T12 class 1: prices on the 0.01 grid, sizes on the 1e-4 grid and above 2.5364", ok, str(places(calls)))


def t14():
    rng = random.Random(3)
    worst, count = 0.0, 0
    for _ in range(2000):
        frac = rng.uniform(0.0005, 0.02)
        g = make(gmod.MyAgent10_23_Gpt, max_order_fraction=frac, size_fraction=0.05, inventory_sizing=0)
        mid = rng.uniform(25, 320)
        wealth = rng.uniform(1_000, 80_000)
        base = 0.3 * wealth / mid
        b = 100 if mid < 60 else 0
        bk = book(mid, 3)
        calls = quote_once(g, bk, account(base, wealth - base * mid, base), book_id=b)
        agent_wealth = base * (bk.bids[0].price + bk.asks[0].price) / 2 + (wealth - base * mid)
        for c in places(calls):
            count += 1
            worst = max(worst, c[3] * c[4] / (frac * agent_wealth))
    check("T14 2000 random cap/size boundaries: no order above the hard cap", worst <= 1 + 1e-9,
          f"{count} orders, largest {worst:.6f} x cap")


def default_regression_limits():
    # The default agent on the regression market: the cap and the band hold on every placed order.
    rng = random.Random(11)
    g = make(gmod.MyAgent10_23_Gpt)
    worst_cap, worst_band = 0.0, -1.0
    for r in range(300):
        mid = 300 * math.exp(rng.gauss(0, 0.01))
        wealth = 50_000.0
        gap = rng.uniform(-0.3, 0.3)
        initial = 0.3 * wealth / mid
        base = initial + gap * wealth / mid
        acct = account(base, wealth - base * mid, initial)
        bk = book(mid, 3)
        calls = quote_once(g, bk, acct, t=(1000 + r) * SEC)
        w = base * (bk.bids[0].price + bk.asks[0].price) / 2 + (wealth - base * mid)
        for c in places(calls):
            worst_cap = max(worst_cap, c[3] * c[4] / (g.max_order_fraction * w))
            adds = gap if c[2] == 0 else -gap
            worst_band = max(worst_band, adds + c[3] * c[4] / w)
    check("T03/T14 default agent, 300 random inventories: cap and post-fill band hold",
          worst_cap <= 1 + 1e-9 and worst_band <= 0.25 + 1e-9, f"cap x{worst_cap:.4f}, gap after fill {worst_band:.4f}")


if __name__ == "__main__":
    t00_t07_t09_t13()
    for test in (t01, t02, t03, t04, t05, t06, t08, t09_extreme, t10, t11, t12, t14, default_regression_limits):
        try:
            test()
        except Exception as ex:
            import traceback
            traceback.print_exc()
            check(test.__name__, False, f"raised {ex!r}")
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
