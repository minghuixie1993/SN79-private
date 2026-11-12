# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_26_13Gpt (no simulator).

    python agents/Agent_10_23/tests/test_10_26_13Gpt.py

B01 bal_mode=0 places exactly 12Gpt's orders (fills delivered as notices); B02 after buys only, the bid is off and
the ask quotes (a sell window); B03 the mirror; B04 nothing acts below bal_min_fills; B05 fills older than
bal_window_s no longer count; B06 on a random market the paid share 2 x min(buy, sell) / (buy + sell) of each book's
capture rises with the balance on.
"""
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_10_26_12Gpt as t12                                   # noqa: E402
import MyAgent10_26_13Gpt as m13                                 # noqa: E402

base, check, make, places = t12.base, t12.check, t12.make, t12.places
N, SEC, BUY, SELL, LIVE = base.N, base.SEC, base.BUY, base.SELL, t12.LIVE
MID, Q0, CASH = t12.MID, t12.Q0, t12.CASH
Agent = m13.MyAgent10_26_13Gpt


def fill(agent, side, price, qty, t, book=0, oid=1):
    """A notice that our resting `side` order was filled (the taker traded the other way)."""
    agent.onTrade(N(makerAgentId=1, takerAgentId=99, bookId=book, timestamp=t, price=price, quantity=qty,
                    side=SELL if side == BUY else BUY, makerFee=0.0, makerOrderId=oid), "v1")


def market(agent, rounds, seed):
    """A random market on one book whose resting orders fill 8% of the rounds, the fills delivered as notices.
    Returns the instruction lists and the book's own capture per side against the mid at the fill."""
    rng = random.Random(seed)
    mid, held, cash, resting, next_id = MID, Q0, CASH, [], 1
    out, cap = [], {BUY: 0.0, SELL: 0.0}
    for r in range(rounds):
        t = (700 + r) * SEC
        mid *= math.exp(rng.gauss(0, 0.0005))
        calls = base.quote_once(agent, base.book(mid, rng.choice([2, 3, 4])), base.account(held, cash, Q0, resting), t=t)
        out.append(calls)
        for c in calls:
            if c[0] == "cancel":
                resting = [o for o in resting if o.id not in c[2]]
            else:
                resting.append(base.Order(next_id, c[2], c[4], c[3], t))
                next_id += 1
        # the side the market leans on fills more often, so a book drifts out of balance on its own
        lean = 0.12 if (r // 60) % 2 == 0 else 0.04
        for o in list(resting):
            if rng.random() < (lean if o.side == BUY else 0.16 - lean):
                held += o.quantity if o.side == BUY else -o.quantity
                cash -= o.quantity * o.price if o.side == BUY else -o.quantity * o.price
                resting.remove(o)
                cap[o.side] += ((mid - o.price) if o.side == BUY else (o.price - mid)) * o.quantity
                fill(agent, o.side, o.price, o.quantity, t + 500_000_000, oid=o.id)
    return out, cap


def b01_off_equals_12gpt():
    a, _ = market(make(t12.Agent, **LIVE), 300, 5)
    b, _ = market(make(Agent, bal_mode=0, **LIVE), 300, 5)
    differ = sum(x != y for x, y in zip(a, b))
    check("B01 bal_mode=0: exactly 12Gpt's instructions over 300 rounds with fill notices", differ == 0, f"{differ} differ")


def b02_b03_windows():
    agent = make(Agent, bal_mode=1, **LIVE)
    base.quote_once(agent, base.book(MID), base.account(Q0, CASH, Q0), t=1000 * SEC)
    for i in range(5):
        fill(agent, BUY, 299.8, 1.0, 1000 * SEC + i * 1000, oid=10 + i)
    calls = base.quote_once(agent, base.book(MID), base.account(Q0 + 5, CASH - 1499.0, Q0), t=1001 * SEC)
    check("B02 five buys, no sells: the bid is off, the ask quotes (sell window)",
          places(calls, SELL) and not places(calls, BUY), str(calls))
    agent = make(Agent, bal_mode=1, **LIVE)
    base.quote_once(agent, base.book(MID), base.account(Q0, CASH, Q0), t=1000 * SEC)
    for i in range(5):
        fill(agent, SELL, 300.2, 1.0, 1000 * SEC + i * 1000, oid=10 + i)
    calls = base.quote_once(agent, base.book(MID), base.account(Q0 - 5, CASH + 1501.0, Q0), t=1001 * SEC)
    check("B03 five sells, no buys: the ask is off, the bid quotes (buy window)",
          places(calls, BUY) and not places(calls, SELL), str(calls))


def b04_min_fills():
    agent = make(Agent, bal_mode=1, **LIVE)
    base.quote_once(agent, base.book(MID), base.account(Q0, CASH, Q0), t=1000 * SEC)
    for i in range(3):
        fill(agent, BUY, 299.8, 1.0, 1000 * SEC + i * 1000, oid=10 + i)
    calls = base.quote_once(agent, base.book(MID), base.account(Q0 + 3, CASH - 899.4, Q0), t=1001 * SEC)
    check("B04 three fills (< bal_min_fills 4): both sides quote", places(calls, BUY) and places(calls, SELL), str(calls))


def b05_window():
    agent = make(Agent, bal_mode=1, bal_window_s=60, **LIVE)
    base.quote_once(agent, base.book(MID), base.account(Q0, CASH, Q0), t=1000 * SEC)
    for i in range(5):
        fill(agent, BUY, 299.8, 1.0, 1000 * SEC + i * 1000, oid=10 + i)
    calls = base.quote_once(agent, base.book(MID), base.account(Q0 + 5, CASH - 1499.0, Q0), t=1100 * SEC)
    check("B05 bal_window_s 60: buys 100 s old no longer count, both sides quote",
          places(calls, BUY) and places(calls, SELL), str(calls))


def b06_paid_share():
    shares = {}
    for mode in (0, 1):
        _, cap = market(make(Agent, bal_mode=mode, **LIVE), 600, 9)
        b, s = max(cap[BUY], 0.0), max(cap[SELL], 0.0)
        shares[mode] = (2 * min(b, s) / max(b + s, 1e-9), b, s)
    check("B06 random market with a leaning flow: the paid share of capture rises with the balance on",
          shares[1][0] > shares[0][0], f"off {shares[0][0]:.2f} (buy {shares[0][1]:.1f} sell {shares[0][2]:.1f}), "
          f"on {shares[1][0]:.2f} (buy {shares[1][1]:.1f} sell {shares[1][2]:.1f})")


if __name__ == "__main__":
    base.RESULTS.clear()
    for test in (b01_off_equals_12gpt, b02_b03_windows, b04_min_fills, b05_window, b06_paid_share):
        test()
    failed = [n for n, ok, _ in base.RESULTS if not ok]
    print(f"\n{len(base.RESULTS) - len(failed)}/{len(base.RESULTS)} passed")
    sys.exit(1 if failed else 0)
