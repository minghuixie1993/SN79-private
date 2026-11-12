# SPDX-License-Identifier: MIT
"""
MyAgent10_23_9Gpt with each quote's size set by what fills in its situation have been worth: a fractional-Kelly
multiplier on a decayed Bayesian estimate of the clamped realized markout (A), and an inventory term that makes
the side that reduces the gap larger (E).

Why (24 October, mainnet recording of run 20261024_0310, agents/data/replays/shield_grid_log.md 17:50 and 18:05):
  - the making pool pays each uid's share of realized spread, captured x clamp(R / C, 0, 1) over all its fills,
    which is the sum of the fills' realized spread while R < C (reward.making_pool_inputs, debeta.markout_quality):
    a fill near 0 bps costs nothing, a negative one costs, a strongly positive one is worth more size;
  - our own 1827 fills differed by situation: class 1 P(bad) 0.20 at +2.03 bps; class 0 5-28 s after a reset
    +1.63 bps but 0-5 s +0.08 bps; class 0 fills that traded with the book's recent taker flow +0.57 bps against
    +1.57 bps for fills against it; class 2 0-5 s after a reset -0.55 bps;
  - across the market, quotes filled by other miners lost (P(bad) 0.92 class 0, 0.72-0.84 class 1) and those
    filled by the background mostly held, and a book's miner share over the last 30 rounds predicts the next
    taker (class 0: below 0.2 -> 20% miners, 0.8 and above -> 72%).

A situation is (class, reset phase: within `post_window_s` of the reseed or not, the book's recent taker flow with
or against the side, the book's miner share of taker notional over `miner_window_s` at or above `miner_line`).
Every maker fill is marked `markout_horizon` later (as 4Gpt marks them) and its realized bps, clamped to +/- its
captured bps as the validator clamps it, goes into its situation's estimate: decayed sums of weight, x and x^2
with half-life `edge_halflife_s` (sim seconds), on top of a prior of `prior_n` fills at the class's mean
(PRIORS, 24 October) and `prior_sd_bps`. A side's size multiplier is `kelly_frac` x (mean / variance) of its
situation over the same ratio for the whole class, held within `size_mult_min`..`size_mult_max`, and
`size_mult_min` whenever the situation's mean less `lcb_z` standard errors is below 0. The side that reduces the
inventory gap is then multiplied by 1 + `inv_gamma` x |gap| / band (the side that adds to it is already cut by
1Gpt's inventory sizing). The result multiplies the size 9Gpt planned, before every limit 9Gpt applies (per-order
cap, post-fill band, funds, minimum), so `max_order_fraction` has to leave room for multipliers above 1.

Also fixed here: 7Gpt read a parsed trade's taker as `takerAgentId`, which TradeInfo does not have (its fields are
`Ta` / `taker_agent_id`), so with lazy_load off (the real miner, the replay) its miner share was always 0 and
`toxic_widen` never acted. The share is read from either form, and 7Gpt's toxic edge now sees it too.

With size_kelly=0 and inv_gamma=0 it places exactly 9Gpt's orders (apart from the toxic-edge fix, which only acts
when toxic_widen > 0).

Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_24_10Gpt.py --port 8888 --agent_id 0 --params lazy_load=0
"""
import importlib.util
import math
from pathlib import Path

import bittensor as bt
from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

