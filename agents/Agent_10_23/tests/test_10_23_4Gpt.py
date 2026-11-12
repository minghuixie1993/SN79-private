# SPDX-License-Identifier: MIT
"""
Offline tests for MyAgent10_23_4Gpt (no simulator).

    python agents/Agent_10_23/tests/test_10_23_4Gpt.py

First the whole MyAgent10_23_3Gpt suite (minus its lazy_load checks, which 4Gpt changes on purpose) against
4Gpt with its own changes switched off: it must still send exactly MyAgent10_23_2Gpt's instructions (P01) and
MyAgentV3's (T00). Then the checks for what 4Gpt adds (X01-X09), including lazy parsing on recorded validator
rounds when a recording is available.
"""
import glob
import json
import math
import os
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import MyAgent10_23_4Gpt as m4                                   # noqa: E402

# 4Gpt with its own changes off: V3's fundamental coefficients, 2Gpt's lean cap, no latency guard, no saved state.
NEUTRAL4 = dict(coef_fund_c0=0.04, coef_fund_c1=0.0035, coef_fund_c2=0.0035, max_lean=1.0, latency_s=0.0,
                persist_state=0)


class Neutral4(m4.MyAgent10_23_4Gpt):
    def initialize(self):
        for name, value in NEUTRAL4.items():
            if getattr(self.config, name, None) is None:
                setattr(self.config, name, value)
        super().initialize()


# The 3Gpt suite imports MyAgent10_23_3Gpt; point it at the neutral 4Gpt.
alias = types.ModuleType("MyAgent10_23_3Gpt")
alias.__dict__.update(m4.__dict__)
alias.MyAgent10_23_3Gpt = Neutral4
sys.modules["MyAgent10_23_3Gpt"] = alias
import test_10_23_3Gpt as t3                                      # noqa: E402

base, onegpt, t2, check, make = t3.base, t3.onegpt, t3.t2, t3.check, t3.make
N, SEC, BUY, SELL = base.N, base.SEC, base.BUY, base.SELL
Agent = m4.MyAgent10_23_4Gpt
REC = "/tmp/claude-1001/-home-Trustvs-workspace-sn-79/2c31a891-6e3d-4b06-a893-c33c892134e5/scratchpad/replaydata/rec"


def defaults(**extra):
    return make(Agent, persist_state=0, **extra)


def x01_class_defaults():
    g = defaults()
    got = {k: g._params(b)["coef_fund"] for k, b in ((0, 0), (1, 100))}
    lean = {k: g._params2(b)["max_lean"] for k, b in ((0, 0), (1, 100))}
    over = defaults(coef_fund_c0=0.02, max_lean_c1=0.8)
    plain = defaults(coef_fund=0.03)
    check("X01 class defaults: coef_fund 0.009 / 0.006, max_lean 0.5; --params still override them",
          got == {0: 0.009, 1: 0.006} and lean == {0: 0.5, 1: 0.5} and over._params(0)["coef_fund"] == 0.02
          and over._params2(100)["max_lean"] == 0.8 and plain._params(0)["coef_fund"] == 0.03
          and plain._params(100)["coef_fund"] == 0.006, f"{got} {lean}")


def x02_simulation_2():
    classes3 = base.CLASSES + [N(name="simulation_2", books=list(range(128, 160)))]
    g = defaults()
    g.asset_classes = lambda: classes3
    bk_gap = base.book(30.0, 4, fundamental=30.0 * 1.06)       # +600 bps
    bk_none = base.book(30.0, 4, fundamental=None)
    a = base.places(base.quote_once(g, bk_gap, base.account(833.3, 25_000.0, 833.3), book_id=140))
    g2 = defaults()
    g2.asset_classes = lambda: classes3
    b = base.places(base.quote_once(g2, bk_none, base.account(833.3, 25_000.0, 833.3), book_id=140))
    check("X02 simulation_2: a +600 bps fundamental gap does not move the quotes (coef_fund 0)",
          a and a == b and g._params(140)["coef_fund"] == 0.0, f"with gap {a} / without {b}")


