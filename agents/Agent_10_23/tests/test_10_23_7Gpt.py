# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_23_7Gpt (no simulator).

    python agents/Agent_10_23/tests/test_10_23_7Gpt.py

First the whole MyAgent10_23_6Gpt suite against 7Gpt with its additions off (it must still behave as 6Gpt), then the
checks for what 7Gpt adds (W01-W06): exactly the real 6Gpt file's orders when off, the toxic-flow edge widening only on
miner-taken books and only above toxic_free, own trades left out, raw (lazy) and parsed events read alike, the
per-class and convex inventory skew, and recorded mainnet rounds run cleanly with it on.
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
import MyAgent10_23_7Gpt as m7                                   # noqa: E402


class Neutral7(m7.MyAgent10_23_7Gpt):
    """7Gpt with its additions off (their defaults): the 6Gpt suite runs against it."""


alias = types.ModuleType("MyAgent10_23_6Gpt")
alias.__dict__.update(m7.__dict__)
alias.MyAgent10_23_6Gpt = Neutral7
sys.modules["MyAgent10_23_6Gpt"] = alias
import test_10_23_6Gpt as t6                                     # noqa: E402

t5, t4, t3, base, onegpt, t2, check, make = t6.t5, t6.t4, t6.t3, t6.base, t6.onegpt, t6.t2, t6.check, t6.make
N, SEC, BUY, SELL = base.N, base.SEC, base.BUY, base.SELL
Agent = m7.MyAgent10_23_7Gpt
LIVE6 = dict(shield_pre_s=1, shield_post_s=1, quiet_size_mult_c0=2, quiet_size_mult_c1=1.25)


def defaults(**extra):
    return make(Agent, persist_state=0, **extra)


