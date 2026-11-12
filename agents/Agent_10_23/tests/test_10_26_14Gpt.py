# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_26_14Gpt (no simulator).

    python agents/Agent_10_23/tests/test_10_26_14Gpt.py

N01 nc_mode=0 places exactly 13Gpt's orders; N02 long at 300 with the mid at 303: the bid is at most 300 and the ask
quotes as 9Gpt's, unfloored (below the 11Gpt lock's floor); N03 the short mirror; N04 flat: both sides as 9Gpt;
N05 300 random rounds: no add beyond the cost and both sides quoted on most rounds.
"""
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_10_26_13Gpt as t13                                   # noqa: E402
import MyAgent10_26_14Gpt as m14                                 # noqa: E402

base, check, make, places, rounds = t13.base, t13.check, t13.make, t13.places, t13.t12.rounds
SEC, BUY, SELL, LIVE = base.SEC, base.BUY, base.SELL, t13.LIVE
MID, Q0, CASH = t13.MID, t13.Q0, t13.CASH
Agent = m14.MyAgent10_26_14Gpt


def n01_off_equals_13gpt():
    a, _ = t13.market(make(t13.Agent, **LIVE), 300, 5)
    b, _ = t13.market(make(Agent, nc_mode=0, **LIVE), 300, 5)
    differ = sum(x != y for x, y in zip(a, b))
    check("N01 nc_mode=0: exactly 13Gpt's instructions over 300 rounds", differ == 0, f"{differ} differ")


def n02_long():
    steps = [(MID, 0, CASH), (MID, 2.0, CASH - 600.0), (303.0, 2.0, CASH - 600.0)]
    on = rounds(make(Agent, nc_mode=1, **LIVE), steps)
    off = rounds(make(Agent, **LIVE), steps)
    check("N02 long at 300, mid 303: bid <= 300, the ask is 9Gpt's own",
          places(on, BUY) and all(p <= 300.0 + 1e-9 for p, _ in places(on, BUY)) and places(on, SELL) == places(off, SELL),
          f"on {on} / off {off}")


def n03_short():
    steps = [(MID, 0, CASH), (MID, -2.0, CASH + 600.0), (297.0, -2.0, CASH + 600.0)]
    on = rounds(make(Agent, nc_mode=1, **LIVE), steps)
    off = rounds(make(Agent, **LIVE), steps)
    check("N03 short at 300, mid 297: ask >= 300, the bid is 9Gpt's own",
          places(on, SELL) and all(p >= 300.0 - 1e-9 for p, _ in places(on, SELL)) and places(on, BUY) == places(off, BUY),
          f"on {on} / off {off}")


def n04_flat():
    steps = [(MID, 0, CASH), (MID, 0, CASH)]
    on, off = rounds(make(Agent, nc_mode=1, **LIVE), steps), rounds(make(Agent, **LIVE), steps)
    check("N04 flat: exactly 9Gpt's quotes", on == off, f"on {on} / off {off}")


def n05_random():
    rng = random.Random(3)
    agent = make(Agent, nc_mode=1, **LIVE)
    mid, held, cash, resting, next_id = MID, Q0, CASH, [], 1
    bad, adds, two_sided, quoted = [], 0, 0, 0
    for r in range(300):
        mid *= math.exp(rng.gauss(0, 0.0008))
        calls = base.quote_once(agent, base.book(mid, rng.choice([2, 3, 4])), base.account(held, cash, Q0, resting),
                                t=(700 + r) * SEC)
        state = agent.memory[("v1", 0)].get("n14")
        for c in calls:
            if c[0] == "cancel":
                resting = [o for o in resting if o.id not in c[2]]
                continue
            if state is not None and (c[2] == BUY) == (state[0] > 0):
                adds += 1
                if c[2] == BUY and c[4] > state[1] + 1e-9 or c[2] == SELL and c[4] < state[1] - 1e-9:
                    bad.append((r, c, state))
            resting.append(base.Order(next_id, c[2], c[4], c[3], (700 + r) * SEC))
            next_id += 1
        sides = {o.side for o in resting}
        quoted += bool(sides)
        two_sided += sides == {BUY, SELL}
        for o in list(resting):
            if rng.random() < 0.08:
                held += o.quantity if o.side == BUY else -o.quantity
                cash -= o.quantity * o.price if o.side == BUY else -o.quantity * o.price
                resting.remove(o)
    check("N05 300 random rounds: no add beyond the cost, the book two-sided on most rounds",
          not bad and adds > 0 and two_sided > 0.8 * quoted, f"{adds} adds, {len(bad)} bad, two-sided {two_sided}/{quoted}")


if __name__ == "__main__":
    base.RESULTS.clear()
    for test in (n01_off_equals_13gpt, n02_long, n03_short, n04_flat, n05_random):
        test()
    failed = [n for n, ok, _ in base.RESULTS if not ok]
    print(f"\n{len(base.RESULTS) - len(failed)}/{len(base.RESULTS)} passed")
    sys.exit(1 if failed else 0)
