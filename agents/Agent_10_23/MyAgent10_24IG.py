# SPDX-License-Identifier: MIT
"""
MyAgent10_24IG: MyAgent10_24_1IovC (IovC with its volume controls loosened) plus the parts of the 10Gpt line that the
24 October mainnet replays back.

Why (agents/data/replays/shield_grid_log.md, 24 October 17:00-20:03): on 3528 recorded mainnet rounds 1IovC scored
making 0 (uid quality 0.03, realized c0 -1.43, c1 -1.02 bps) while the live 9Gpt made 27.7: IovC quotes straight
through every 30 s fundamental reseed, and its class 1 fills 1-5 s after a reset (144k notional, 9Gpt: none) clamp the
whole uid's markout quality to 0. Its one good point was class 2 PnL (52 vs 9Gpt's 42). From the 10Gpt line it gets:
  - reseed shield (5Gpt): every quote is cancelled `shield_pre_s` (3) sim seconds before and `shield_post_s` (class 0
    1 s, classes 1 and 2 5 s) after each `reseed_s` (30 s) boundary; the live 9Gpt values. pre 3 is what removed the
    boundary losses once the one-round reply lag was modelled (22:45 grid, 23 October); c2 post 1 s instead of 5 s
    cut c2 PnL 42 -> 7 (18:20 grid),
  - quiet-window size (6Gpt): `size_fraction` x `quiet_size_mult` (c0 2.5, c1 1.25, c2 1.6, live) from `quiet_from_s`
    (5) to `quiet_to_s` (28) s into the cycle, where fills mark out at +2 to +5 bps; on top of the live sizes
    (size_fraction c0 0.012, c1 and c2 0.024, twice IovC's),
  - class 2 fundamental lean `coef_fund_c2` 0.002 (live since 19:06): the only variant that turned class 2 net alpha
    positive (+15 vs -169) at -15% making; IovC's 0.0035 cost c2 PnL (42 -> 34),
  - 10Gpt's situation sizing (A): each side's size x a fractional-Kelly multiplier on a decayed Bayesian estimate of
    the clamped realized markout of past maker fills in the same situation (class, within `post_window_s` of the
    reseed, with or against the book's recent taker flow, miner-heavy book), with `lcb_z` 0: the corrected 10Gpt
    (19:40 grid, making +6%, c0 net alpha +151 vs +142). With lcb_z 1 its prior's standard error put every c0 and
    c2 situation at the minimum and halved making,
  - 10Gpt's inventory sizing (E): the side that reduces the inventory gap is x (1 + `inv_gamma` x |gap| / band),
  - its taker ids read from parsed trades as well as raw ones (IovC's `_prints` already does; 7Gpt's did not).
Not taken: 9Gpt's pending-aware planner (equal within noise on lagged rounds), expiry 0.9 s (making -19%), toxic
widen (only a candidate). The size multipliers apply to the planned size before 1IovC's limits: the per-order cap
`max_order_fraction` (3%), the inventory band and the funds.

Every value can be set through --params (per class `<name>_c<k>`); with shield_pre_s=0 shield_post_s=0
quiet_size_mult=1 size_kelly=0 inv_gamma=0 coef_fund_c2=0.0035 size_fraction_c0=0.006 size_fraction_c1=0.012
size_fraction_c2=0.012 it places exactly MyAgent10_24_1IovC's orders.

MyAgent10_24_1IovC (the base, unchanged): MyAgent10_23IovC with
  - `pace_bucket` an hour instead of 60 s, and a pacing pause cancels only the side that adds to the inventory gap
    (60 s of allowance was about one class 1 fill, after which the book went dark for ~1 minute); the daily cap
    still stops both,
  - budget sizing off (`budget_sizing=0`), which made quotes smaller on the books that fill most often,
  - spread multipliers capped at x1.25 class and x1.25 book toward a 2 bps markout target (were 2 x 2 toward 3 bps),
  - one order up to 3% of the book's wealth (`max_order_fraction`; 2% capped every class 1 and class 2 quote),
  - lazy_load always off: IovC slices book levels, which lazily parsed books cannot do.

Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_24IG.py --port 8888 --agent_id 0 --params lazy_load=0
"""
import importlib.util
import math
from pathlib import Path

