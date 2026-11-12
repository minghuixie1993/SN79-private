# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_23_6Gpt (no simulator).

    python agents/Agent_10_23/tests/test_10_23_6Gpt.py

First the whole MyAgent10_23_5Gpt suite against 6Gpt with quiet sizing off (it must still behave as 5Gpt), then the
checks for what 6Gpt adds (Z01-Z06): exactly the real 5Gpt file's orders when off, the size multiplied only inside the
quiet window and only for its class, every 1Gpt limit still holding on the larger size, and the learned state seeded
from 5Gpt's file.
"""
import math
import os
import random
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import MyAgent10_23_6Gpt as m6                                   # noqa: E402


class Neutral6(m6.MyAgent10_23_6Gpt):
    """6Gpt with quiet sizing off (its default): the 5Gpt suite runs against it."""


# The 5Gpt suite imports MyAgent10_23_5Gpt; point it at 6Gpt.
alias = types.ModuleType("MyAgent10_23_5Gpt")
alias.__dict__.update(m6.__dict__)
alias.MyAgent10_23_5Gpt = Neutral6
sys.modules["MyAgent10_23_5Gpt"] = alias
import test_10_23_5Gpt as t5                                     # noqa: E402

t4, t3, base, onegpt, t2, check, make = t5.t4, t5.t3, t5.base, t5.onegpt, t5.t2, t5.check, t5.make
N, SEC, BUY, SELL = base.N, base.SEC, base.BUY, base.SELL
Agent = m6.MyAgent10_23_6Gpt
LIVE = dict(shield_pre_s=1, shield_post_s=1)


def defaults(**extra):
    return make(Agent, persist_state=0, **extra)


def load_real_5gpt():
    """MyAgent10_23_5Gpt.py itself (sys.modules' MyAgent10_23_5Gpt is the 6Gpt alias above)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("real_5gpt", os.path.join(os.path.dirname(HERE), "MyAgent10_23_5Gpt.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def z01_off_equals_5gpt():
    """With the live shield and quiet sizing off, 6Gpt sends exactly the real 5Gpt file's instructions."""
    real = load_real_5gpt()
    rng = random.Random(31)
    five, six = make(real.MyAgent10_23_5Gpt, persist_state=0, **LIVE), make(Agent, persist_state=0, **LIVE)
    mids = {0: 300.0, 9: 301.0, 100: 29.8, 120: 30.1}
    wallets = {b: [50_000 / 2 / m, 25_000.0, 50_000 / 2 / m] for b, m in mids.items()}
    resting = {b: [] for b in mids}
    next_id, mismatches, placed = [1], 0, 0
    for r in range(400):
        t = (700 + r) * SEC
        books, accounts = {}, {}
        for b in mids:
            mids[b] *= math.exp(rng.gauss(0, 0.0005))
            books[b] = base.book(mids[b], rng.choice([2, 3, 4]), fundamental=mids[b] * (1 + rng.gauss(0, 0.003)),
                                 bid_q=rng.uniform(1, 20), ask_q=rng.uniform(1, 20))
            accounts[b] = base.account(*wallets[b], resting[b])
        calls = []
        for agent in (five, six):
            agent.accounts = dict(accounts)
            base.gmod._REQUEST_ACCOUNTS.set(None)
            calls.append(agent.respond(base.state(books, t)).calls)
        mismatches += calls[0] != calls[1]
        placed += len(base.places(calls[0]))
        for c in calls[0]:
            if c[0] == "cancel":
                resting[c[1]] = [o for o in resting[c[1]] if o.id not in c[2]]
            else:
                resting[c[1]].append(base.Order(next_id[0], c[2], c[4], c[3], t))
                next_id[0] += 1
        for b in mids:
            for o in list(resting[b]):
                if rng.random() < 0.15:
                    w = wallets[b]
                    w[0] += o.quantity if o.side == BUY else -o.quantity
                    w[1] -= o.quantity * o.price if o.side == BUY else -o.quantity * o.price
                    resting[b].remove(o)
                    ev = N(makerAgentId=1, takerAgentId=-2, bookId=b, timestamp=t, price=o.price, quantity=o.quantity,
                           side=SELL if o.side == BUY else BUY, makerFee=0.0, makerOrderId=o.id)
                    five.onTrade(ev, "v1"); six.onTrade(ev, "v1")
    check("Z01 quiet sizing off: 6Gpt sends exactly the real 5Gpt's instructions (live shield on) over 400 rounds",
          mismatches == 0 and placed > 0, f"{mismatches} rounds differ, {placed} placements")


def _sizes(agent, second, book_id=0):
    """Quantities placed on a fresh book in the round at `second` of the reseed cycle."""
    bk = base.book(300.0, 3) if book_id < 96 else base.book(30.0, 4)
    acct = base.account(83.33, 25_000.0, 83.33) if book_id < 96 else base.account(833.3, 25_000.0, 833.3)
    return sorted(c[3] for c in base.places(base.quote_once(agent, bk, acct, book_id=book_id, t=(1200 + second) * SEC)))


def z02_window():
    off, on = defaults(), defaults(quiet_size_mult=1.5)
    got = {s: (_sizes(off, s), _sizes(on, s)) for s in (2, 5, 10, 27, 28)}
    inside = all(abs(b - 1.5 * a) <= 2e-4 for s in (5, 10, 27) for a, b in zip(*got[s]))
    outside = all(got[s][0] == got[s][1] for s in (2, 28))
    check("Z02 quiet_size_mult 1.5: sizes x1.5 from second 5 to 27 of the cycle, unchanged at 2 and 28",
          inside and outside and all(got[s][0] for s in got), {s: v for s, v in got.items()})


def z03_per_class():
    g = defaults(quiet_size_mult_c1=1.5)
    plain = defaults()
    c0 = (_sizes(plain, 10, 0), _sizes(g, 10, 0))
    c1 = (_sizes(plain, 10, 100), _sizes(g, 10, 100))
    ok = c0[0] == c0[1] and c1[0] and all(abs(b - 1.5 * a) <= 2e-4 for a, b in zip(*c1))
    check("Z03 quiet_size_mult_c1 sizes class 1 only", ok, f"c0 {c0}, c1 {c1}")


def z04_limits_hold():
    rng = random.Random(43)
    g = defaults(quiet_size_mult=5.0)
    worst_cap, worst_band = 0.0, -1.0
    for r in range(300):
        mid = 300 * math.exp(rng.gauss(0, 0.01))
        wealth = 50_000.0
        initial = 0.3 * wealth / mid
        units = initial + rng.uniform(-0.3, 0.3) * wealth / mid
        bk = base.book(mid, rng.choice([1, 2, 3, 4]))
        calls = base.quote_once(g, bk, base.account(units, wealth - units * mid, initial), t=(1210 + 30 * r) * SEC)
        book_mid = (bk.bids[0].price + bk.asks[0].price) / 2
        w = units * book_mid + (wealth - units * mid)
        gap = (units - initial) * book_mid / w
        for c in base.places(calls):
            worst_cap = max(worst_cap, c[3] * c[4] / (g.max_order_fraction * w))
            worst_band = max(worst_band, (gap if c[2] == 0 else -gap) + c[3] * c[4] / w)
    check("Z04 quiet_size_mult 5 in the quiet window, 300 random states: order cap and post-fill band still hold",
          worst_cap <= 1 + 1e-9 and worst_band <= 0.25 + 1e-9, f"cap x{worst_cap:.4f}, gap after fill {worst_band:.4f}")


def _with_output_dir(path, **params):
    agent = object.__new__(Agent)
    agent.output_dir = path
    agent.config = N(**params)
    agent.initialize()
    return agent


def z05_state_seeded_from_5gpt():
    tmp = tempfile.mkdtemp(prefix="g6state_")
    seed = m6.Path(tmp) / "MyAgent10_23_5Gpt_state.json"
    seed.write_text('{"v1": {"sim_id": "s", "timestamp": 1, "classes": {}, "books": {}}}')
    a = _with_output_dir(tmp, persist_state=1)
    copied = (m6.Path(tmp) / "MyAgent10_23_6Gpt_state.json").read_text() == seed.read_text()
    (m6.Path(tmp) / "MyAgent10_23_6Gpt_state.json").write_text("{}")
    _with_output_dir(tmp, persist_state=1)        # its own file exists now: left alone
    kept = (m6.Path(tmp) / "MyAgent10_23_6Gpt_state.json").read_text() == "{}"
    other = tempfile.mkdtemp(prefix="g6none_")
    _with_output_dir(other, persist_state=0)       # persistence off: nothing written
    check("Z05 learned state seeded from 5Gpt's file once, never over 6Gpt's own, not when persistence is off",
          copied and kept and a._state_file.name == "MyAgent10_23_6Gpt_state.json" and not os.listdir(other),
          f"copied {copied}, kept {kept}, files without persistence {os.listdir(other)}")


def z07_restored_state_capped():
    """5Gpt's Y07 with 6Gpt's own state file: x1.6 / x1.9 saved are restored held within the x1.25 caps."""
    tmp = tempfile.mkdtemp(prefix="g6caps_")
    saver = make(Agent, persist_state=1, adapt_max=2.0, book_mult_max=2.0)
    saver._state_file = m6.Path(tmp) / "state.json"
    base.quote_once(saver, base.book(300.0, 3), base.account(83.33, 25_000.0, 83.33), t=1000 * SEC)
    saver.spread_mult[("v1", 0)] = 1.6
    saver.memory[("v1", 0)]["book_mult"] = 1.9
    saver._save_state("v1", "sim-A", 1000 * SEC)
    g = make(Agent, persist_state=1)
    g._state_file = saver._state_file
    g.accounts = {0: base.account(83.33, 25_000.0, 83.33)}
    base.gmod._REQUEST_ACCOUNTS.set(None)
    g.respond(N(books={0: base.book(300.0, 3)}, timestamp=1060 * SEC, dendrite=N(hotkey="v1"),
                config=N(simulation_id="sim-A")))
    got = (g.spread_mult.get(("v1", 0)), g.memory[("v1", 0)].get("book_mult"))
    check("Z07 restored multipliers held within the caps; 6Gpt keeps its own state file",
          got == (1.25, 1.25) and make(Agent, persist_state=0)._state_file.name == "MyAgent10_23_6Gpt_state.json", str(got))


CLASSES3 = [N(name="simulation_0", books=list(range(64))), N(name="simulation_1", books=list(range(64, 96))),
            N(name="simulation_2", books=list(range(96, 128)))]


def z08_layout_change():
    """96 / 32 -> 64 / 32 / 32: announced mid-run (same simulation_id) it is ignored, as the mainnet validator sent it
    early on 23 October; at a new run book 70 moves to class 1 (class 1's half spread), book 100 to class 2, and
    the per-class multipliers start over. 4Gpt / 5Gpt keep book 70 in class 0 for good."""
    g = defaults()
    acct = lambda: base.account(83.33, 25_000.0, 83.33)
    g.simulation_config = N(simulation_id="run-A")
    base.quote_once(g, base.book(300.0, 3), acct(), book_id=70, t=1210 * SEC)
    before = (g._class_index(70), g._params(70)["half_spread"], g._class_index(100))
    g.spread_mult[("v1", 0)] = 1.2
    g.asset_classes = lambda: CLASSES3
    base.quote_once(g, base.book(300.0, 3), acct(), book_id=70, t=1211 * SEC)
    announced = (g._class_index(70), g.spread_mult.get(("v1", 0)), g.plan_stats[None].get("layout_announced"))
    g.simulation_config = N(simulation_id="run-B")
    base.quote_once(g, base.book(300.0, 3), acct(), book_id=70, t=5 * SEC)
    after = (g._class_index(70), g._params(70)["half_spread"], g._class_index(100))
    old = make(t5.load_real_4gpt().MyAgent10_23_4Gpt, persist_state=0)
    base.quote_once(old, base.book(300.0, 3), base.account(83.33, 25_000.0, 83.33), book_id=70, t=1210 * SEC)
    old.asset_classes = lambda: CLASSES3
    base.quote_once(old, base.book(300.0, 3), base.account(83.33, 25_000.0, 83.33), book_id=70, t=1211 * SEC)
    check("Z08 a layout announced mid-run is ignored; at a new run every book's class and settings are re-read",
          before == (0, 0.0006, 1) and announced == (0, 1.2, 1) and after == (1, 0.0005, 2)
          and ("v1", 0) not in g.spread_mult and g.plan_stats[None].get("layout_changes") == 1
          and old._class_index(70) == 0,
          f"before {before}, announced {announced}, after {after}, 4Gpt keeps book 70 in class {old._class_index(70)}")


def z09_reseed_from_config():
    g = defaults()
    g.simulation_config = N(fp_seed_interval=20_000_000_000)
    base.quote_once(g, base.book(300.0, 3), base.account(83.33, 25_000.0, 83.33), t=1210 * SEC)
    fixed = defaults(reseed_s=30)
    fixed.simulation_config = N(fp_seed_interval=20_000_000_000)
    base.quote_once(fixed, base.book(300.0, 3), base.account(83.33, 25_000.0, 83.33), t=1210 * SEC)
    none = defaults()
    base.quote_once(none, base.book(300.0, 3), base.account(83.33, 25_000.0, 83.33), t=1210 * SEC)
    check("Z09 the reseed interval follows the config's fp_seed_interval; reseed_s overrides; 30 s without a config",
          g.reseed_s == 20.0 and fixed.reseed_s == 30.0 and none.reseed_s == 30.0,
          f"config {g.reseed_s}, override {fixed.reseed_s}, none {none.reseed_s}")


def z06_card():
    g = defaults(quiet_size_mult=1.5)
    _sizes(g, 10)
    card = g._plan_card("v1")
    check("Z06 the card shows the quiet sizing and how often it applied", "6GPT quiet size c0 x1.5 on 1 book-rounds" in card,
          card[card.find("6GPT"):])


if __name__ == "__main__":
    base.t00_t07_t09_t13()
    t2.t00_against_repo_v3()
    tests = (base.t01, base.t02, base.t03, base.t04, base.t05, base.t06, base.t08, base.t09_extreme, base.t10,
             base.t11, base.t12, base.t14, base.default_regression_limits,
             onegpt.u01_oversize_replaced, onegpt.u02_smaller_kept, onegpt.u03_nan_prediction,
             onegpt.u04_rejection_attributed, onegpt.u05_fast_fill_counted, onegpt.u06_default_limits_hold,
             t2.v01_grid_nudge, t2.v02_fees, t2.v03_pacing, t2.v04_expiry, t2.v05_bounded_lean,
             t2.v06_book_multiplier, t2.v07_counterparties, t2.v09_default_limits_hold,
             t3.p01_same_orders_as_2gpt, t3.p02_each_reason, t3.p03_card,
             t4.x01_class_defaults, t4.x02_simulation_2, t4.x03_latency_guard, t4.x04_persist_roundtrip,
             t4.x05_timing_and_delay, t4.x06_lazy_mode_float, t4.x08_default_limits_hold,
             t5.y01_neutral_equals_4gpt, t5.y02_shield_cancel_window, t5.y03_shield_per_class_and_off,
             t5.y04_shield_widen, t5.y05_phase_stats_on_card, t5.y06_caps, t5.y09_one_sided_shield,
             # 5Gpt's Y07 also checks 5Gpt's own state file name; Z07 is the same check for 6Gpt's file
             z01_off_equals_5gpt, z02_window, z03_per_class, z04_limits_hold, z05_state_seeded_from_5gpt, z06_card,
             z07_restored_state_capped, z08_layout_change, z09_reseed_from_config)
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