_spec = importlib.util.spec_from_file_location("MyAgent10_23_9Gpt", Path(__file__).with_name("MyAgent10_23_9Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)

CLASS10_PARAMS = {
    "size_kelly":      1.0,     # 0 = 9Gpt's sizes
    "kelly_frac":      1.0,     # multiplier = this x the situation's mean / variance over the class's
    "size_mult_min":   0.25,
    "size_mult_max":   3.0,
    "lcb_z":           1.0,     # mean - this x standard error below 0 -> size_mult_min
    "prior_n":         20.0,    # pseudo-fills of the prior
    "prior_sd_bps":    6.0,
    "edge_halflife_s": 1800.0,  # sim seconds
    "post_window_s":   5.0,     # reset phase: within this many sim seconds after the reseed boundary
    "miner_window_s":  30.0,    # decay of the book's taker record, sim seconds
    "miner_line":      0.5,     # miner share of taker notional at or above which a book counts as miner-heavy
    "inv_gamma":       1.0,     # reducing side x (1 + this x |gap| / band)
}
# Mean clamped realized markout of our own maker fills by class, bps (24 October, 1827 fills).
PRIORS = {0: 1.2, 1: 2.0, 2: 0.8}


def _taker_maker(event):
    """(taker id, maker id, notional) of a parsed or raw trade, or None for anything else."""
    if isinstance(event, dict):
        if event.get("y") != "t":
            return None
        return event.get("Ta"), event.get("Ma"), (event.get("q") or 0.0) * (event.get("p") or 0.0)
    if getattr(event, "type", None) != "t" and getattr(event, "y", None) != "t":
        return None
    taker = next((v for v in (getattr(event, n, None) for n in ("Ta", "taker_agent_id", "takerAgentId")) if v is not None), None)
    maker = next((v for v in (getattr(event, n, None) for n in ("Ma", "maker_agent_id", "makerAgentId")) if v is not None), None)
    quantity = next((v for v in (getattr(event, n, None) for n in ("q", "quantity")) if v is not None), 0.0)
    price = next((v for v in (getattr(event, n, None) for n in ("p", "price")) if v is not None), 0.0)
    return taker, maker, quantity * price


class MyAgent10_24_10Gpt(_parent.MyAgent10_23_9Gpt):
    """MyAgent10_23_9Gpt with situation-based Kelly sizing and inventory-reducing size; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.class10_params = {}
        self.edge_stats = {}        # (validator, class, situation) -> [weight, sum x, sum x^2, last update ns]
        self.edge_pending = {}      # validator -> [(fill ns, book, sign, price, mid at fill, class, situation)]
        self.edge_last_ts = {}      # validator -> last state timestamp, to notice a new simulation
        p = self._params10_index(0)
        bt.logging.info(f"10Gpt: kelly sizing {int(p['size_kelly'] > 0)} x{p['kelly_frac']:g} "
                        f"[{p['size_mult_min']:g}, {p['size_mult_max']:g}], half-life {p['edge_halflife_s']:g} s, "
                        f"inventory gamma {p['inv_gamma']:g} (class 0; _c<k> overrides per class)")

    def _params10_index(self, index):
        params = self.class10_params.get(index)
        if params is None:
            params = {}
            for name, default in CLASS10_PARAMS.items():
                base = float(getattr(self.config, name, default))
                params[name] = float(getattr(self.config, f"{name}_c{index}", base))
            self.class10_params[index] = params
        return params

    def _sync_config(self, state):
        before = self._layout
        super()._sync_config(state)
        if self._layout != before:
            self.class10_params.clear()

    # ── The book's takers: read from parsed trades too (7Gpt's fix) ─────────────────────────────────────────────

    def _taker_mix(self, book):
        """(miner taker notional, total taker notional) since the previous state, own trades left out."""
        miners = total = 0.0
        raw = getattr(book, "_raw", None)
        events = (raw.get("e") or []) if isinstance(raw, dict) else (getattr(book, "events", None) or [])
        for event in events:
            trade = _taker_maker(event)
            if trade is None:
                continue
            taker, maker, notional = trade
            if taker is None or self.uid in (taker, maker):
                continue
            total += notional
            if taker >= 0:
                miners += notional
        return miners, total

    def _remember(self, validator, book_id, book, timestamp):
        last = self.edge_last_ts.get(validator)
        if last is not None and timestamp < last:
            # a new simulation: its fills say nothing about the old one's situations
            for key in [k for k in self.edge_stats if k[0] == validator]:
                del self.edge_stats[key]
            self.edge_pending.pop(validator, None)
        self.edge_last_ts[validator] = timestamp
        memory = super()._remember(validator, book_id, book, timestamp)
        p = self._params10_index(self._class_index(book_id))
        miners, total = self._taker_mix(self._plain(book))
        decay = math.exp(-(memory.get("elapsed") or 0.0) / max(p["miner_window_s"], 1e-9))
        memory["m10_miner"] = memory.get("m10_miner", 0.0) * decay + miners
        memory["m10_total"] = memory.get("m10_total", 0.0) * decay + total
        return memory

    # ── Situations and their estimates ───────────────────────────────────────────────────────────────────────

    def _situation(self, memory, direction, index):
        """(post-reset, with-flow, miner-heavy) for a quote on `direction` of this book now."""
        p = self._params10_index(index)
        post = self._phase(memory["timestamp"]) < p["post_window_s"] - 1e-9
        flow = memory.get("flow") or 0.0
        # a resting ask is hit by taker buys (flow > 0), a resting bid by taker sells
        with_flow = flow > 0 if direction == OrderDirection.SELL else flow < 0
        total = memory.get("m10_total", 0.0)
        heavy = total > 0 and memory.get("m10_miner", 0.0) / total >= p["miner_line"]
        return (bool(post), bool(with_flow), bool(heavy))

    def _decayed(self, entry, now, halflife):
        w, s1, s2, then = entry
        if now > then:
            f = 0.5 ** ((now - then) / 1e9 / max(halflife, 1e-9))
            w, s1, s2 = w * f, s1 * f, s2 * f
        return w, s1, s2

    def _estimate(self, validator, index, situations, now):
        """(mean, variance, standard error) of the clamped realized bps over `situations` (None = the whole class),
        with the class prior."""
        p = self._params10_index(index)
        w = s1 = s2 = 0.0
        for (v, i, s), entry in self.edge_stats.items():
            if v != validator or i != index or (situations is not None and s not in situations):
                continue
            dw, d1, d2 = self._decayed(entry, now, p["edge_halflife_s"])
            w, s1, s2 = w + dw, s1 + d1, s2 + d2
        n0, mu0, sd0 = p["prior_n"], PRIORS.get(index, PRIORS[max(PRIORS)]), p["prior_sd_bps"]
        n = n0 + w
        mean = (n0 * mu0 + s1) / n
        var = max(1e-6, (n0 * (sd0 ** 2 + mu0 ** 2) + s2) / n - mean ** 2)
        return mean, var, math.sqrt(var / n)

    def _side_mult(self, direction, ctx):
        validator, book_id = ctx["validator"], ctx.get("book_id")
        memory = self.memory.get((validator, book_id))
        if memory is None:
            return 1.0
        index = self._class_index(book_id)
        p = self._params10_index(index)
        mult = 1.0
        if p["size_kelly"] > 0:
            now = memory["timestamp"]
            situation = self._situation(memory, direction, index)
            mean, var, se = self._estimate(validator, index, {situation}, now)
            if mean - p["lcb_z"] * se < 0:
                mult = p["size_mult_min"]
            else:
                c_mean, c_var, _ = self._estimate(validator, index, None, now)
                ref = c_mean / c_var if c_mean > 0 else PRIORS.get(index, 1.0) / p["prior_sd_bps"] ** 2
                mult = p["kelly_frac"] * (mean / var) / max(ref, 1e-9)
        if p["inv_gamma"] > 0:
            inventory = ctx["inventory"]
            reduces = inventory > 0 if direction == OrderDirection.SELL else inventory < 0
            if reduces:
                mult *= 1.0 + p["inv_gamma"] * abs(inventory) / max(self.inventory_band, 1e-9)
        return max(p["size_mult_min"], min(p["size_mult_max"], mult)) if (p["size_kelly"] > 0 or p["inv_gamma"] > 0) else 1.0

    def _size(self, direction, base_quantity, price, cancelled, ctx, count=True):
        """9Gpt's sizing, on the planned size times this side's multiplier."""
        mult = self._side_mult(direction, ctx)
        if count and mult != 1.0:
            index = self._class_index(ctx.get("book_id"))
            self._count(ctx["validator"], f"k10_up_c{index}" if mult > 1.0 else f"k10_down_c{index}")
        return super()._size(direction, base_quantity * mult, price, cancelled, ctx, count)

    # ── Fills: each one's situation, marked later ────────────────────────────────────────────────────────────

    def onTrade(self, event, validator=None):
        super().onTrade(event, validator)
        if event.makerAgentId != self.uid:
            return
        memory = self.memory.get((validator, event.bookId))
        if memory is None:
            return
        side = OrderDirection.SELL if event.side == OrderDirection.BUY else OrderDirection.BUY
        index = self._class_index(event.bookId)
        situation = self._situation(dict(memory, timestamp=event.timestamp), side, index)
        sign = 1 if side == OrderDirection.BUY else -1
        self.edge_pending.setdefault(validator, []).append(
            (event.timestamp, event.bookId, sign, event.price, memory["mid"], index, situation))

    def _settle_markouts(self, validator, timestamp):
        pending = self.edge_pending.get(validator)
        if pending:
            due = timestamp - self.markout_horizon
            keep = []
            for fill in pending:
                fill_time, book_id, sign, price, fill_mid, index, situation = fill
                memory = self.memory.get((validator, book_id))
                if memory is None or fill_time > memory["timestamp"]:
                    continue
                if fill_time > due:
                    keep.append(fill)
                    continue
                realized = sign * (memory["mid"] - price) / price * 1e4
                captured = abs(sign * (fill_mid - price) / price * 1e4)
                x = max(-captured, min(captured, realized))
                p = self._params10_index(index)
                key = (validator, index, situation)
                w, s1, s2 = self._decayed(self.edge_stats.get(key, (0.0, 0.0, 0.0, timestamp)), timestamp,
                                          p["edge_halflife_s"])
                self.edge_stats[key] = (w + 1.0, s1 + x, s2 + x * x, timestamp)
            self.edge_pending[validator] = keep
        super()._settle_markouts(validator, timestamp)

    # ── Score card ───────────────────────────────────────────────────────────────────────────────────────────

    def _plan_card(self, validator):
        card = super()._plan_card(validator)
        stats = self.plan_stats.get(validator, {})
        now = self.edge_last_ts.get(validator, 0)
        parts = []
        for index, name in enumerate(self._class_names() or ["class 0"]):
            cells = []
            for (v, i, s), entry in sorted(self.edge_stats.items()):
                if v != validator or i != index:
                    continue
                w, _, _ = self._decayed(entry, now, self._params10_index(index)["edge_halflife_s"])
                mean, _, se = self._estimate(validator, index, {s}, now)
                tag = ("post" if s[0] else "quiet") + ("/with" if s[1] else "/against") + ("/miner" if s[2] else "")
                cells.append(f"{tag} {mean:+.2f}+-{se:.2f} ({w:.0f})")
            parts.append(f"{name}: up {stats.get(f'k10_up_c{index}', 0)} down {stats.get(f'k10_down_c{index}', 0)}"
                         + (" [" + ", ".join(cells) + "]" if cells else ""))
        return card + " | 10GPT " + "; ".join(parts)


if __name__ == "__main__":
    launch(MyAgent10_24_10Gpt)
