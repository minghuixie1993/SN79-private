# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_26_12Gpt (no simulator).

    python agents/Agent_10_23/tests/test_10_26_12Gpt.py

E01 dir_no_chase=0 places exactly 11Gpt's orders; E02 a long whose price rose adds no bid above its cost and still
sells; E03 after a long is sold to flat, no bid above the exit price while the ask stays open; E04 the short mirror;
E05 the exit price is forgotten after dir_exit_memory_s; E06 mode 6 enters short after a rise, long after a fall;
E07 300 random rounds of mode 5: no add above the cost, no re-entry above the last exit; E08 dir_patience_s.
"""
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_10_26_11Gpt as t11                                   # noqa: E402
import MyAgent10_26_12Gpt as m12                                 # noqa: E402

base, check, make, places = t11.base, t11.check, t11.make, t11.places
SEC, BUY, SELL, LIVE = base.SEC, base.BUY, base.SELL, t11.LIVE
MID, Q0, CASH = t11.MID, t11.Q0, t11.CASH
Agent = m12.MyAgent10_26_12Gpt


def rounds(agent, steps):
    """Runs (mid, base held beyond the initial, cash) per round on book 0; returns the last round's calls."""
    calls = None
    for i, (mid, held, cash) in enumerate(steps):
        calls = base.quote_once(agent, base.book(mid), base.account(Q0 + held, cash, Q0), t=(1000 + i) * SEC)
    return calls


def e01_off_equals_11gpt():
    (a, _, _), (b, _, _) = t11.t9._market([make(t11.Agent, dir_mode=5, **LIVE),
                                           make(Agent, dir_mode=5, dir_no_chase=0, **LIVE)], 300, 47, lag=0)
    differ = sum(x != y for x, y in zip(a, b))
    check("E01 dir_no_chase=0: exactly 11Gpt's instructions over 300 rounds (mode 5)", differ == 0, f"{differ} differ")


def e02_no_averaging_up():
    calls = rounds(make(Agent, dir_mode=3, **LIVE), [(MID, 0, CASH), (MID, 2.0, CASH - 600.0), (303.0, 2.0, CASH - 600.0)])
    bids, asks = places(calls, BUY), places(calls, SELL)
    check("E02 long at 300, price 303: no bid above 300, the exit still sells",
          all(p <= 300.0 + 1e-9 for p, _ in bids) and asks, f"bids {bids} asks {asks}")


def e03_no_buy_back_higher():
    agent = make(Agent, dir_mode=5, **LIVE)
    steps = [(MID, 0, CASH), (MID, 2.0, CASH - 600.0), (304.0, 0.0, CASH - 600.0 + 2 * 304.0), (306.0, 0.0, CASH + 8.0)]
    calls = rounds(agent, steps)
    bids, asks = places(calls, BUY), places(calls, SELL)
    check("E03 sold at 304, price 306: no bid above 304, the short entry (ask) stays open",
          all(p <= 304.0 + 1e-9 for p, _ in bids) and asks, f"bids {bids} asks {asks}")


def e04_short_mirror():
    agent = make(Agent, dir_mode=5, **LIVE)
    steps = [(MID, 0, CASH), (MID, -2.0, CASH + 600.0), (296.0, 0.0, CASH + 600.0 - 2 * 296.0), (294.0, 0.0, CASH + 8.0)]
    calls = rounds(agent, steps)
    bids, asks = places(calls, BUY), places(calls, SELL)
    check("E04 short covered at 296, price 294: no ask below 296, the long entry stays open",
          all(p >= 296.0 - 1e-9 for p, _ in asks) and bids, f"bids {bids} asks {asks}")


def e05_exit_memory():
    # small moves, so the volatility estimate leaves the quote near the touch: sold at 300.3, then the mid is 300.6
    steps = [(MID, 0, CASH), (MID, 2.0, CASH - 600.0), (300.3, 0.0, CASH + 0.6)] + [(300.6, 0.0, CASH + 0.6)] * 3
    kept = places(rounds(make(Agent, dir_mode=5, **LIVE), steps), BUY)
    freed = places(rounds(make(Agent, dir_mode=5, dir_exit_memory_s=1, **LIVE), steps), BUY)
    check("E05 the exit binds the re-entry bid (<= 300.3); with dir_exit_memory_s 1 it follows the market again",
          kept and freed and max(p for p, _ in kept) <= 300.3 + 1e-9 and max(p for p, _ in freed) > 300.3,
          f"kept {kept} freed {freed}")