def x03_latency_guard():
    """The guard formula, where it binds: a narrow half spread (half_spread 1 bp, vol_mult 0.1) and a strong upward
    lean, sigma 10 bps over 20 s, latency 0.72 s -> the bid keeps ~5.7 ticks under the ask instead of 3. With V3's
    default spread (>= 1.3 sigma over 20 s, lean + skew <= 0.8 of it) a quote sits >= ~0.26 sigma from the mid, more
    than the ~0.19 sigma the touch moves in 0.72 s, so on the defaults the guard almost never binds."""
    narrow = dict(half_spread=0.0001, vol_mult=0.1, min_half_ticks=1, max_lean=1.0, lean=20, max_pred_bps=50)

    def bid_for(latency):
        g = defaults(latency_s=latency, **narrow)
        bk = base.book(300.0, 2, fundamental=330.0)
        g.accounts = {0: base.account(83.33, 25_000.0, 83.33)}
        base.gmod._REQUEST_ACCOUNTS.set(None)
        memory = g._remember("v1", 0, bk, 1000 * SEC)
        memory["variance"] = (10e-4) ** 2 / 20
        resp = base.Resp()
        g._quote_book(resp, "v1", 0, bk, memory)
        bids = [c for c in base.places(resp.calls) if c[2] == 0]
        return (bids[0][4] if bids else None), bk.asks[0].price
    slow, ask = bid_for(0.72)
    fast, _ = bid_for(0.0)
    need = 10e-4 * math.sqrt(0.72 / 20) * 300 / 0.01                  # 5.69 ticks
    check("X03 latency guard where it binds: bid ~5.7 ticks under the ask at 0.72 s, 3 ticks without latency",
          slow is not None and fast is not None and abs((ask - slow) / 0.01 - math.ceil(need)) <= 1
          and abs((ask - fast) / 0.01 - 3) < 1e-6, f"ask {ask}: bid {slow} at 0.72 s, {fast} at 0 s; need {need:.2f} ticks")


def x04_persist_roundtrip():
    tmp = tempfile.mkdtemp(prefix="g4state_")
    g = make(Agent, persist_state=1, persist_every_s=5)
    g._state_file = m4.Path(tmp) / "state.json"
    base.quote_once(g, base.book(300.0, 3), base.account(83.33, 25_000.0, 83.33), t=1000 * SEC)
    g.spread_mult[("v1", 0)] = 1.36
    g.markout_stats.setdefault(("v1", 0), {"realized": 0, "captured": 0, "notional": 0, "n": 0})["average"] = -1.2
    g.memory[("v1", 0)]["book_mult"] = 1.5
    g.memory[("v1", 0)]["book_markout"] = -3.0
    g._save_state("v1", "sim-A", 1000 * SEC)

    def fresh(sim_id, t):
        h = make(Agent, persist_state=1)
        h._state_file = g._state_file
        h.accounts = {0: base.account(83.33, 25_000.0, 83.33)}
        base.gmod._REQUEST_ACCOUNTS.set(None)
        h.respond(N(books={0: base.book(300.0, 3)}, timestamp=t, dendrite=N(hotkey="v1"),
                    config=N(simulation_id=sim_id)))
        return h
    same = fresh("sim-A", 1060 * SEC)
    other = fresh("sim-B", 1060 * SEC)
    older = fresh("sim-A", 900 * SEC)
    ok = (same.spread_mult.get(("v1", 0)) == 1.36 and same.memory[("v1", 0)].get("book_mult") == 1.5
          and same.markout_stats[("v1", 0)]["average"] == -1.2
          and other.spread_mult.get(("v1", 0)) is None and older.spread_mult.get(("v1", 0)) is None)
    check("X04 learned state saved and restored for the same simulation only (not another, not an earlier time)",
          ok, f"same {same.spread_mult} book {same.memory[('v1', 0)].get('book_mult')}; other {other.spread_mult}; "
              f"older {older.spread_mult}")


def x05_timing_and_delay():
    g = defaults(added_delay_s=0.2)
    st = N(books={0: base.book(300.0, 3)}, timestamp=1000 * SEC, dendrite=N(hotkey="v1"), accounts={1: {}})
    # The framework's handle (update + respond + response checks) stubbed to its two calls that matter here.
    original = m4.FinanceAgent.handle

    def stub_handle(self, s):
        self.accounts = {0: base.account(83.33, 25_000.0, 83.33)}
        return self.respond(s)
    m4.FinanceAgent.handle = stub_handle
    try:
        t = time.perf_counter()
        g.handle(st)
        took = time.perf_counter() - t
    finally:
        m4.FinanceAgent.handle = original
    card = g._plan_card("v1")
    check("X05 added_delay_s sleeps after replying and counts toward the latency; reply time on the card",
          took >= 0.2 and abs(g.latency_s - 0.25) < 1e-9 and "4GPT reply p50" in card, card[card.find("4GPT"):])


