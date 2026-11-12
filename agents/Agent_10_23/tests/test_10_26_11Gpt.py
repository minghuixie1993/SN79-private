# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_26_11Gpt (no simulator).

    python agents/Agent_10_23/tests/test_10_26_11Gpt.py

D01 dir_mode=0 places exactly 9Gpt's orders; D02-D03 a flat long-only / short-only book quotes only its entry side;
D04 the cost is booked from the balance change and a long's exit sits at or above cost x (1 + fee + margin), no
larger than the position; D05 the short mirror; D06 mode 1 enters on the fundamental's side; D07 no signal: both
entries; D08 dir_stop_bps lifts the floor; D09 300 random rounds of mode 5: no exit ever priced through its floor,
no exit larger than the position; D10 dir_long / dir_short book lists; D11 the card.
"""
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_10_23_9Gpt as t9                                     # noqa: E402
import MyAgent10_26_11Gpt as m11                                 # noqa: E402

base, check, make = t9.base, t9.check, t9.make
SEC, BUY, SELL = base.SEC, base.BUY, base.SELL
Agent = m11.MyAgent10_26_11Gpt
LIVE = dict(t9.LIVE7, persist_state=0, pending_aware=1)
MID, Q0, CASH = 300.0, 50_000 / 2 / 300.0, 25_000.0


def places(calls, side):
    return [(c[4], c[3]) for c in calls if c[0] != "cancel" and c[2] == side]


def two_rounds(agent, cost, held, mid, fee=0.0, fundamental=None):
    """A flat round, then one where `held` BASE (negative: sold) changed hands at `cost`; returns round 2's calls."""
    base.quote_once(agent, base.book(MID, 3, fundamental), base.account(Q0, CASH, Q0, fee=fee), t=1000 * SEC)
    acct = base.account(Q0 + held, CASH - held * cost, Q0, fee=fee)
    return base.quote_once(agent, base.book(mid, 3, fundamental), acct, t=1001 * SEC)


def d01_off_equals_9gpt():
    (a, _, _), (b, _, _) = t9._market([make(t9.Agent, **LIVE), make(Agent, **LIVE)], 300, 43, lag=0)
    differ = sum(x != y for x, y in zip(a, b))
    check("D01 dir_mode=0: exactly 9Gpt's instructions over 300 rounds", differ == 0, f"{differ} rounds differ")


def d02_d03_flat_entries():
    calls = base.quote_once(make(Agent, dir_mode=3, **LIVE), base.book(MID), base.account(Q0, CASH, Q0))
    check("D02 long only, flat: bids only", places(calls, BUY) and not places(calls, SELL), str(calls))
    calls = base.quote_once(make(Agent, dir_mode=4, **LIVE), base.book(MID), base.account(Q0, CASH, Q0))
    check("D03 short only, flat: asks only", places(calls, SELL) and not places(calls, BUY), str(calls))


def d04_long_exit_floor():
    agent = make(Agent, dir_mode=3, dir_margin_bps=2, **LIVE)
    calls = two_rounds(agent, cost=303.0, held=2.0, mid=MID, fee=0.0005)
    lock = agent.memory[("v1", 0)]["d11_lock"]
    asks = places(calls, SELL)
    floor = 303.0 * (1 + 0.0005 + 2e-4)
    check("D04 long: cost booked from the balance change", abs(lock["cost"] - 303.0) < 1e-9, str(lock))
    check("D04 long: the exit rests at or above cost x (1 + fee + margin), no larger than the position",
          asks and all(p >= floor - 1e-9 and q <= 2.0 + 1e-9 for p, q in asks), f"{asks} floor {floor:.4f}")
    check("D04 long: the entry side still quotes", bool(places(calls, BUY)), str(calls))
    # a cheaper second lot moves the average
    acct = base.account(Q0 + 4.0, CASH - 2.0 * 303.0 - 2.0 * 297.0, Q0)
    base.quote_once(agent, base.book(MID), acct, t=1002 * SEC)
    cost = agent.memory[("v1", 0)]["d11_cost"]
    check("D04 long: adding a lot averages the cost", abs(cost - 300.0) < 1e-9, f"{cost}")


def d05_short_exit_ceiling():
    agent = make(Agent, dir_mode=4, dir_margin_bps=2, **LIVE)
    calls = two_rounds(agent, cost=297.0, held=-2.0, mid=MID)
    bids = places(calls, BUY)
    ceiling = 297.0 * (1 - 2e-4)
    check("D05 short: the exit rests at or below cost x (1 - fee - margin), no larger than the position",
          bids and all(p <= ceiling + 1e-9 and q <= 2.0 + 1e-9 for p, q in bids), f"{bids} ceiling {ceiling:.4f}")
    check("D05 short: the entry side still quotes", bool(places(calls, SELL)), str(calls))


