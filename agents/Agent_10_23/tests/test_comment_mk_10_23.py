# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_23_CommentMk, on stub states (no simulator).

    python agents/Agent_10_23/tests/test_comment_mk_10_23.py

Runs MyAgent10_23_Comment's own tests (C01-C08) against the candidate, which must behave the same everywhere but
the markout, then M01-M05 on the print-based markout: an adverse fill measured the validator's way, no settling
before the horizon's windows fill, the stale flush, which fills count, and a bounded print window.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_comment_10_23 as base                                # noqa: E402
import MyAgent10_23_CommentMk as mkmod                            # noqa: E402

# The base tests build agents through `base.cmod.MyAgent10_23_Comment`; point them at the candidate.
mkmod.MyAgent10_23_Comment = mkmod.MyAgent10_23_CommentMk
base.cmod = mkmod

N, SEC, check = base.N, base.SEC, base.check
UID, W = 1, 15


def prints(t, price, n, maker=-1, taker=-2, side=0, quantity=1.0):
    return [N(type="t", timestamp=t + k, price=price, quantity=quantity, side=side,
              maker_agent_id=maker, taker_agent_id=taker) for k in range(n)]


def feed(agent, t, events, mid=300.0):
    bk = base.book(mid)
    bk.events = events
    base.run(agent, bk, base.account(100.0, 30_000.0), t=t)


def stats(agent):
    return agent.markout_stats.get(("v1", "simulation_0"))


def adverse_fill(agent, t0):
    """15 background prints at 300.00, this agent's maker buy of 2 at 299.95, then 15 prints at 299.93 as the price
    falls through it."""
    events = prints(t0, 300.00, W) + prints(t0 + 100, 299.95, 1, maker=UID, taker=-3, side=1, quantity=2.0) \
        + prints(t0 + 200, 299.93, W)
    feed(agent, t0 + SEC, events)


def m01_adverse_fill_validator_marks():
    agent = base.make()
    t0 = 1000 * SEC
    adverse_fill(agent, t0)
    # 21 s later the background prints at 299.80: realized -0.15 x 2, clamped to the captured spread.
    feed(agent, t0 + 25 * SEC, prints(t0 + 21 * SEC, 299.80, 2 * W + 1))
    s = stats(agent)
    cap_mid  = (W * 300.00 + 299.95 + W * 299.93) / (2 * W + 1)
    captured = (cap_mid - 299.95) * 2
    ok = s is not None and s["n"] == 1 and abs(s["captured"] - captured) < 1e-9 and abs(s["clamped"] + captured) < 1e-9
    check("M01 adverse maker buy: captured against the print-centered mid, realized clamped to -captured",
          ok, f"{s}, expected captured {captured:.5f} (the book mid would have given {(300.0 - 299.95) * 2:.3f})")


def m02_waits_for_the_horizon_window():
    agent = base.make()
    t0 = 1000 * SEC
    adverse_fill(agent, t0)
    feed(agent, t0 + 10 * SEC, prints(t0 + 9 * SEC, 299.80, 2 * W + 1))          # before the horizon
    early = stats(agent)
    feed(agent, t0 + 22 * SEC, prints(t0 + 21 * SEC, 299.80, W - 1))             # past it, forward window short
    short = stats(agent)
    feed(agent, t0 + 23 * SEC, prints(t0 + 22 * SEC, 299.80, 2))                 # forward window complete
    done = stats(agent)
    check("M02 a fill settles only once the horizon print has a full forward window",
          early is None and short is None and done is not None and done["n"] == 1, f"{early} / {short} / {done}")


def m03_stale_fill_flushes():
    agent = base.make()
    t0 = 1000 * SEC
    adverse_fill(agent, t0)
    feed(agent, t0 + 300 * SEC, [])
    before = stats(agent)
    feed(agent, t0 + 321 * SEC, [])                                              # horizon 20 s + flush 300 s passed
    after = stats(agent)
    check("M03 on a quiet book a fill settles after horizon + flush against what has printed",
          before is None and after is not None and after["n"] == 1, f"{before} / {after}")


def m04_only_own_maker_fills():
    agent = base.make()
    t0 = 1000 * SEC
    events = prints(t0, 300.0, W) \
        + prints(t0 + 100, 300.0, 1, maker=-1, taker=UID, side=0) \
        + prints(t0 + 200, 300.0, 1, maker=UID, taker=UID, side=0) \
        + prints(t0 + 300, 300.0, 1, maker=7, taker=-2, side=1) \
        + prints(t0 + 400, 300.01, 1, maker=UID, taker=-2, side=0) \
        + prints(t0 + 500, 300.0, W)
    feed(agent, t0 + SEC, events)
    pending = agent.memory[("v1", 0)]["mk_pending"]
    check("M04 only this agent's maker fills against another account are queued, a sell as -1",
          len(pending) == 1 and pending[0][2] == -1 and pending[0][3] == 300.01, f"{pending}")


def m05_window_stays_bounded():
    agent = base.make()
    t = 1000 * SEC
    for k in range(50):
        feed(agent, t + k * SEC, prints(t + k * SEC, 300.0, 200))
    size = len(agent.memory[("v1", 0)]["prints"])
    check("M05 with nothing pending the print window keeps only what a future fill can reach",
          size <= 2 * agent.mark_reach * W + 200, f"{size} prints kept of 10000")


if __name__ == "__main__":
    tests = (base.c01_cap_over_minimum, base.c02_unaffordable_replacement_keeps_safe_quote,
             base.c03_risky_quote_cancelled_and_affordable_replaced, base.c04_quoting_time_needs_a_resting_quote,
             base.c05_token_bucket_pauses_and_refills, base.c06_token_bucket_spreads_quoting,
             base.c07_centered_fundamental, base.c08_invariants,
             m01_adverse_fill_validator_marks, m02_waits_for_the_horizon_window, m03_stale_fill_flushes,
             m04_only_own_maker_fills, m05_window_stays_bounded)
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
