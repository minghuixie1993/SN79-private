# SPDX-License-Identifier: MIT
"""
Offline parity tests for MyAgent10_26_Gpt, the flat rewrite of MyAgent10_23_9Gpt (no simulator).

    python agents/Agent_10_23/tests/test_10_26_Gpt.py

Both agents run side by side on the same random three-class market, see the same fills (own maker fills fed back as
trade notices) and must send exactly the same instructions, unrounded, every round: with the live parameter set,
with replies applied one round late, with fee spikes and pacing pauses, with pending_aware=0, with every default, and
across a new simulation (the clock going back). Then the learned-state file: one agent's file restores in the other.
"""
import importlib.util
import math
import os
import random
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_gpt_10_23 as base                                    # noqa: E402  (stubs: account, book, state, Order)

N, SEC, BUY, SELL = base.N, base.SEC, base.BUY, base.SELL
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(os.path.dirname(HERE), f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m9, m26 = load("MyAgent10_23_9Gpt"), load("MyAgent10_26_Gpt")
CLASSES = [N(name="simulation_0", books=list(range(64))), N(name="simulation_1", books=list(range(64, 96))),
           N(name="simulation_2", books=list(range(96, 128)))]
LIVE = dict(event_lookback_minutes=1, latency_s=1.05, shield_pre_s=3, shield_post_s=1, shield_post_s_c1=5,
            shield_post_s_c2=5, quiet_size_mult_c0=2.5, quiet_size_mult_c1=1.25, quiet_size_mult_c2=1.6, skew_c1=8,
            max_inv_ratio_c1=0.06, half_spread_c0=0.0004, size_fraction_c0=0.024, size_fraction_c1=0.024,
            size_fraction_c2=0.024, max_order_fraction=0.06, coef_fund_c2=0.004, pace_burst=8, adapt_max=1.0,
            max_lean=0.75, pending_aware=1, fill_skew_c0=0.6, fill_skew_c2=0.6)


class Resp:
    """Records every instruction exactly as sent."""
    def __init__(self):
        self.calls = []

    def limit_order(self, **k):
        self.calls.append(("place", k["book_id"], int(k["direction"]), k["quantity"], k["price"], int(k["stp"]),
                           k["postOnly"], int(k["timeInForce"]), k["expiryPeriod"]))

    def cancel_orders(self, book_id, ids):
        self.calls.append(("cancel", book_id, tuple(ids)))


def make(cls, params):
    agent = object.__new__(cls)
    agent.config = N(**{k: float(v) for k, v in params.items()})
    agent.output_dir = tempfile.mkdtemp()
    agent.initialize()
    agent.uid = 1
    agent.asset_classes = lambda: CLASSES
    agent.price_decimals = lambda b: 2
    agent.volume_decimals = lambda b: 4
    agent.min_order_size = lambda b: 2.5364 if b >= 96 else 0.0
    agent.config_for = lambda b: N(miner_wealth=50_000.0)
    agent.make_response = lambda: Resp()
    agent.accounts = {}
    return agent


def run(params, rounds=600, seed=7, lag=0, fee_spikes=False, reset_at=None):
    """Runs 9Gpt and 10_26_Gpt on one market; returns (rounds that differ, instructions, first difference)."""
    rng = random.Random(seed)
    agents = [make(m9.MyAgent10_23_9Gpt, params), make(m26.MyAgent10_26_Gpt, params)]
    mids = {0: 300.0, 9: 301.0, 70: 120.0, 80: 121.0, 100: 29.8, 120: 30.1}

    def fresh_wallets():
        # [base, quote, initial base]; book 80 is short of QUOTE and book 120 of BASE, so the funds limits bind
        w = {b: [50_000 / 2 / m, 25_000.0, 50_000 / 2 / m] for b, m in mids.items()}
        w[80][1], w[120][0] = 400.0, 20.0
        return w
    wallets = fresh_wallets()
    resting = {b: [] for b in mids}
    in_flight, next_id, differ, sent, first = [], [1], 0, 0, None
    clock = 700
    for r in range(rounds):
        if reset_at is not None and r == reset_at:
            clock = 100                                   # a new simulation: the clock goes back
            resting = {b: [] for b in mids}
            wallets = fresh_wallets()
            in_flight = []
        t = (clock + r) * SEC
        books, accounts = {}, {}
        for b in mids:
            if rng.random() < 0.01:
                # an inventory shock the agent hears nothing about (as a transfer would be): resting quotes on the
                # side that now adds to the gap become oversize, or the side crosses the band
                w = wallets[b]
                move = rng.uniform(-0.15, 0.15) * (w[0] + w[1] / mids[b])
                if w[0] + move > 0 and w[1] - move * mids[b] > 0:
                    w[0] += move
                    w[1] -= move * mids[b]
            # a rough market: jumps, a fundamental up to a few % off, lopsided books and bursts of taker flow, so the
            # lean cap, the skews, the guards and the size limits all bind some of the time
            mids[b] *= math.exp(rng.gauss(0, 0.0003 if rng.random() < 0.95 else 0.003))
            bk = base.book(mids[b], rng.choice([2, 3, 4, 6]), fundamental=mids[b] * (1 + rng.gauss(0, 0.02)),
                           bid_q=rng.uniform(0.5, 60), ask_q=rng.uniform(0.5, 60))
            bk.events = [N(type="t", side=rng.choice([0, 1]), quantity=rng.uniform(0.1, 30)) for _ in range(rng.randint(0, 5))]
            books[b] = bk
            fee = 0.03 if fee_spikes and rng.random() < 0.05 else rng.choice([0.0, 0.001])
            accounts[b] = base.account(*wallets[b], list(resting[b]), fee=fee, traded=0.0)
        calls = []
        for agent in agents:
            agent.accounts = dict(accounts)
            calls.append(agent.respond(base.state(books, t)).calls)
        sent += len(calls[0])
        if calls[0] != calls[1]:
            differ += 1
            if first is None:
                first = (r, calls[0][:6], calls[1][:6])
        land = in_flight if lag else [calls[0]]
        in_flight = [calls[0]] if lag else []
        for batch in land:
            for c in batch:
                if c[0] == "cancel":
                    resting[c[1]] = [o for o in resting[c[1]] if o.id not in c[2]]
                else:
                    resting[c[1]].append(base.Order(next_id[0], c[2], c[4], c[3], t))
                    next_id[0] += 1
        # fills: each resting order may trade; both agents get the same notice
        for b in mids:
            for o in list(resting[b]):
                if rng.random() < 0.03:
                    w = wallets[b]
                    q = o.quantity if rng.random() < 0.7 else round(o.quantity / 2, 4)
                    w[0] += q if o.side == BUY else -q
                    w[1] -= q * o.price if o.side == BUY else -q * o.price
                    ev = N(makerAgentId=1, takerAgentId=rng.choice([-1, 5, 7]), bookId=b, price=o.price,
                           quantity=q, side=SELL if o.side == BUY else BUY, timestamp=t + 300_000_000,
                           makerFee=0.0, makerOrderId=o.id)
                    for agent in agents:
                        agent.onTrade(ev, "v1")
                    if q >= o.quantity:
                        resting[b].remove(o)
                    else:
                        o.quantity = round(o.quantity - q, 4)
    return differ, sent, first


def parity(name, params, **kw):
    differ, sent, first = run(params, **kw)
    check(name, differ == 0 and sent > 0, f"{differ} rounds differ of {kw.get('rounds', 600)}, {sent} instructions"
          + (f"; first at round {first[0]}: 9Gpt {first[1]} / 10_26 {first[2]}" if first else ""))


def state_carries_over():
    """9Gpt's learned-state file restores into 10_26_Gpt (and back) with the same multipliers."""
    out = []
    for writer, reader in ((m9.MyAgent10_23_9Gpt, m26.MyAgent10_26_Gpt), (m26.MyAgent10_26_Gpt, m9.MyAgent10_23_9Gpt)):
        a = make(writer, dict(LIVE, persist_state=1))
        a.spread_mult[("v1", 0)] = 0.93
        a.markout_stats[("v1", 0)] = {"realized": 0.0, "captured": 0.0, "notional": 0.0, "n": 0, "average": 1.7}
        a.memory[("v1", 9)] = {"book_mult": 1.11, "book_markout": 0.4}
        a._save_state("v1", "sim", 1000 * SEC)
        b = make(reader, dict(LIVE, persist_state=1))
        b._state_file = a._state_file
        b._load_state("v1", "sim", 1100 * SEC)
        out.append((b.spread_mult.get(("v1", 0)), b.markout_stats.get(("v1", 0), {}).get("average"),
                    b._restore.get("v1", {}).get(9)))
    check("learned state file: 9Gpt <-> 10_26_Gpt restore the same values", out[0] == out[1] and out[0][0] == 0.93,
          str(out))


def recorded_mainnet_rounds(limit=900):
    """The last `limit` recorded mainnet rounds, decompressed lazily as under the real miner (the live miner's own
    account, orders and fill notices included): both agents, built as the miner builds them, send the same
    instructions every round."""
    import glob
    import json
    files = sorted(glob.glob("/home/Trustvs/workspace/sn-79/agents/data/mainnet_records/*/*.frames.zst"))
    if not files:
        check("recorded mainnet rounds", True, "SKIPPED: no recording")
        return
    sys.path.insert(0, "/home/Trustvs/workspace/sn-79/agents/mainnet")
    from pathlib import Path
    from state_tap import read_frames
    import replay_agent as ra
    from taos.im.protocol import MarketSimulationStateUpdate
    frames = [f for p in files[-2:] for f in read_frames(p)][-limit:]
    uid = int(next(iter(ra.decode_raw(frames[-1]).get("accounts") or {0: None})))
    params = dict({k: float(v) for k, v in LIVE.items()}, persist_state=0.0)
    folder = os.path.dirname(HERE)
    agents = [ra.load_agent(os.path.join(folder, f"{name}.py"), uid, dict(params), Path(tempfile.mkdtemp()))
              for name in ("MyAgent10_23_9Gpt", "MyAgent10_26_Gpt")]
    differ, sent, first = 0, 0, None
    for n, frame in enumerate(frames):
        out = []
        for agent in agents:
            syn = MarketSimulationStateUpdate(timestamp=frame["timestamp"], version=frame.get("version"),
                                              compression_engine=frame.get("engine") or "lz4")
            syn.compressed = frame["compressed"]
            syn.dendrite.hotkey = frame["validator"]
            syn.decompress(lazy=bool(agent.config.lazy_load))
            out.append(json.dumps(agent.handle(syn).model_dump(mode="json").get("instructions") or [], sort_keys=True,
                                  default=str))
        if out[0] != out[1]:
            differ += 1
            first = first if first is not None else n
        sent += len(json.loads(out[0]))
    check(f"recorded mainnet rounds (uid {uid}, live params, lazy books): same instructions", differ == 0 and sent > 0,
          f"{len(frames)} rounds, {differ} differ (first {first}), {sent} instructions")


if __name__ == "__main__":
    if "--recorded" in sys.argv:
        recorded_mainnet_rounds()
        sys.exit(0 if all(ok for _, ok in RESULTS) else 1)
    parity("live set, replies applied at once", LIVE, seed=7)
    parity("live set, replies applied one round late (lagged regime)", LIVE, seed=11, lag=1)
    parity("live set, fee spikes above max_fee_rate", LIVE, seed=13, fee_spikes=True)
    parity("live set, pace_burst 1 (pacing pauses)", dict(LIVE, pace_burst=1), seed=17)
    parity("live set with pending_aware=0", dict(LIVE, pending_aware=0), seed=19, lag=1)
    parity("every default (no params)", {}, seed=23)
    parity("defaults + plain coef_fund / max_inv_ratio / skew / inventory_band", dict(coef_fund=0.02, max_inv_ratio=0.1,
           skew=6, inventory_band=0.2, expiry_s=90, cross_guard_ticks=2), seed=29)
    parity("live set across a new simulation (clock goes back)", LIVE, seed=31, reset_at=300)
    state_carries_over()
    failed = sum(not ok for _, ok in RESULTS)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
