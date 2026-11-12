# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_23_8Gpt (no simulator).

    python agents/Agent_10_23/tests/test_10_23_8Gpt.py

First the whole MyAgent10_23_7Gpt suite against 8Gpt with its momentum input off, then the checks for what 8Gpt adds
(M01-M05): exactly the real 7Gpt file's orders when off, the EMA tracking the mid with its time constant, a rise above
the EMA leaning the quotes up (per class), the prediction still held within max_pred_bps, and recorded mainnet rounds.
"""
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
import MyAgent10_23_8Gpt as m8                                   # noqa: E402


class Neutral8(m8.MyAgent10_23_8Gpt):
    """8Gpt with coef_dev 0 (its default): the 7Gpt suite runs against it."""


alias = types.ModuleType("MyAgent10_23_7Gpt")
alias.__dict__.update(m8.__dict__)
alias.MyAgent10_23_7Gpt = Neutral8
sys.modules["MyAgent10_23_7Gpt"] = alias
import test_10_23_7Gpt as t7                                     # noqa: E402

t6, t5, t4, t3, base, onegpt, t2, check, make = (t7.t6, t7.t5, t7.t4, t7.t3, t7.base, t7.onegpt, t7.t2, t7.check,
                                                 t7.make)
N, SEC, BUY, SELL = base.N, base.SEC, base.BUY, base.SELL
Agent = m8.MyAgent10_23_8Gpt
LIVE7 = dict(shield_pre_s=1, shield_post_s=1, shield_post_s_c1=5, quiet_size_mult_c0=2, quiet_size_mult_c1=1.25,
             skew_c1=8, max_inv_ratio_c1=0.06)


def defaults(**extra):
    return make(Agent, persist_state=0, **extra)


def load_real_7gpt():
    import importlib.util
    spec = importlib.util.spec_from_file_location("real_7gpt", os.path.join(os.path.dirname(HERE), "MyAgent10_23_7Gpt.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def m01_off_equals_7gpt():
    real = load_real_7gpt()
    rng = random.Random(41)
    seven, eight = make(real.MyAgent10_23_7Gpt, persist_state=0, **LIVE7), make(Agent, persist_state=0, **LIVE7)
    mids = {0: 300.0, 9: 301.0, 100: 29.8, 120: 30.1}
    wallets = {b: [50_000 / 2 / m, 25_000.0, 50_000 / 2 / m] for b, m in mids.items()}
    resting = {b: [] for b in mids}
    next_id, mismatches, placed = [1], 0, 0
    for r in range(400):
        t = (700 + r) * SEC
        books, accounts = {}, {}
        for b in mids:
            mids[b] *= math.exp(rng.gauss(0, 0.0008))
            books[b] = base.book(mids[b], rng.choice([2, 3, 4]), fundamental=mids[b] * (1 + rng.gauss(0, 0.003)),
                                 bid_q=rng.uniform(1, 20), ask_q=rng.uniform(1, 20))
            accounts[b] = base.account(*wallets[b], resting[b])
        calls = []
        for agent in (seven, eight):
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
                    seven.onTrade(ev, "v1"); eight.onTrade(ev, "v1")
    check("M01 coef_dev 0: 8Gpt sends exactly the real 7Gpt's instructions (live settings) over 400 rounds",
          mismatches == 0 and placed > 0, f"{mismatches} rounds differ, {placed} placements")


def m02_ema_tracks():
    g = defaults(coef_dev=0.3)
    acct = base.account(83.33, 25_000.0, 83.33)
    base.quote_once(g, base.book(300.0, 3), acct, t=1206 * SEC)
    for s in range(1, 21):                                  # the mid jumps to 303 and stays for 20 s
        base.quote_once(g, base.book(303.0, 3), acct, t=(1206 + s) * SEC)
    m = g.memory[("v1", 0)]
    mid0 = (base.book(300.0, 3).bids[0].price + base.book(300.0, 3).asks[0].price) / 2
    mid1 = m["mid"]
    expected = mid1 - (mid1 - mid0) * math.exp(-20 / 20.0)  # one time constant later: 63% of the way
    check("M02 the EMA follows the mid with its time constant (63% of a step after dev_tau_s)",
          abs(m["ema"] - expected) < 0.02, f"ema {m['ema']:.4f}, expected {expected:.4f}")


def m03_lean_up():
    def quotes(**p):
        g = defaults(**p)
        acct = base.account(83.33, 25_000.0, 83.33)
        base.quote_once(g, base.book(300.0, 3), acct, t=1206 * SEC)
        calls = base.quote_once(g, base.book(301.5, 3), acct, t=1210 * SEC)   # +50 bps above the EMA
        return {c[2]: c[4] for c in base.places(calls)}, g.memory[("v1", 0)]["pred"]
    off, pred_off = quotes()
    on, pred_on = quotes(coef_dev=0.3)
    c1_only, pred_c1 = quotes(coef_dev_c1=0.3)
    ok = on[0] > off[0] and on[1] > off[1] and pred_on > pred_off and c1_only == off and pred_c1 == pred_off
    check("M03 a mid above its EMA leans both quotes up with coef_dev; coef_dev_c1 leaves class 0 alone",
          ok, f"off {off} pred {pred_off:+.2f}; on {on} pred {pred_on:+.2f}; c1 only {c1_only}")


def m04_capped():
    g = defaults(coef_dev=100.0)
    acct = base.account(83.33, 25_000.0, 83.33)
    base.quote_once(g, base.book(300.0, 3), acct, t=1206 * SEC)
    base.quote_once(g, base.book(330.0, 3), acct, t=1210 * SEC)
    pred = g.memory[("v1", 0)]["pred"]
    check("M04 a huge deviation x coef_dev still holds the prediction within max_pred_bps",
          abs(pred) <= g.max_pred_bps + 1e-9, f"pred {pred}, max {g.max_pred_bps}")


def m05_recorded_rounds():
    frames = t5._mainnet_frames(60)
    if not frames:
        check("M05 recorded mainnet rounds", True, "SKIPPED: no recording")
        return
    import replay_agent as ra
    from taos.im.protocol import MarketSimulationStateUpdate
    uid = int(next(iter(ra.decode_raw(frames[0]).get("accounts") or {0: None})))
    path = os.path.join(os.path.dirname(HERE), "MyAgent10_23_8Gpt.py")
    agent = ra.load_agent(path, uid, {"persist_state": 0.0, "event_lookback_minutes": 1.0, "coef_dev": 0.27,
                                      "coef_dev_c1": 0.13, **{k: float(v) for k, v in LIVE7.items()}},
                          m8.Path(tempfile.mkdtemp()))
    placed = 0
    for frame in frames:
        syn = MarketSimulationStateUpdate(timestamp=frame["timestamp"], version=frame.get("version"),
                                          compression_engine=frame.get("engine") or "lz4")
        syn.compressed = frame["compressed"]
        syn.dendrite.hotkey = frame["validator"]
        syn.decompress(lazy=True)
        placed += len(agent.handle(syn).model_dump(mode="json").get("instructions") or [])
    failed = sum(v.get("non_finite", 0) for v in agent.plan_stats.values())
    emas = sum(1 for m in agent.memory.values() if m.get("ema"))
    check("M05 recorded mainnet rounds with coef_dev on: every book keeps an EMA, no failed book",
          placed > 0 and failed == 0 and emas >= 100, f"{len(frames)} rounds, {placed} instructions, failed {failed}, EMAs {emas}")


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
             t7.w01_off_equals_6gpt, t7.w02_toxic_edge, t7.w03_raw_events, t7.w04_class_skew, t7.w05_card,
             t7.w06_recorded_mainnet_rounds, t7.w07_max_inv_ratio,
             m01_off_equals_7gpt, m02_ema_tracks, m03_lean_up, m04_capped, m05_recorded_rounds)
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