import bittensor as bt
from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

# The parent is loaded from its file next to this one, so the agent works however its directory is put on the path
# (the miner's agent loader, the replay harness or a direct launch).
_spec = importlib.util.spec_from_file_location("MyAgent10_23IovC", Path(__file__).with_name("MyAgent10_23IovC.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)

# Defaults that differ from MyAgent10_23IovC; any of them passed in --params wins.
DEFAULTS = {
    "pace_bucket":        3600.0,
    "budget_sizing":      0.0,
    "adapt_max":          1.25,
    "book_mult_max":      1.25,
    "markout_target_bps": 2.0,
    "max_order_fraction": 0.03,
    "pace_one_sided":     1.0,
    # the live 9Gpt's sizes and class 2 lean (24 October 16:01 and 19:06)
    "size_fraction_c0":   0.012,
    "size_fraction_c1":   0.024,
    "size_fraction_c2":   0.024,
    "coef_fund_c2":       0.002,
}

# Per-class parameters added here; `<name>_c<k>` in --params overrides one for class k, else the class default below,
# else `<name>`, as for IovC's.
IG_PARAMS = {
    # reseed shield (5Gpt): every quote cancelled this many sim seconds before / after each reseed boundary
    "shield_pre_s":    3.0,
    "shield_post_s":   1.0,
    # quiet-window size (6Gpt): size_fraction x this from quiet_from_s to quiet_to_s into the cycle; 1 = off
    "quiet_size_mult": 2.5,
    "quiet_from_s":    5.0,
    "quiet_to_s":      28.0,
    # situation sizing (10Gpt A)
    "size_kelly":      1.0,     # 0 = off
    "kelly_frac":      1.0,     # multiplier = this x the situation's mean / variance over the class's
    "size_mult_min":   0.25,
    "size_mult_max":   3.0,
    "lcb_z":           0.0,     # mean - this x standard error below 0 -> size_mult_min
    "prior_n":         20.0,    # pseudo-fills of the prior
    "prior_sd_bps":    6.0,
    "edge_halflife_s": 1800.0,  # sim seconds
    "post_window_s":   5.0,     # reset phase: within this many sim seconds after the reseed boundary
    "miner_window_s":  30.0,    # decay of the book's taker record, sim seconds
    "miner_line":      0.5,     # miner share of taker notional at or above which a book counts as miner-heavy
    # inventory sizing (10Gpt E): reducing side x (1 + this x |gap| / band)
    "inv_gamma":       1.0,
}
IG_CLASS_DEFAULTS = {1: {"shield_post_s": 5.0, "quiet_size_mult": 1.25},
                     2: {"shield_post_s": 5.0, "quiet_size_mult": 1.6}}
# Mean clamped realized markout of the live miner's maker fills by class, bps (24 October, 1827 fills).
PRIORS = {0: 1.2, 1: 2.0, 2: 0.8}


class MyAgent10_24IG(_parent.MyAgent10_23IovC):
    """MyAgent10_24_1IovC with the reseed shield, quiet-window size, situation and inventory sizing of the 10Gpt line;
    see the module docstring."""

    def initialize(self):
        """Fills in this agent's defaults for every parameter not passed, then reads them as the parent does."""
        for name, value in DEFAULTS.items():
            if not hasattr(self.config, name):
                setattr(self.config, name, value)
        super().initialize()
        # The quoting slices book levels (book.bids[:5]), which taos.im.protocol.lazy's levels do not support: with
        # lazy_load on every book failed with "unhashable type: 'slice'" and nothing was quoted.
        self.config.lazy_load = False
        self.pace_one_sided = float(getattr(self.config, "pace_one_sided", 1.0)) > 0
        # The book being quoted; set by _over_volume_pace, which _quote_book calls before either side is quoted.
        self._pacing_memory = None
        self.reseed_s  = max(1.0, float(getattr(self.config, "reseed_s", 30.0)))
        self.ig_params = {}
        self.ig_stats  = {}         # validator -> counters for the score card
        self.edge_stats = {}        # (validator, class, situation) -> (weight, sum x, sum x^2, last update ns)
        self.edge_pending = {}      # validator -> [(fill ns, book, sign, price, mid at fill, class, situation)]
        self.edge_last_ts = {}      # validator -> last state timestamp, to notice a new simulation
        # The book `_quote_side` is sizing for: (validator, book_id, memory, quiet multiplier); set by _quote_book.
        self._sizing = None
        p = self._ig_params_index(0)
        bt.logging.info(f"IG: reseed shield pre {p['shield_pre_s']:g} s post {p['shield_post_s']:g} s, quiet size "
                        f"x{p['quiet_size_mult']:g}, kelly sizing {int(p['size_kelly'] > 0)} lcb_z {p['lcb_z']:g}, "
                        f"inventory gamma {p['inv_gamma']:g} (class 0; _c<k> overrides per class)")

    def _ig_params_index(self, index):
        params = self.ig_params.get(index)
        if params is None:
            params = {}
            defaults = IG_CLASS_DEFAULTS.get(min(index, max(IG_CLASS_DEFAULTS)), {}) if index > 0 else {}
            for name, default in IG_PARAMS.items():
                base = float(getattr(self.config, name, default))
                params[name] = float(getattr(self.config, f"{name}_c{index}", defaults.get(name, base)))
            self.ig_params[index] = params
        return params

    def _ig_params(self, book_id):
        return self._ig_params_index(self._class_index(book_id))

    def _count(self, validator, key):
        stats = self.ig_stats.setdefault(validator, {})
        stats[key] = stats.get(key, 0) + 1

    def _phase(self, timestamp):
        """Sim seconds since the last reseed boundary."""
        return (timestamp / 1e9) % self.reseed_s

    # ── Memory: the book's taker mix, a new simulation ───────────────────────────────────────────────────────

    def _remember(self, validator, book_id, book, timestamp):
        """The parent's memory, plus the book's miner and total taker notional (own trades left out), decayed over
        `miner_window_s`. A clock going back also clears the fill situations' estimates."""
        last = self.edge_last_ts.get(validator)
        if last is not None and timestamp < last:
            # a new simulation: its fills say nothing about the old one's situations
            for key in [k for k in self.edge_stats if k[0] == validator]:
                del self.edge_stats[key]
            self.edge_pending.pop(validator, None)
            self.ig_stats.pop(validator, None)
        self.edge_last_ts[validator] = timestamp
        memory = super()._remember(validator, book_id, book, timestamp)
        miners = total = 0.0
        for price, quantity, _, maker, taker in self._prints(book):
            if taker is None or self.uid in (taker, maker):
                continue
            total += price * quantity
            if taker >= 0:
                miners += price * quantity
        decay = math.exp(-(memory.get("elapsed") or 0.0) / max(self._ig_params(book_id)["miner_window_s"], 1e-9))
        memory["ig_miner"] = memory.get("ig_miner", 0.0) * decay + miners
        memory["ig_total"] = memory.get("ig_total", 0.0) * decay + total
        return memory

    # ── Reseed shield and quiet-window size ──────────────────────────────────────────────────────────────────

    def _shielded(self, book_id, timestamp):
        if getattr(self, "exchange_mode", False):
            return False    # no fundamental reseeds on the exchange
        p = self._ig_params(book_id)
        phase = self._phase(timestamp)
        return phase >= self.reseed_s - p["shield_pre_s"] - 1e-9 or phase < p["shield_post_s"] - 1e-9

    def _quote_book(self, response, validator, book_id, book, memory):
        """Inside the shield window every quote on the book is cancelled; outside it, the parent's quoting, with
        `_quote_side` sizing each side."""
        index = self._class_index(book_id)
        if self._shielded(book_id, memory["timestamp"]):
            account = self.accounts[book_id]
            # the parent predicts every round, so its decayed inputs stay current while the book is not quoted
            self._predict(book, account, memory, self._params(book_id))
            self._cancel(response, book_id, list(account.orders))
            memory["quoting"] = False
            self._count(validator, f"shield_c{index}")
            return
        p = self._ig_params(book_id)
        quiet = p["quiet_from_s"] - 1e-9 <= self._phase(memory["timestamp"]) < p["quiet_to_s"] - 1e-9
        self._sizing = (validator, book_id, memory, p["quiet_size_mult"] if quiet else 1.0)
        try:
            super()._quote_book(response, validator, book_id, book, memory)
        finally:
            self._sizing = None

    # ── Pacing pause (1IovC) ─────────────────────────────────────────────────────────────────────────────────

    def _over_volume_pace(self, book_id, account, memory):
        """True only at the daily cap. A pacing pause instead marks the book, and `_quote_side` then quotes only the
        side that reduces the inventory gap, so a book that has just filled can still be filled back."""
        self._pacing_memory = memory
        memory["pace_paused"] = False
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        paced = super()._over_volume_pace(book_id, account, memory)
        if not paced or not self.pace_one_sided or wealth <= 0:
            return paced
        cap = self.turnover_cap * wealth
        day = sum(memory["volume"].values())
        if (account.traded_volume or 0.0) >= self.turnover_margin * cap or day >= self.turnover_margin * cap:
            return True
        memory["pace_paused"] = True
        return False

    def _quote_side(self, response, book_id, orders, target, direction, quantity, min_quantity, volume_decimals,
                    away, toward, free, expiry):
        """The parent's quote at this side's size (`_side_quantity`), except during a pacing pause, when the side
        that would add to the inventory gap (the bid when long, the ask when short, both when flat) is cancelled."""
        memory = self._pacing_memory
        if memory is not None and memory.get("pace_paused"):
            gap = memory["alpha"].get("inventory") or 0.0
            reduces = (direction == OrderDirection.SELL and gap > 0) or (direction == OrderDirection.BUY and gap < 0)
            if not reduces:
                self._cancel(response, book_id, orders)
                return False
        quantity = self._side_quantity(book_id, direction, quantity, min_quantity, volume_decimals)
        return super()._quote_side(response, book_id, orders, target, direction, quantity, min_quantity,
                                   volume_decimals, away, toward, free, expiry)

    # ── Sizing: quiet window x situation x inventory ─────────────────────────────────────────────────────────

    def _side_quantity(self, book_id, direction, quantity, min_quantity, volume_decimals):
        """The parent's planned size times the quiet-window, situation and inventory multipliers, re-cut to the
        parent's limits (minimum order, budget size, `max_order_fraction`); `quantity` when they are all 1."""
        if self._sizing is None or self._sizing[1] != book_id:
            return quantity
        validator, _, memory, quiet = self._sizing
        account = self.accounts[book_id]
        mid = memory["mid"]
        wealth = account.base_balance.total * mid + account.quote_balance.total
        inventory = self._inventory_gap(account, memory, mid, wealth)
        mult = quiet * self._side_mult(validator, book_id, memory, direction, inventory)
        if mult == 1.0:
            return quantity
        index = self._class_index(book_id)
        self._count(validator, f"size_up_c{index}" if mult > 1.0 else f"size_down_c{index}")
        params = self._params(book_id)
        size = params["size_fraction"] * mult * wealth / mid if params["size_fraction"] > 0 else self.quantity * mult
        if self.budget_sizing:
            size = min(size, self._budget_size(book_id, memory, mid))
        size = round(max(size, min_quantity), volume_decimals)
        if self.max_order_fraction > 0:
            step = 10 ** -volume_decimals
            cap_quantity = math.floor(self.max_order_fraction * wealth / mid / step + 1e-9) * step
            size = round(min(size, cap_quantity), volume_decimals)
        return size

    def _situation(self, memory, direction, index, timestamp):
        """(post-reset, with-flow, miner-heavy) for a quote on `direction` of this book at `timestamp`."""
        p = self._ig_params_index(index)
        post = self._phase(timestamp) < p["post_window_s"] - 1e-9
        flow = memory.get("flow") or 0.0
        # a resting ask is hit by taker buys (flow > 0), a resting bid by taker sells
        with_flow = flow > 0 if direction == OrderDirection.SELL else flow < 0
        total = memory.get("ig_total", 0.0)
        heavy = total > 0 and memory.get("ig_miner", 0.0) / total >= p["miner_line"]
        return (bool(post), bool(with_flow), bool(heavy))

    @staticmethod
    def _decayed(entry, now, halflife):
        w, s1, s2, then = entry
        if now > then:
            f = 0.5 ** ((now - then) / 1e9 / max(halflife, 1e-9))
            w, s1, s2 = w * f, s1 * f, s2 * f
        return w, s1, s2

    def _estimate(self, validator, index, situations, now):
        """(mean, variance, standard error) of the clamped realized bps over `situations` (None = the whole class),
        with the class prior."""
        p = self._ig_params_index(index)
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

    def _side_mult(self, validator, book_id, memory, direction, inventory):
        """10Gpt's multiplier for this side: Kelly on its situation's estimate over the class's, times the inventory
        term on the reducing side, within `size_mult_min`..`size_mult_max`; 1 with both off."""
        index = self._class_index(book_id)
        p = self._ig_params_index(index)
        if p["size_kelly"] <= 0 and p["inv_gamma"] <= 0:
            return 1.0
        mult = 1.0
        if p["size_kelly"] > 0:
            now = memory["timestamp"]
            mean, var, se = self._estimate(validator, index, {self._situation(memory, direction, index, now)}, now)
            if mean - p["lcb_z"] * se < 0:
                mult = p["size_mult_min"]
            else:
                c_mean, c_var, _ = self._estimate(validator, index, None, now)
                ref = c_mean / c_var if c_mean > 0 else PRIORS.get(index, 1.0) / p["prior_sd_bps"] ** 2
                mult = p["kelly_frac"] * (mean / var) / max(ref, 1e-9)
        if p["inv_gamma"] > 0:
            reduces = inventory > 0 if direction == OrderDirection.SELL else inventory < 0
            if reduces:
                mult *= 1.0 + p["inv_gamma"] * abs(inventory) / max(self.inventory_band, 1e-9)
        return max(p["size_mult_min"], min(p["size_mult_max"], mult))

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
        situation = self._situation(memory, side, index, event.timestamp)
        sign = 1 if side == OrderDirection.BUY else -1
        self.edge_pending.setdefault(validator, []).append(
            (event.timestamp, event.bookId, sign, event.price, memory["mid"], index, situation))

    def _settle_markouts(self, validator, timestamp):
        """Each maker fill older than `markout_horizon`, its realized bps clamped to +/- its captured bps as the
        validator clamps it, into its situation's decayed estimate; then the parent's markouts."""
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
                key = (validator, index, situation)
                w, s1, s2 = self._decayed(self.edge_stats.get(key, (0.0, 0.0, 0.0, timestamp)), timestamp,
                                          self._ig_params_index(index)["edge_halflife_s"])
                self.edge_stats[key] = (w + 1.0, s1 + x, s2 + x * x, timestamp)
            self.edge_pending[validator] = keep
        super()._settle_markouts(validator, timestamp)

    # ── Score card ───────────────────────────────────────────────────────────────────────────────────────────

    def _report(self, validator, timestamp, returns):
        """The parent's score card, followed, whenever it is printed, by this agent's shield and sizing line."""
        before = self.next_report.get(validator)
        super()._report(validator, timestamp, returns)
        if before is None or timestamp < before or not returns:
            return
        try:
            names = [cls.name for cls in self.asset_classes()]
        except Exception:
            names = []
        stats = self.ig_stats.get(validator, {})
        parts = []
        for index, name in enumerate(names or ["class 0"]):
            cells = []
            for (v, i, s), entry in sorted(self.edge_stats.items()):
                if v != validator or i != index:
                    continue
                w, _, _ = self._decayed(entry, timestamp, self._ig_params_index(index)["edge_halflife_s"])
                mean, _, se = self._estimate(validator, index, {s}, timestamp)
                tag = ("post" if s[0] else "quiet") + ("/with" if s[1] else "/against") + ("/miner" if s[2] else "")
                cells.append(f"{tag} {mean:+.2f}+-{se:.2f} ({w:.0f})")
            parts.append(f"{name}: shielded {stats.get(f'shield_c{index}', 0)} book-rounds, sides sized up "
                         f"{stats.get(f'size_up_c{index}', 0)} down {stats.get(f'size_down_c{index}', 0)}"
                         + (" [" + ", ".join(cells) + "]" if cells else ""))
        bt.logging.info("IG CARD | " + " | ".join(parts))


if __name__ == "__main__":
    launch(MyAgent10_24IG)