def x06_lazy_mode_float():
    on = defaults(lazy_mode=1.0)
    off = defaults(lazy_mode=0.0)
    auto = defaults()
    check("X06 lazy_mode given as a float by the miner (1.0 / 0.0) is read correctly; auto turns lazy on",
          on.config.lazy_load is True and off.config.lazy_load is False and auto.config.lazy_load is True,
          f"{on.config.lazy_load} {off.config.lazy_load} {auto.config.lazy_load}")


def _frames(limit):
    sys.path.insert(0, "/home/Trustvs/workspace/sn-79/agents/mainnet")
    from state_tap import read_frames
    files = sorted(glob.glob(f"{REC}/*/*.frames.zst"))
    return [f for path in files for f in read_frames(path)][:limit]


def x07_lazy_equals_full():
    """On recorded validator rounds, delivered as the miner delivers them: a lazily and a fully parsed 4Gpt send
    exactly the same instructions every round; 3Gpt fails every book when forced lazy."""
    frames = _frames(60)
    if not frames:
        check("X07 lazy == full on recorded rounds", True, "SKIPPED: no recording at " + REC)
        return
    import replay_agent as ra
    from taos.im.protocol import MarketSimulationStateUpdate
    uid = int(next(iter(ra.decode_raw(frames[0]).get("accounts") or {0: None})))
    path = os.path.join(os.path.dirname(HERE), "MyAgent10_23_4Gpt.py")
    lazy = ra.load_agent(path, uid, {"lazy_mode": 1.0, "persist_state": 0.0, "event_lookback_minutes": 1.0},
                         m4.Path(tempfile.mkdtemp()))
    full = ra.load_agent(path, uid, {"lazy_mode": 0.0, "persist_state": 0.0, "event_lookback_minutes": 1.0},
                         m4.Path(tempfile.mkdtemp()))
    differ, placed = 0, 0
    for frame in frames:
        out = []
        for agent, is_lazy in ((lazy, True), (full, False)):
            syn = MarketSimulationStateUpdate(timestamp=frame["timestamp"], version=frame.get("version"),
                                              compression_engine=frame.get("engine") or "lz4")
            syn.compressed = frame["compressed"]
            syn.dendrite.hotkey = frame["validator"]
            syn.decompress(lazy=is_lazy)
            resp = agent.handle(syn)
            out.append(json.dumps(resp.model_dump(mode="json").get("instructions") or [], sort_keys=True, default=str))
        differ += out[0] != out[1]
        placed += len(json.loads(out[0]))
    failed = sum(v.get("non_finite", 0) for v in lazy.plan_stats.values())
    check("X07 recorded rounds: lazily and fully parsed 4Gpt send identical instructions",
          differ == 0 and placed > 0 and lazy.config.lazy_load and not full.config.lazy_load,
          f"{len(frames)} rounds, {differ} differ, {placed} instructions, lazy non-finite {failed}")


def x08_default_limits_hold():
    import random
    rng = random.Random(41)
    g = defaults(latency_s=0.72)
    worst_cap, worst_band, crossed = 0.0, -1.0, 0
    for r in range(300):
        mid = 300 * math.exp(rng.gauss(0, 0.01))
        wealth = 50_000.0
        gap = rng.uniform(-0.3, 0.3)
        initial = 0.3 * wealth / mid
        units = initial + gap * wealth / mid
        bk = base.book(mid, rng.choice([1, 2, 3, 4]), fundamental=mid * (1 + rng.gauss(0, 0.01)))
        calls = base.quote_once(g, bk, base.account(units, wealth - units * mid, initial), t=(1000 + r) * SEC)
        book_mid = (bk.bids[0].price + bk.asks[0].price) / 2
        w = units * book_mid + (wealth - units * mid)
        agent_gap = (units - initial) * book_mid / w
        for c in base.places(calls):
            worst_cap = max(worst_cap, c[3] * c[4] / (g.max_order_fraction * w))
            worst_band = max(worst_band, (agent_gap if c[2] == 0 else -agent_gap) + c[3] * c[4] / w)
            crossed += (c[2] == 0 and c[4] >= bk.asks[0].price) or (c[2] == 1 and c[4] <= bk.bids[0].price)
    check("X08 4Gpt defaults, 300 random states: cap, post-fill band hold and no quote crosses the touch",
          worst_cap <= 1 + 1e-9 and worst_band <= 0.25 + 1e-9 and crossed == 0,
          f"cap x{worst_cap:.4f}, gap after fill {worst_band:.4f}, crossed {crossed}")


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
             x01_class_defaults, x02_simulation_2, x03_latency_guard, x04_persist_roundtrip, x05_timing_and_delay,
             x06_lazy_mode_float, x07_lazy_equals_full, x08_default_limits_hold)
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