def e06_fade():
    up = make(Agent, dir_mode=6, dir_enter_bps=20, **LIVE)
    calls_up = rounds(up, [(MID * (1.001 ** i), 0, CASH) for i in range(6)])
    down = make(Agent, dir_mode=6, dir_enter_bps=20, **LIVE)
    calls_down = rounds(down, [(MID * (0.999 ** i), 0, CASH) for i in range(6)])
    check("E06 mode 6: after a rise asks only, after a fall bids only",
          places(calls_up, SELL) and not places(calls_up, BUY) and places(calls_down, BUY) and not places(calls_down, SELL),
          f"up {calls_up} / down {calls_down} (mom300 {up.memory[('v1', 0)]['mom300']:+.1f})")


def e08_patience():
    # long 2 at 300, the mid falls to 297 and stays: the floor starts at the cost, then slides to the mid
    steps = [(MID, 0, CASH), (MID, 2.0, CASH - 600.0)] + [(297.0, 2.0, CASH - 600.0)] * 2
    early = places(rounds(make(Agent, dir_mode=3, dir_patience_s=600, **LIVE), steps), SELL)
    late_steps = steps[:2] + [(297.0 * (1 + 1e-5 * (i % 2)), 2.0, CASH - 600.0) for i in range(1, 700, 100)]
    agent = make(Agent, dir_mode=3, dir_patience_s=600, **LIVE)
    late = None
    for i, (mid, held, cash) in enumerate(late_steps):
        t = (1000 + (i if i < 2 else 1 + (i - 1) * 100)) * SEC
        late = base.quote_once(agent, base.book(mid), base.account(Q0 + held, cash, Q0), t=t)
    late = places(late, SELL)
    winner = places(rounds(make(Agent, dir_mode=3, dir_patience_s=1, **LIVE),
                           [(MID, 0, CASH), (MID, 2.0, CASH - 600.0)] + [(MID * 1.0002, 2.0, CASH - 600.0)] * 3), SELL)
    check("E08 dir_patience_s 600: the exit starts at the cost floor and is near the mid after 600 s; a winner keeps it",
          early and late and min(p for p, _ in early) >= 300.0 and max(p for p, _ in late) < 299.0
          and winner and min(p for p, _ in winner) >= 300.0, f"early {early} late {late} winner {winner}")


def e07_random_market():
    rng = random.Random(11)
    agent = make(Agent, dir_mode=5, **LIVE)
    mid, held_base, cash, resting, next_id = MID, Q0, CASH, [], 1
    bad, adds, reentries = [], 0, 0
    for r in range(300):
        mid *= math.exp(rng.gauss(0, 0.0008))
        calls = base.quote_once(agent, base.book(mid, rng.choice([2, 3, 4])), base.account(held_base, cash, Q0, resting),
                                t=(700 + r) * SEC)
        memory = agent.memory[("v1", 0)]
        lock, exit_ = memory["d11_lock"], memory.get("d12_exit")
        for c in calls:
            if c[0] == "cancel":
                resting = [o for o in resting if o.id not in c[2]]
                continue
            side, qty, price = c[2], c[3], c[4]
            if not lock["flat"] and lock["cost"] is not None and (side == BUY) == (lock["position"] > 0):
                adds += 1
                if side == BUY and price > lock["cost"] + 1e-9 or side == SELL and price < lock["cost"] - 1e-9:
                    bad.append(("add", r, side, price, lock["cost"]))
            if lock["flat"] and exit_ is not None and (side == BUY) == (exit_[0] > 0):
                reentries += 1
                if side == BUY and price > exit_[1] + 1e-9 or side == SELL and price < exit_[1] - 1e-9:
                    bad.append(("reentry", r, side, price, exit_[1]))
            resting.append(base.Order(next_id, side, price, qty, (700 + r) * SEC))
            next_id += 1
        for o in list(resting):
            if rng.random() < 0.08:
                held_base += o.quantity if o.side == BUY else -o.quantity
                cash -= o.quantity * o.price if o.side == BUY else -o.quantity * o.price
                resting.remove(o)
    check("E07 300 random rounds, mode 5: no add beyond the cost, no re-entry beyond the last exit",
          not bad and adds > 0 and reentries > 0, f"{adds} adds, {reentries} re-entries, {len(bad)} bad {bad[:3]}")


if __name__ == "__main__":
    base.RESULTS.clear()
    for test in (e01_off_equals_11gpt, e02_no_averaging_up, e03_no_buy_back_higher, e04_short_mirror, e05_exit_memory,
                 e06_fade, e07_random_market, e08_patience):
        test()
    failed = [n for n, ok, _ in base.RESULTS if not ok]
    print(f"\n{len(base.RESULTS) - len(failed)}/{len(base.RESULTS)} passed")
    sys.exit(1 if failed else 0)