def d06_fundamental_mode():
    up = base.quote_once(make(Agent, dir_mode=1, dir_enter_bps=20, **LIVE), base.book(MID, 3, MID * 1.005),
                         base.account(Q0, CASH, Q0))
    down = base.quote_once(make(Agent, dir_mode=1, dir_enter_bps=20, **LIVE), base.book(MID, 3, MID * 0.995),
                           base.account(Q0, CASH, Q0))
    check("D06 mode 1: fundamental 50 bps above -> long entry only, below -> short entry only",
          places(up, BUY) and not places(up, SELL) and places(down, SELL) and not places(down, BUY), f"{up} / {down}")


def d07_no_signal():
    calls = base.quote_once(make(Agent, dir_mode=1, dir_enter_bps=20, **LIVE), base.book(MID, 3, MID * 1.0005),
                            base.account(Q0, CASH, Q0))
    check("D07 weak signal: both entries while flat", places(calls, BUY) and places(calls, SELL), str(calls))


def d08_stop():
    agent = make(Agent, dir_mode=3, dir_stop_bps=50, **LIVE)
    calls = two_rounds(agent, cost=303.0, held=2.0, mid=MID)            # mid ~99 bps below the cost
    asks = places(calls, SELL)
    check("D08 dir_stop_bps 50: a long 99 bps under water is quoted without the floor",
          asks and all(p < 303.0 for p, _ in asks), str(asks))


def d09_random_market():
    rng = random.Random(7)
    agent = make(Agent, dir_mode=5, dir_margin_bps=1, **LIVE)
    mid, held_base, cash, resting, next_id = MID, Q0, CASH, [], 1
    bad, exits, crossed, fills = [], 0, 0, 0
    for r in range(300):
        mid *= math.exp(rng.gauss(0, 0.0008))
        acct = base.account(held_base, cash, Q0, resting)
        calls = base.quote_once(agent, base.book(mid, rng.choice([2, 3, 4])), acct, t=(700 + r) * SEC)
        lock = agent.memory[("v1", 0)]["d11_lock"]
        for c in calls:
            if c[0] == "cancel":
                resting = [o for o in resting if o.id not in c[2]]
                continue
            side, qty, price = c[2], c[3], c[4]
            if not lock["flat"] and lock["cost"] is not None and (side == SELL) == (lock["position"] > 0):
                exits += 1
                if qty > abs(lock["position"]) + 1e-9:
                    bad.append(("size", r, qty, lock["position"]))
                if lock["position"] > 0 and price < lock["cost"] * (1 + 1e-4) - 1e-9:
                    bad.append(("price", r, price, lock["cost"]))
                if lock["position"] < 0 and price > lock["cost"] * (1 - 1e-4) + 1e-9:
                    bad.append(("price", r, price, lock["cost"]))
            resting.append(base.Order(next_id, side, price, qty, (700 + r) * SEC))
            next_id += 1
        before = held_base - Q0
        for o in list(resting):
            if rng.random() < 0.08:
                held_base += o.quantity if o.side == BUY else -o.quantity
                cash -= o.quantity * o.price if o.side == BUY else -o.quantity * o.price
                resting.remove(o)
                fills += 1
        after = held_base - Q0
        crossed += (before > 1e-9 and after < -1e-9) or (before < -1e-9 and after > 1e-9)
    check("D09 300 random rounds, mode 5: every exit at or beyond its floor and within the position",
          not bad and exits > 0, f"{exits} exits, {fills} fills, {len(bad)} bad {bad[:3]}")
    check("D09 the position never flips sign through an exit in one round", crossed == 0, f"{crossed} flips")


def d10_book_lists():
    agent = make(Agent, dir_long="0:5", dir_short=9, **LIVE)
    a = base.quote_once(agent, base.book(MID), base.account(Q0, CASH, Q0), book_id=0)
    b = base.quote_once(agent, base.book(MID), base.account(Q0, CASH, Q0), book_id=9)
    c = base.quote_once(agent, base.book(MID), base.account(Q0, CASH, Q0), book_id=3)
    check("D10 dir_long '0:5' -> book 0 bids only; dir_short 9 -> book 9 asks only; book 3 two-sided",
          places(a, BUY) and not places(a, SELL) and places(b, SELL) and not places(b, BUY)
          and places(c, BUY) and places(c, SELL), f"{a} / {b} / {c}")


def d11_card():
    agent = make(Agent, dir_mode=3, **LIVE)
    two_rounds(agent, cost=303.0, held=2.0, mid=MID)
    card = agent._plan_card("v1")
    check("D11 card shows the open locks", "11GPT" in card and "long 1" in card, card[-200:])


if __name__ == "__main__":
    t9.base.RESULTS.clear()
    for test in (d01_off_equals_9gpt, d02_d03_flat_entries, d04_long_exit_floor, d05_short_exit_ceiling,
                 d06_fundamental_mode, d07_no_signal, d08_stop, d09_random_market, d10_book_lists, d11_card):
        test()
    failed = [n for n, ok, _ in t9.base.RESULTS if not ok]
    print(f"\n{len(t9.base.RESULTS) - len(failed)}/{len(t9.base.RESULTS)} passed")
    sys.exit(1 if failed else 0)
