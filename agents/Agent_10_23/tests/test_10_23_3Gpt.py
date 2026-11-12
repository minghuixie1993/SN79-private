# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_23_3Gpt, on stub states (no simulator).

    python agents/Agent_10_23/tests/test_10_23_3Gpt.py

Runs the whole MyAgent10_23_2Gpt suite against MyAgent10_23_3Gpt (3Gpt must quote exactly as 2Gpt does), then the
checks for what 3Gpt adds: lazy_load chosen by where the agent runs (W01-W04), and pauses counted by reason
(P01-P03; P01 also checks 3Gpt still sends exactly the real 2Gpt file's instructions).
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import MyAgent10_23_3Gpt as m3                                    # noqa: E402

# The 2Gpt suite imports MyAgent10_23_2Gpt and tests its class of that name; point both at 3Gpt.
alias = types.ModuleType("MyAgent10_23_2Gpt")
alias.__dict__.update(m3.__dict__)
alias.MyAgent10_23_2Gpt = m3.MyAgent10_23_3Gpt
sys.modules["MyAgent10_23_2Gpt"] = alias
import test_10_23_2Gpt as t2                                      # noqa: E402

base, onegpt, check, make = t2.base, t2.onegpt, t2.check, t2.make
Agent = m3.MyAgent10_23_3Gpt


def with_argv(argv, fn):
    saved = sys.argv
    sys.argv = argv
    try:
        return fn()
    finally:
        sys.argv = saved


def w01_detects_miner():
    local = with_argv(["agents/network/agent_server.py", "Agent_10_23/MyAgent10_23_3Gpt.py", "--port", "9100",
                       "--agent_id", "3", "--params", "lazy_load=1"], m3.running_under_miner)
    miner = with_argv(["/repo/taos/im/neurons/miner.py", "--netuid", "79", "--wallet.name", "w"], m3.running_under_miner)
    recorded = with_argv(["/repo/taos/im/neurons/miner.py", "--netuid", "79"], m3.running_under_miner)
    check("W01 detection: local agent server -> local, miner.py / recorded_miner -> miner",
          not local and miner and recorded, f"local {local}, miner {miner}, recorded {recorded}")


def w02_auto_sets_lazy_load():
    on_miner = with_argv(["miner.py", "--netuid", "79"], lambda: make(Agent, lazy_load=1))
    local = with_argv(["agent_server.py", "--port", "9100"], lambda: make(Agent, lazy_load=0))
    check("W02 auto: lazy_load forced 0 under the miner (given 1), 1 locally (given 0)",
          on_miner.config.lazy_load is False and local.config.lazy_load is True,
          f"miner {on_miner.config.lazy_load}, local {local.config.lazy_load}")


def w03_overrides():
    forced_on = with_argv(["miner.py", "--netuid", "79"], lambda: make(Agent, lazy_load=0, lazy_mode=1))
    forced_off = with_argv(["agent_server.py"], lambda: make(Agent, lazy_load=1, lazy_mode="0"))
    check("W03 lazy_mode=1 / 0 override the detection",
          forced_on.config.lazy_load is True and forced_off.config.lazy_load is False,
          f"forced on {forced_on.config.lazy_load}, forced off {forced_off.config.lazy_load}")


def w04_warns_once_on_lazy_books():
    from taos.im.protocol.lazy import LazyBooks
    agent = with_argv(["agent_server.py"], lambda: make(Agent))
    raw = {"i": 1, "b": [{"p": 300.0, "q": 1.0}], "a": [{"p": 300.1, "q": 1.0}], "e": []}
    state = types.SimpleNamespace(books=LazyBooks({1: raw}), timestamp=1, dendrite=None, accounts={})
    logged = []
    saved = m3.bt.logging.error
    m3.bt.logging.error = lambda msg, *a, **k: logged.append(str(msg))
    try:
        for _ in range(3):
            try:
                agent.respond(state)
            except Exception:
                pass
    finally:
        m3.bt.logging.error = saved
    warnings = [m for m in logged if "lazily parsed books" in m]
    check("W04 lazily parsed books reaching the agent are reported once", len(warnings) == 1 and agent.lazy_books_warned,
          f"{len(warnings)} warnings")


def load_real_2gpt():
    """MyAgent10_23_2Gpt.py itself (sys.modules' MyAgent10_23_2Gpt is the 3Gpt alias above)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("real_2gpt", os.path.join(os.path.dirname(HERE), "MyAgent10_23_2Gpt.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def p01_same_orders_as_2gpt():
    """With fee spikes and pacing pauses in play, 3Gpt sends exactly the real 2Gpt's instructions."""
    import math
    import random
    real = load_real_2gpt()
    rng = random.Random(23)
    two, three = make(real.MyAgent10_23_2Gpt), make(Agent)
    mids = {0: 300.0, 9: 301.0, 100: 29.8, 120: 30.1}
    wallets = {b: [50_000 / 2 / m, 25_000.0, 50_000 / 2 / m] for b, m in mids.items()}
    resting = {b: [] for b in mids}
    next_id, mismatches, pauses = [1], 0, 0
    for r in range(500):
        t = (700 + r) * base.SEC
        books, accounts = {}, {}
        for b in mids:
            mids[b] *= math.exp(rng.gauss(0, 0.0005))
            books[b] = base.book(mids[b], rng.choice([2, 3, 4]), fundamental=mids[b] * (1 + rng.gauss(0, 0.003)),
                                 bid_q=rng.uniform(1, 20), ask_q=rng.uniform(1, 20))
            acct = base.account(*wallets[b], resting[b])
            acct.fees.maker_fee_rate = 0.03 if rng.random() < 0.05 else 0.0
            accounts[b] = acct
        calls = []
        for agent in (two, three):
            agent.accounts = dict(accounts)
            base.gmod._REQUEST_ACCOUNTS.set(None)
            calls.append(agent.respond(base.state(books, t)).calls)
        mismatches += calls[0] != calls[1]
        for c in calls[0]:
            if c[0] == "cancel":
                resting[c[1]] = [o for o in resting[c[1]] if o.id not in c[2]]
            else:
                resting[c[1]].append(base.Order(next_id[0], c[2], c[4], c[3], t))
                next_id[0] += 1
        for b in mids:
            for o in list(resting[b]):
                if rng.random() < 0.15:       # heavy fills, so the 5-minute pacing budget binds
                    w = wallets[b]
                    w[0] += o.quantity if o.side == base.BUY else -o.quantity
                    w[1] -= o.quantity * o.price if o.side == base.BUY else -o.quantity * o.price
                    resting[b].remove(o)
                    ev = base.N(makerAgentId=1, takerAgentId=-2, bookId=b, timestamp=t, price=o.price,
                                quantity=o.quantity, side=base.SELL if o.side == base.BUY else base.BUY,
                                makerFee=0.0, makerOrderId=o.id)
                    two.onTrade(ev, "v1"); three.onTrade(ev, "v1")
    stats = three.plan_stats["v1"]
    reasons = {k: v for k, v in stats.items() if k.startswith("paused_") and "_c" not in k}
    check("P01 3Gpt sends exactly 2Gpt's instructions over 500 rounds with fee spikes and pacing pauses",
          mismatches == 0 and stats.get("paused", 0) == sum(reasons.values()) and stats.get("paused", 0) > 0
          and reasons.get("paused_fee", 0) > 0 and reasons.get("paused_period", 0) > 0,
          f"{mismatches} rounds differ; paused {stats.get('paused')} = {reasons}")


def p02_each_reason():
    def one(setup, book_id=0):
        g = make(Agent)
        acct = base.account(83.33, 25_000.0, 83.33)
        t = 4_000 * base.SEC
        base.quote_once(g, base.book(300.0, 3), base.account(83.33, 25_000.0, 83.33), book_id=book_id, t=t - base.SEC)
        memory = g.memory[("v1", book_id)]
        setup(acct, memory, t)
        base.quote_once(g, base.book(300.0, 3), acct, book_id=book_id, t=t)
        return {k: v for k, v in g.plan_stats["v1"].items() if k.startswith("paused_")}

    def fee(acct, memory, t):
        acct.fees.maker_fee_rate = 0.03

    def cap(acct, memory, t):
        acct.traded_volume = 460_000.0

    def day(acct, memory, t):
        p = int(300 * 1e9)
        memory["pvolume"] = {t // p - 20 - i: 3_000.0 for i in range(150)}     # 450k over the last 24 h, none now

    def period(acct, memory, t):
        memory["pvolume"] = {t // int(300 * 1e9): 3_500.0}

    got = {name: one(fn) for name, fn in (("fee", fee), ("cap", cap), ("day", day), ("period", period))}
    got["period_c1"] = one(period, book_id=100)
    ok = all(got[r] == {f"paused_{r}": 1, f"paused_{r}_c0": 1} for r in ("fee", "cap", "day", "period")) \
        and got["period_c1"] == {"paused_period": 1, "paused_period_c1": 1}
    check("P02 each pause is booked under its own reason and class (fee, validator cap, 24 h, 5-minute period)",
          ok, str(got))


def p03_card():
    g = make(Agent)
    acct = base.account(83.33, 25_000.0, 83.33)
    base.quote_once(g, base.book(300.0, 3), acct, t=1000 * base.SEC)
    acct.fees.maker_fee_rate = 0.03
    base.quote_once(g, base.book(300.0, 3), acct, t=1001 * base.SEC)
    card = g._plan_card("v1")
    tail = card[card.index("PAUSES"):] if "PAUSES" in card else card[-200:]
    check("P03 the card shows pauses by reason and each class's active share",
          "paused fee 1, cap 0, day 0, period 0" in card and "active 50% of 2 book-rounds, fee 50%" in card, tail)


if __name__ == "__main__":
    base.t00_t07_t09_t13()
    t2.t00_against_repo_v3()
    tests = (base.t01, base.t02, base.t03, base.t04, base.t05, base.t06, base.t08, base.t09_extreme, base.t10,
             base.t11, base.t12, base.t14, base.default_regression_limits,
             onegpt.u01_oversize_replaced, onegpt.u02_smaller_kept, onegpt.u03_nan_prediction,
             onegpt.u04_rejection_attributed, onegpt.u05_fast_fill_counted, onegpt.u06_default_limits_hold,
             t2.v01_grid_nudge, t2.v02_fees, t2.v03_pacing, t2.v04_expiry, t2.v05_bounded_lean,
             t2.v06_book_multiplier, t2.v07_counterparties, t2.v08_self_contained, t2.v09_default_limits_hold,
             w01_detects_miner, w02_auto_sets_lazy_load, w03_overrides, w04_warns_once_on_lazy_books,
             p01_same_orders_as_2gpt, p02_each_reason, p03_card)
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
