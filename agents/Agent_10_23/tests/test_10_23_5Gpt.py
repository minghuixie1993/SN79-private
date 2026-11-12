# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_23_5Gpt (no simulator).

    python agents/Agent_10_23/tests/test_10_23_5Gpt.py

First the whole MyAgent10_23_4Gpt suite against 5Gpt with its own changes switched off (shield off, multipliers
capped at 4Gpt's x2), then the checks for what 5Gpt adds (Y01-Y08): it still sends exactly the real 4Gpt file's
instructions when neutral, the shield cancels or widens only inside its window, fills are booked by reseed phase,
the caps hold (also on restored state), and on recorded mainnet rounds it runs cleanly and lazily == fully parsed.
"""
import glob
import json
import math
import os
import random
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import MyAgent10_23_5Gpt as m5                                   # noqa: E402

# 5Gpt with its own changes off: no shield, 4Gpt's multiplier caps.
NEUTRAL5 = dict(adapt_max=2.0, book_mult_max=2.0)


class Neutral5(m5.MyAgent10_23_5Gpt):
    def initialize(self):
        for name, value in NEUTRAL5.items():
            if getattr(self.config, name, None) is None:
                setattr(self.config, name, value)
        super().initialize()


# The 4Gpt suite imports MyAgent10_23_4Gpt; point it at the neutral 5Gpt.
alias = types.ModuleType("MyAgent10_23_4Gpt")
alias.__dict__.update(m5.__dict__)
alias.MyAgent10_23_4Gpt = Neutral5
sys.modules["MyAgent10_23_4Gpt"] = alias
import test_10_23_4Gpt as t4                                     # noqa: E402

t3, base, onegpt, t2, check, make = t4.t3, t4.base, t4.onegpt, t4.t2, t4.check, t4.make
N, SEC, BUY, SELL = base.N, base.SEC, base.BUY, base.SELL
Agent = m5.MyAgent10_23_5Gpt
MAINNET_REC = "/home/Trustvs/workspace/sn-79/agents/data/mainnet_records"


def defaults(**extra):
    return make(Agent, persist_state=0, **extra)


def load_real_4gpt():
    """MyAgent10_23_4Gpt.py itself (sys.modules' MyAgent10_23_4Gpt is the 5Gpt alias above)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("real_4gpt", os.path.join(os.path.dirname(HERE), "MyAgent10_23_4Gpt.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def y01_neutral_equals_4gpt():
    """With fills, fee spikes and pacing pauses, a neutral 5Gpt sends exactly the real 4Gpt file's instructions."""
    real = load_real_4gpt()
    rng = random.Random(29)
    four, five = make(real.MyAgent10_23_4Gpt, persist_state=0), make(Neutral5, persist_state=0)
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
            acct = base.account(*wallets[b], resting[b])
            acct.fees.maker_fee_rate = 0.03 if rng.random() < 0.05 else 0.0
            accounts[b] = acct
        calls = []
        for agent in (four, five):
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
                    ev = N(makerAgentId=1, takerAgentId=-2 if rng.random() < 0.5 else 7, bookId=b, timestamp=t,
                           price=o.price, quantity=o.quantity, side=SELL if o.side == BUY else BUY,
                           makerFee=0.0, makerOrderId=o.id)
                    four.onTrade(ev, "v1"); five.onTrade(ev, "v1")
    check("Y01 neutral 5Gpt sends exactly the real 4Gpt's instructions over 400 rounds with fills and fee spikes",
          mismatches == 0 and placed > 0, f"{mismatches} rounds differ, {placed} placements")


def _resting_round(agent, t, book_id=0, mid=300.0):
    """One round on a book holding one resting bid and one resting ask; returns the instructions."""
    orders = [base.Order(11, BUY, mid - 0.05, 1.0, t - SEC), base.Order(12, SELL, mid + 0.05, 1.0, t - SEC)]
    bk = base.book(mid, 3) if book_id < 96 else base.book(30.0, 4)
    acct = base.account(83.33, 25_000.0, 83.33, orders) if book_id < 96 else base.account(833.3, 25_000.0, 833.3, orders)
    return base.quote_once(agent, bk, acct, book_id=book_id, t=t)


def y02_shield_cancel_window():
    g = defaults(shield_pre_s=1, shield_post_s=1)
    at = {s: _resting_round(g, (1200 + s) * SEC) for s in (28, 29, 30, 31, 45)}   # phases 28, 29, 0, 1, 15
    only_cancel = {s: bool(base.cancels(c)) and not base.places(c) for s, c in at.items()}
    ok = (only_cancel[29] and only_cancel[30] and not only_cancel[28] and not only_cancel[31]
          and not only_cancel[45] and base.places(at[31]) and g.plan_stats["v1"].get("shield_cancel_c0") == 2)
    check("Y02 shield pre 1 / post 1: cancels only in the rounds at second 29 and 0 of the cycle, quotes otherwise",
          ok, {s: (len(base.cancels(c)), len(base.places(c))) for s, c in at.items()})


def y03_shield_per_class_and_off():
    g = defaults(shield_pre_s_c1=2)
    c0 = _resting_round(g, 1229 * SEC, book_id=0)
    c1 = _resting_round(g, 1229 * SEC, book_id=100)
    off = defaults()
    plain = _resting_round(off, 1229 * SEC, book_id=100)
    ok = (bool(base.places(c0)) and bool(base.cancels(c1)) and not base.places(c1) and bool(base.places(plain))
          and off._shield(100, 1229 * SEC) is None)
    check("Y03 shield_pre_s_c1 shields class 1 only; the default (all 0) never shields",
          ok, f"c0 {len(base.places(c0))} places, c1 {c1}, default {len(base.places(plain))} places")


def y04_shield_widen():
    def quotes(**p):
        g = defaults(**p)
        bk = base.book(300.0, 3)
        g.accounts = {0: base.account(83.33, 25_000.0, 83.33)}
        base.gmod._REQUEST_ACCOUNTS.set(None)
        memory = g._remember("v1", 0, bk, 1229 * SEC)
        memory["variance"] = (10e-4) ** 2 / 20
        memory["book_mult"] = 1.1
        resp = base.Resp()
        g._quote_book(resp, "v1", 0, bk, memory)
        p_ = {c[2]: c[4] for c in base.places(resp.calls)}
        return p_.get(0), p_.get(1), memory.get("book_mult")
    bid0, ask0, m0 = quotes()
    bid1, ask1, m1 = quotes(shield_pre_s=1, shield_widen=1.0)
    ok = None not in (bid0, ask0, bid1, ask1) and bid1 < bid0 and ask1 > ask0 and m0 == m1 == 1.1
    check("Y04 shield_widen 1: inside the window the quotes sit further out; the book's own multiplier is restored",
          ok, f"plain {bid0}/{ask0}, widened {bid1}/{ask1}, book_mult after {m1}")


def y05_phase_stats_on_card():
    g = defaults()
    bk = base.book(300.0, 3)
    acct = base.account(83.33, 25_000.0, 83.33)
    base.quote_once(g, bk, acct, t=1199 * SEC)                    # mid 300 remembered
    # A buy filled 0.05 s after the boundary at 1200 s by a miner, one at 1210 s by the background.
    for t, taker in ((1200 * SEC + 50_000_000, 7), (1210 * SEC, -2)):
        g.onTrade(N(makerAgentId=1, takerAgentId=taker, bookId=0, timestamp=t, price=299.9, quantity=1.0,
                    side=SELL, makerFee=0.0, makerOrderId=5), "v1")
    # 26 s later the mid has fallen through the fill price: both buys captured edge and marked out below it.
    before = (bk.bids[0].price + bk.asks[0].price) / 2
    after_book = base.book(299.8, 3)
    after = (after_book.bids[0].price + after_book.asks[0].price) / 2
    base.quote_once(g, after_book, acct, t=1236 * SEC)
    card = g._plan_card("v1")
    tail = card[card.index("5GPT"):]
    markout, capture = (after - 299.9) / 299.9 * 1e4, (before - 299.9) / 299.9 * 1e4
    one = f"markout {markout:+.2f} capture {capture:+.2f} q 0.00"
    ok = (f"0-1s n 1 {one} bg 0%" in tail and f"5-28s n 1 {one} bg 100%" in tail
          and "quality uid ~0.00" in tail and "S3 cap cuts below 20%" in tail and markout < 0 < capture)
    check("Y05 fills booked by reseed phase on the card (markout, capture, quality, background share), uid quality",
          ok, tail)


def y06_caps():
    g = defaults()
    bk = base.book(300.0, 3)
    acct = base.account(83.33, 25_000.0, 83.33)
    for r in range(400):                                          # every fill is a maker buy 10 bps over the mid
        t = (2000 + r) * SEC
        base.quote_once(g, base.book(300.0, 3), acct, t=t)
        g.onTrade(N(makerAgentId=1, takerAgentId=7, bookId=0, timestamp=t, price=300.3, quantity=1.0,
                    side=SELL, makerFee=0.0, makerOrderId=r), "v1")
    cls, book = g.spread_mult.get(("v1", 0), 1.0), g.memory[("v1", 0)].get("book_mult", 1.0)
    neutral = make(Neutral5, persist_state=0)
    check("Y06 defaults cap the class and book multipliers at x1.25 (4Gpt: x2) under steady bad markouts",
          abs(cls - 1.25) < 1e-9 and abs(book - 1.25) < 1e-9 and neutral.adapt_max == 2.0
          and neutral.book_mult_max == 2.0, f"class x{cls:.3f}, book x{book:.3f}")


def y07_restored_state_capped():
    tmp = tempfile.mkdtemp(prefix="g5state_")
    four = make(Neutral5, persist_state=1)
    four._state_file = m5.Path(tmp) / "state.json"
    base.quote_once(four, base.book(300.0, 3), base.account(83.33, 25_000.0, 83.33), t=1000 * SEC)
    four.spread_mult[("v1", 0)] = 1.6
    four.memory[("v1", 0)]["book_mult"] = 1.9
    four._save_state("v1", "sim-A", 1000 * SEC)
    g = make(Agent, persist_state=1)
    g._state_file = four._state_file
    g.accounts = {0: base.account(83.33, 25_000.0, 83.33)}
    base.gmod._REQUEST_ACCOUNTS.set(None)
    g.respond(N(books={0: base.book(300.0, 3)}, timestamp=1060 * SEC, dendrite=N(hotkey="v1"),
                config=N(simulation_id="sim-A")))
    got = (g.spread_mult.get(("v1", 0)), g.memory[("v1", 0)].get("book_mult"))
    check("Y07 restored multipliers are held within the new caps (x1.6 / x1.9 saved -> x1.25 / x1.25)",
          got == (1.25, 1.25) and make(Agent)._state_file.name == "MyAgent10_23_5Gpt_state.json", str(got))


def y09_one_sided_shield():
    """shield_side 1: inside the window only the side the fundamental pulls the price through is cancelled; the
    other side is quoted as usual; without a fundamental both sides are cancelled."""
    def one(fundamental):
        g = defaults(shield_post_s=2, shield_side=1)
        t = 1230 * SEC                                                # second 0 of the cycle
        orders = [base.Order(11, BUY, 299.90, 1.0, t - SEC), base.Order(12, SELL, 300.10, 1.0, t - SEC)]
        calls = base.quote_once(g, base.book(300.0, 3, fundamental=fundamental),
                                base.account(83.33, 25_000.0, 83.33, orders), t=t)
        cancelled = {i for c in base.cancels(calls) for i in c[2]}
        sides = {c[2] for c in base.places(calls)}
        return cancelled, sides
    up, down, none = one(303.0), one(297.0), one(None)
    ok = (12 in up[0] and 1 not in up[1] and 0 in up[1]          # above the mid: asks pulled, a bid placed
          and 11 in down[0] and 0 not in down[1] and 1 in down[1]
          and none[0] == {11, 12} and not none[1])
    check("Y09 one-sided shield: fundamental above the mid pulls only asks, below only bids, none -> both",
          ok, f"up {up}, down {down}, none {none}")


def _mainnet_frames(limit):
    sys.path.insert(0, "/home/Trustvs/workspace/sn-79/agents/mainnet")
    from state_tap import read_frames
    files = sorted(glob.glob(f"{MAINNET_REC}/*/*.frames.zst"))
    return [f for path in files for f in read_frames(path)][:limit]


def y08_recorded_mainnet_rounds():
    """On recorded mainnet rounds (128 books, two classes), delivered as the miner delivers them: the default 5Gpt
    lazily parsed sends exactly what a fully parsed one does, no book fails, and a shielded 5Gpt cancels every
    book in the rounds at second 29 of the cycle and places nothing there."""
    frames = _mainnet_frames(70)
    if not frames:
        check("Y08 recorded mainnet rounds", True, "SKIPPED: no recording at " + MAINNET_REC)
        return
    import replay_agent as ra
    from taos.im.protocol import MarketSimulationStateUpdate
    uid = int(next(iter(ra.decode_raw(frames[0]).get("accounts") or {0: None})))
    path = os.path.join(os.path.dirname(HERE), "MyAgent10_23_5Gpt.py")
    common = {"persist_state": 0.0, "event_lookback_minutes": 1.0}
    agents = {"lazy": (ra.load_agent(path, uid, dict(common, lazy_mode=1.0), m5.Path(tempfile.mkdtemp())), True),
              "full": (ra.load_agent(path, uid, dict(common, lazy_mode=0.0), m5.Path(tempfile.mkdtemp())), False),
              "shield": (ra.load_agent(path, uid, dict(common, shield_pre_s=1.0, shield_post_s=0.0),
                                       m5.Path(tempfile.mkdtemp())), True)}
    differ, placed, shield_bad, shield_rounds = 0, 0, 0, 0
    shield_agent = agents["shield"][0]
    for frame in frames:
        placed_before = shield_agent.plan_stats.get(None, {}).get("placed", 0)
        out = {}
        for name, (agent, is_lazy) in agents.items():
            syn = MarketSimulationStateUpdate(timestamp=frame["timestamp"], version=frame.get("version"),
                                              compression_engine=frame.get("engine") or "lz4")
            syn.compressed = frame["compressed"]
            syn.dendrite.hotkey = frame["validator"]
            syn.decompress(lazy=is_lazy)
            out[name] = agent.handle(syn).model_dump(mode="json").get("instructions") or []
        differ += json.dumps(out["lazy"], sort_keys=True, default=str) != json.dumps(out["full"], sort_keys=True, default=str)
        placed += len(out["lazy"])
        if (frame["timestamp"] // SEC) % 30 == 29:
            shield_rounds += 1
            shield_bad += shield_agent.plan_stats.get(None, {}).get("placed", 0) > placed_before
    failed = sum(v.get("non_finite", 0) for a, _ in agents.values() for v in a.plan_stats.values())
    check("Y08 recorded mainnet rounds: lazy == full, no failed book, shield places nothing at second 29",
          differ == 0 and placed > 0 and failed == 0 and shield_rounds > 0 and shield_bad == 0,
          f"{len(frames)} rounds, {differ} differ, {placed} instructions, failed {failed}, "
          f"shield rounds {shield_rounds} with placements {shield_bad}")


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
             y01_neutral_equals_4gpt, y02_shield_cancel_window, y03_shield_per_class_and_off, y04_shield_widen,
             y05_phase_stats_on_card, y06_caps, y07_restored_state_capped, y08_recorded_mainnet_rounds,
             y09_one_sided_shield)
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