def load_real_6gpt():
    import importlib.util
    spec = importlib.util.spec_from_file_location("real_6gpt", os.path.join(os.path.dirname(HERE), "MyAgent10_23_6Gpt.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def w01_off_equals_6gpt():
    real = load_real_6gpt()
    rng = random.Random(37)
    six, seven = make(real.MyAgent10_23_6Gpt, persist_state=0, **LIVE6), make(Agent, persist_state=0, **LIVE6)
    mids = {0: 300.0, 9: 301.0, 100: 29.8, 120: 30.1}
    wallets = {b: [50_000 / 2 / m, 25_000.0, 50_000 / 2 / m] for b, m in mids.items()}
    resting = {b: [] for b in mids}
    next_id, mismatches, placed = [1], 0, 0
    for r in range(400):
        t = (700 + r) * SEC
        books, accounts = {}, {}
        for b in mids:
            mids[b] *= math.exp(rng.gauss(0, 0.0005))
            bk = base.book(mids[b], rng.choice([2, 3, 4]), fundamental=mids[b] * (1 + rng.gauss(0, 0.003)),
                           bid_q=rng.uniform(1, 20), ask_q=rng.uniform(1, 20))
            bk.events = [N(type="t", takerAgentId=rng.choice([-3, 50]), makerAgentId=-4, quantity=1.0, price=mids[b], side=0)]
            books[b] = bk
            accounts[b] = base.account(*wallets[b], resting[b])
        calls = []
        for agent in (six, seven):
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
                    six.onTrade(ev, "v1"); seven.onTrade(ev, "v1")
    check("W01 additions off: 7Gpt sends exactly the real 6Gpt's instructions (live settings) over 400 rounds",
          mismatches == 0 and placed > 0, f"{mismatches} rounds differ, {placed} placements")


def _book_with(takers, mid=300.0):
    bk = base.book(mid, 3)
    bk.events = [N(type="t", takerAgentId=a, makerAgentId=m, quantity=1.0, price=mid, side=0) for a, m in takers]
    return bk


def _quotes(agent, bk, t=1210 * SEC):
    calls = base.quote_once(agent, bk, base.account(83.33, 25_000.0, 83.33), t=t)
    return {c[2]: c[4] for c in base.places(calls)}


def w02_toxic_edge():
    miners = [(50, -4)] * 9 + [(-3, -4)]            # 90% miner takers
    background = [(-3, -4)] * 10
    own = [(1, -4)] * 10 + [(-3, -4)]               # this agent's own takes do not count
    plain = _quotes(defaults(), _book_with(miners))
    wide = _quotes(defaults(toxic_widen=1.0), _book_with(miners))
    calm = _quotes(defaults(toxic_widen=1.0), _book_with(background))
    mine = _quotes(defaults(toxic_widen=1.0), _book_with(own))
    ok = (wide[0] < plain[0] and wide[1] > plain[1] and calm == plain and mine == plain)
    check("W02 toxic edge: 90% miner takers widen both quotes; background-only or own trades leave them",
          ok, f"plain {plain}, miner-heavy {wide}, background {calm}, own {mine}")


def w03_raw_events():
    g = defaults(toxic_widen=1.0)
    raw = N(_raw={"e": [{"y": "t", "Ta": 50, "Ma": -4, "q": 2.0, "p": 300.0}, {"y": "t", "Ta": -3, "Ma": -4, "q": 1.0, "p": 300.0},
                        {"y": "t", "Ta": 1, "Ma": -4, "q": 5.0, "p": 300.0}, {"y": "o", "q": 9.0, "p": 1.0}]})
    parsed = N(events=[N(type="t", takerAgentId=50, makerAgentId=-4, quantity=2.0, price=300.0),
                       N(type="t", takerAgentId=-3, makerAgentId=-4, quantity=1.0, price=300.0),
                       N(type="t", takerAgentId=1, makerAgentId=-4, quantity=5.0, price=300.0)])
    check("W03 taker mix read alike from raw (lazy) and parsed events, own trades and non-trades left out",
          g._taker_mix(raw) == g._taker_mix(parsed) == (600.0, 900.0), f"{g._taker_mix(raw)} {g._taker_mix(parsed)}")


def _skew_used(book_id, **p):
    """The inventory skew V3's pricing reads (self.skew) while a long book is quoted, and after it."""
    g = defaults(**p)
    seen = []
    original = g._fill_balance                          # called right where V3 computes the skew shift
    g._fill_balance = lambda *a, **k: (seen.append(g.skew), original(*a, **k))[1]
    bk = base.book(30.0, 4) if book_id >= 96 else base.book(300.0, 3)
    acct = (base.account(833.3 + 150.0, 25_000.0 - 150.0 * 30, 833.3) if book_id >= 96
            else base.account(83.33 + 15.0, 25_000.0 - 15.0 * 300, 83.33))   # long ~9% of wealth
    base.quote_once(g, bk, acct, book_id=book_id, t=1210 * SEC)
    return (seen[0] if seen else None), g.skew


def w04_class_skew():
    plain = _skew_used(100)
    c1 = _skew_used(100, skew_c1=8)
    c0_on_c1 = _skew_used(100, skew_c0=8)
    c0 = _skew_used(0, skew_c0=8)
    convex = _skew_used(100, skew_convex=2.0)         # inventory gap ~0.09 of a 0.25 band: x(1 + 2 x 0.36)
    ok = (plain == (4.0, 4.0) and c1 == (8.0, 4.0) and c0_on_c1 == (4.0, 4.0) and c0 == (8.0, 4.0)
          and convex[0] is not None and 6.0 < convex[0] < 7.5 and convex[1] == 4.0)
    check("W04 per-class skew (skew_c1 on class 1 only, skew_c0 on class 0 only) and the convex term; V3's skew "
          "restored after the quote", ok, f"plain {plain}, skew_c1 {c1}, skew_c0 on c1 {c0_on_c1}, skew_c0 on c0 {c0}, "
          f"convex {convex}")


def w07_max_inv_ratio():
    """A class 1 book long ~9% of its wealth: max_inv_ratio_c1 0.05 stops its bid (only the ask is quoted);
    the default 0.25 keeps both sides; class 0 is unaffected by the class 1 limit; the base band is restored."""
    def sides(book_id, **p):
        g = defaults(**p)
        if book_id >= 96:
            bk, acct = base.book(30.0, 4), base.account(833.3 + 150.0, 25_000.0 - 150.0 * 30, 833.3)
        else:
            bk, acct = base.book(300.0, 3), base.account(83.33 + 15.0, 25_000.0 - 15.0 * 300, 83.33)
        calls = base.quote_once(g, bk, acct, book_id=book_id, t=1210 * SEC)
        return sorted({c[2] for c in base.places(calls)}), g.inventory_band
    plain = sides(100)
    tight = sides(100, max_inv_ratio_c1=0.05)
    c0 = sides(0, max_inv_ratio_c1=0.05)
    every = sides(0, max_inv_ratio=0.05)
    check("W07 max_inv_ratio_c1 0.05: a 9%-long class 1 book quotes only its ask; class 0 untouched; plain "
          "max_inv_ratio applies to every class; inventory_band restored after the quote",
          plain == ([0, 1], 0.25) and tight == ([1], 0.25) and c0 == ([0, 1], 0.25) and every == ([1], 0.25),
          f"default {plain}, c1 limit {tight}, class 0 with c1 limit {c0}, all-class limit {every}")


def w05_card():
    g = defaults(toxic_widen=1.0)
    _quotes(g, _book_with([(50, -4)] * 9 + [(-3, -4)]))
    card = g._plan_card("v1")
    check("W05 the card shows the toxic edge, its use and the miner taker share",
          "7GPT simulation_0: toxic x1 widened 1, miner taker share 90%" in card, card[card.find("7GPT"):])


def w06_recorded_mainnet_rounds():
    """On recorded mainnet rounds as the miner delivers them (lazily parsed): the toxic edge reads real taker ids
    and widens some books, nothing fails; with it off, lazy and full parsing still agree."""
    frames = t5._mainnet_frames(70)
    if not frames:
        check("W06 recorded mainnet rounds", True, "SKIPPED: no recording")
        return
    import replay_agent as ra
    from taos.im.protocol import MarketSimulationStateUpdate
    uid = int(next(iter(ra.decode_raw(frames[0]).get("accounts") or {0: None})))
    path = os.path.join(os.path.dirname(HERE), "MyAgent10_23_7Gpt.py")
    common = {"persist_state": 0.0, "event_lookback_minutes": 1.0, "shield_pre_s": 1.0, "shield_post_s": 1.0}
    on = ra.load_agent(path, uid, dict(common, lazy_mode=1.0, toxic_widen=1.0, skew_c1=6.0), m7.Path(tempfile.mkdtemp()))
    lazy = ra.load_agent(path, uid, dict(common, lazy_mode=1.0), m7.Path(tempfile.mkdtemp()))
    full = ra.load_agent(path, uid, dict(common, lazy_mode=0.0), m7.Path(tempfile.mkdtemp()))
    differ, placed = 0, 0
    for frame in frames:
        out = []
        for agent, is_lazy in ((on, True), (lazy, True), (full, False)):
            syn = MarketSimulationStateUpdate(timestamp=frame["timestamp"], version=frame.get("version"),
                                              compression_engine=frame.get("engine") or "lz4")
            syn.compressed = frame["compressed"]
            syn.dendrite.hotkey = frame["validator"]
            syn.decompress(lazy=is_lazy)
            out.append(json.dumps(agent.handle(syn).model_dump(mode="json").get("instructions") or [], sort_keys=True, default=str))
        differ += out[1] != out[2]
        placed += len(json.loads(out[0]))
    failed = sum(v.get("non_finite", 0) for a in (on, lazy, full) for v in a.plan_stats.values())
    widened = sum(v for s in on.plan_stats.values() for k, v in s.items() if k.startswith("toxic_widened"))
    check("W06 recorded mainnet rounds: toxic edge on (lazy) widens books, nothing fails; off, lazy == full",
          differ == 0 and placed > 0 and failed == 0 and widened > 0,
          f"{len(frames)} rounds, lazy/full differ {differ}, {placed} instructions, failed {failed}, widened {widened}")


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
             t6.z01_off_equals_5gpt, t6.z02_window, t6.z03_per_class, t6.z04_limits_hold,
             t6.z05_state_seeded_from_5gpt, t6.z06_card, t6.z07_restored_state_capped, t6.z08_layout_change,
             t6.z09_reseed_from_config,
             w01_off_equals_6gpt, w02_toxic_edge, w03_raw_events, w04_class_skew, w05_card,
             w06_recorded_mainnet_rounds, w07_max_inv_ratio)
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
