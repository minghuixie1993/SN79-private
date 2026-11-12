# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_23_9Gpt (no simulator).

    python agents/Agent_10_23/tests/test_10_23_9Gpt.py

First the whole MyAgent10_23_7Gpt suite against 9Gpt with pending_aware=0 (it must behave as 7Gpt), then the checks
for what 9Gpt adds (Q01-Q05): exactly the real 7Gpt file's orders with pending_aware=0, and also with pending_aware=1
when replies take effect at once; with replies applied one round late it stops re-cancelling and stacking quotes;
lag detection; the card; recorded mainnet rounds run cleanly with it on.
"""
import collections
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
import MyAgent10_23_9Gpt as m9                                   # noqa: E402


class Neutral9(m9.MyAgent10_23_9Gpt):
    """9Gpt with its planner off: the 7Gpt suite runs against it."""

    def initialize(self):
        if getattr(self.config, "pending_aware", None) is None:
            self.config.pending_aware = 0
        super().initialize()


alias = types.ModuleType("MyAgent10_23_7Gpt")
alias.__dict__.update(m9.__dict__)
alias.MyAgent10_23_7Gpt = Neutral9
sys.modules["MyAgent10_23_7Gpt"] = alias
import test_10_23_7Gpt as t7                                     # noqa: E402

t6, t5, t4, t3, base, onegpt, t2, check, make = t7.t6, t7.t5, t7.t4, t7.t3, t7.base, t7.onegpt, t7.t2, t7.check, t7.make
N, SEC, BUY, SELL = base.N, base.SEC, base.BUY, base.SELL
Agent = m9.MyAgent10_23_9Gpt
LIVE7 = dict(shield_pre_s=3, shield_post_s=1, shield_post_s_c1=5, quiet_size_mult_c0=2.5, quiet_size_mult_c1=1.25,
             skew_c1=8, max_inv_ratio_c1=0.06, latency_s=1.05)


def load_real_7gpt():
    import importlib.util
    spec = importlib.util.spec_from_file_location("real_7gpt", os.path.join(os.path.dirname(HERE), "MyAgent10_23_7Gpt.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _market(agents, rounds, seed, lag):
    """Runs `agents` side by side on the same random market. With lag=1 every reply takes effect only after the next
    round's state (the 21:45 mainnet regime): its cancels and placements are applied to the book one round late.
    Returns per agent: the instruction lists, repeat cancels (ids cancelled again while their cancel is in flight)
    and the largest number of own orders resting on one side of one book."""
    rng = random.Random(seed)
    mids = {0: 300.0, 9: 301.0, 100: 29.8, 120: 30.1}
    out = []
    for agent in agents:
        rng.seed(seed)
        m = dict(mids)
        wallets = {b: [50_000 / 2 / x, 25_000.0, 50_000 / 2 / x] for b, x in m.items()}
        resting = {b: [] for b in m}
        in_flight = []
        next_id, calls_all, repeats, worst = [1], [], 0, 0
        seen_cancel = set()
        for r in range(rounds):
            t = (700 + r) * SEC
            books, accounts = {}, {}
            for b in m:
                m[b] *= math.exp(rng.gauss(0, 0.0005))
                bk = base.book(m[b], rng.choice([2, 3, 4]), fundamental=m[b] * (1 + rng.gauss(0, 0.003)),
                               bid_q=rng.uniform(1, 20), ask_q=rng.uniform(1, 20))
                bk.events = []
                books[b] = bk
                accounts[b] = base.account(*wallets[b], resting[b])
            agent.accounts = dict(accounts)
            base.gmod._REQUEST_ACCOUNTS.set(None)
            calls = agent.respond(base.state(books, t)).calls
            calls_all.append(calls)
            for c in calls:
                if c[0] == "cancel":
                    repeats += sum(1 for i in c[2] if i in seen_cancel)
                    seen_cancel.update(c[2])
            # the previous round's reply lands now (lag) or this one does at once
            land = in_flight if lag else [(calls, t)]
            in_flight = [(calls, t)] if lag else []
            for batch, bt_ in land:
                for c in batch:
                    if c[0] == "cancel":
                        resting[c[1]] = [o for o in resting[c[1]] if o.id not in c[2]]
                    else:
                        resting[c[1]].append(base.Order(next_id[0], c[2], c[4], c[3], bt_))
                        next_id[0] += 1
            for b in m:
                for side in (BUY, SELL):
                    worst = max(worst, sum(1 for o in resting[b] if o.side == side))
                for o in list(resting[b]):
                    if rng.random() < 0.05:
                        w = wallets[b]
                        w[0] += o.quantity if o.side == BUY else -o.quantity
                        w[1] -= o.quantity * o.price if o.side == BUY else -o.quantity * o.price
                        resting[b].remove(o)
        out.append((calls_all, repeats, worst))
    return out


def q01_off_equals_7gpt():
    real = load_real_7gpt()
    (a, _, _), (b, _, _) = _market([make(real.MyAgent10_23_7Gpt, persist_state=0, **LIVE7),
                                    make(Agent, persist_state=0, pending_aware=0, **LIVE7)], 300, 41, lag=0)
    differ = sum(x != y for x, y in zip(a, b))
    check("Q01 pending_aware=0: exactly the real 7Gpt's instructions (live settings) over 300 rounds",
          differ == 0 and sum(len(x) for x in a) > 0, f"{differ} rounds differ")


def q02_on_without_lag_equals_7gpt():
    real = load_real_7gpt()
    (a, _, _), (b, _, _) = _market([make(real.MyAgent10_23_7Gpt, persist_state=0, **LIVE7),
                                    make(Agent, persist_state=0, pending_aware=1, **LIVE7)], 300, 43, lag=0)
    differ = sum(x != y for x, y in zip(a, b))
    check("Q02 pending_aware=1, replies applied at once: still exactly 7Gpt's instructions", differ == 0,
          f"{differ} rounds differ")


def q03_lagged_regime():
    real = load_real_7gpt()
    seven = make(real.MyAgent10_23_7Gpt, persist_state=0, **LIVE7)
    nine = make(Agent, persist_state=0, pending_aware=1, **LIVE7)
    (a, rep7, worst7), (b, rep9, worst9) = _market([seven, nine], 300, 47, lag=1)
    n7, n9 = sum(len(x) for x in a), sum(len(x) for x in b)
    check("Q03 replies applied one round late: 9Gpt detects it, stops re-cancelling and stacking quotes",
          nine.lagged("v1") and rep9 < rep7 / 5 and worst9 <= 2 and worst9 <= worst7 and n9 < 0.6 * n7,
          f"instructions 7Gpt {n7} / 9Gpt {n9}, repeat cancels {rep7} / {rep9}, max own orders on a side {worst7} / {worst9}, "
          f"lagged {nine.lagged('v1')}")


def q04_card():
    nine = make(Agent, persist_state=0, pending_aware=1, **LIVE7)
    _market([nine], 60, 53, lag=1)
    card = nine._plan_card("v1")
    tail = card[card.index("9GPT"):] if "9GPT" in card else card[-200:]
    check("Q04 the card shows the lag regime, hidden cancels, skipped placements and instructions per reply",
          "9GPT pending-aware 1" in card and "lagged" in tail and "instructions/reply p50" in tail, tail)


def q05_recorded_mainnet_rounds():
    """On recorded mainnet rounds with the live params: no failed book, lazy == full parsing."""
    files = sorted(glob.glob("/home/Trustvs/workspace/sn-79/agents/data/mainnet_records/*/*.frames.zst"))
    if not files:
        check("Q05 recorded mainnet rounds", True, "SKIPPED: no recording")
        return
    sys.path.insert(0, "/home/Trustvs/workspace/sn-79/agents/mainnet")
    from state_tap import read_frames
    import replay_agent as ra
    from taos.im.protocol import MarketSimulationStateUpdate
    frames = [f for p in files[-1:] for f in read_frames(p)][-60:]
    uid = int(next(iter(ra.decode_raw(frames[0]).get("accounts") or {0: None})))
    path = os.path.join(os.path.dirname(HERE), "MyAgent10_23_9Gpt.py")
    params = dict({k: float(v) for k, v in LIVE7.items()}, persist_state=0.0, event_lookback_minutes=1.0)
    lazy = ra.load_agent(path, uid, dict(params, lazy_mode=1.0), m9.Path(tempfile.mkdtemp()))
    full = ra.load_agent(path, uid, dict(params, lazy_mode=0.0), m9.Path(tempfile.mkdtemp()))
    differ, placed = 0, 0
    for frame in frames:
        out = []
        for agent, is_lazy in ((lazy, True), (full, False)):
            syn = MarketSimulationStateUpdate(timestamp=frame["timestamp"], version=frame.get("version"),
                                              compression_engine=frame.get("engine") or "lz4")
            syn.compressed = frame["compressed"]
            syn.dendrite.hotkey = frame["validator"]
            syn.decompress(lazy=is_lazy)
            out.append(json.dumps(agent.handle(syn).model_dump(mode="json").get("instructions") or [], sort_keys=True,
                                  default=str))
        differ += out[0] != out[1]
        placed += len(json.loads(out[0]))
    failed = sum(v.get("non_finite", 0) for v in lazy.plan_stats.values())
    check("Q05 recorded mainnet rounds (live params, pending-aware on): lazy == full, no failed book",
          differ == 0 and placed > 0 and failed == 0, f"{len(frames)} rounds, {differ} differ, {placed} instructions")


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
             q01_off_equals_7gpt, q02_on_without_lag_equals_7gpt, q03_lagged_regime, q04_card,
             q05_recorded_mainnet_rounds)
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
