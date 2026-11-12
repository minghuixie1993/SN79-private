# SPDX-License-Identifier: MIT
"""
MyAgent10_23_2Gpt: MyAgent10_23_1Gpt with grid-safe prices, fee weighting, 5-minute pacing, per-class expiry, a
bounded lean, per-book markout widening and a counterparty monitor.

One self-contained file: MyAgentV3, MyAgent10_23_Gpt and MyAgent10_23_1Gpt are embedded below unchanged (copied on
23 October from agents/Agent_10_23/MyAgent10_23_Gpt.py, which itself embeds agents/MyAgentV3.py, and from
MyAgent10_23_1Gpt.py), so later edits to those files cannot change this agent. 2Gpt's own class is at the end.

What 2Gpt changes, from the survey of agents/Agent_10_23 (MyAgent10_23IOV led making on every validator of the
23 October network run, at about 1.6x Gpt's) and the engine's code:
  - grid-safe prices: the engine fits an order's price to the tick grid by truncation (OrderPlacementValidator.cpp
    util::round; its test TheOrderGridPathStillTruncates), and 48% of two-decimal prices are stored as a double a
    hair below their decimal, so they landed one tick low: bids a tick further out, asks a tick closer in and into
    post-only refusals (111 of IOV's 134 refusals were class 0 asks; a class 1 tick is ~3.4 bps). Every price is
    sent `grid_nudge` of a tick higher, far too little to reach the next tick,
  - fees: the validator nets only `making_fee_weight` (0.005) of each maker fee from making credit and skill alpha
    ignores fees, so the half spread carries `fee_weight` (0.005) of the maker fee instead of all of it, and a book
    pauses only above `max_fee_rate` 2% (the dynamic fee tops out at 1.5%) instead of V3's 0.2%,
  - pacing in `pace_period_s` (300 s) periods instead of hours: with hourly periods a book spent its share in the
    first few minutes and sat dark for the rest of the hour, and 97% of refusals fell in those minutes,
  - per-class expiry (`expiry_s`): 60 s on class 0, 300 s on class 1, where queue place decides fills and IOV's
    class 1 fills marked out +8-9 bps at quality 0.62-0.67,
  - a bounded lean (`max_lean`, 1 half spread): V3 could lean up to 2 x 15 bps, beyond the half spread, putting a
    quote across the mid (IOV's bounded lean: 33 post-only refusals against 1015 unbounded),
  - per-book markout widening: the validator's markout quality is one ratio over all of an agent's books, so a book
    whose own fills mark out below `markout_target_bps` widens by up to `book_mult_max` (2x) at `book_adapt_rate`,
    on top of V3's class multiplier,
  - a counterparty monitor on the PLAN CARD: per class, the background share of maker notional over the
    validator's 3 h window and the S3 factor it implies (miner-sourced credit counts up to 4x the background's).
    IOV's making lead came from S3 (0.5-0.6 against Gpt's 0.24-0.32), not from markout, so this is the number to
    move next; it only means something with other miners present (a network run).

Every addition has a setting that turns it off (grid_nudge=0, fee_weight=1 with max_fee_rate=0.002,
pace_period_s=3600, expiry_s=60, max_lean=inf, book_adapt_rate=0); with those and 1Gpt's and Gpt's own switches
off, the agent places exactly MyAgentV3's orders (tests/test_10_23_2Gpt.py, T00).

Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_23_2Gpt.py --port 8888 --agent_id 1 --params lazy_load=1
"""
import contextvars
import gc
import math

import bittensor as bt
from taos.common.agents import launch
from taos.im.agents import FinanceAgent, UnifiedAccount
from taos.im.protocol.models import OrderDirection, STP, TimeInForce

# --------------------------------------------------------------------------------------------------------------------
# MyAgentV3, the parent strategy, embedded unchanged from agents/MyAgentV3.py (23 October).
# --------------------------------------------------------------------------------------------------------------------

HOUR = 3_600_000_000_000
DAY_HOURS = 24

# Per-class strategy parameters and their defaults; `<name>_c<k>` in --params overrides one for class k.
# The coefficients are bps of predicted 20 s move per unit of their input (see _predict).
CLASS_PARAMS = {
    "half_spread":          0.0006,
    "vol_mult":             1.3,
    "lean":                 2.0,
    "coef_fund":            0.04,
    "coef_ofi":             1.5,
    "coef_flow":            0.26,
    "coef_imb5":            1.1,
    "coef_imb1":            0.27,
    "coef_mom5":            0.15,
    "coef_mom300":          -0.04,
    "fill_skew":            0.3,
    "fill_confidence":      3.0,
    "size_fraction":        0.006,
    "min_half_ticks":       1.5,
    "requote_toward_ticks": 0.0,
    "cross_guard_ticks":    3.0,
}
# Class defaults by index; classes past the last one listed start from its settings.
CLASS_DEFAULTS = {1: {"half_spread": 0.0005, "vol_mult": 1.5, "lean": 1.0, "coef_fund": 0.0035, "coef_ofi": 0.0,
                      "coef_flow": 8.0, "coef_imb5": 0.0, "coef_imb1": 0.27, "coef_mom5": -0.2, "coef_mom300": 0.025,
                      "size_fraction": 0.012, "requote_toward_ticks": 1.0, "cross_guard_ticks": 1.0}}


class MyAgentV3(FinanceAgent):
    """Predictive two-sided market maker, tuned to the validator's de-beta scoring.

    What the score pays (taos/im/validator/debeta.py, reward.py, at the shipped defaults):
      - making (half the score): per book 2 x min(buy capture, sell capture) against a centered trade mid, times the
        uid's markout quality, the share of that capture still held 20 s after each maker fill (realized / captured,
        clamped to 0..1 over all books). A maker the market runs over keeps none of its credit.
      - skill (the other half): per book alpha = MTM P&L - mean inventory x drift; only books whose |alpha| clears
        2.3 bps of their filled notional count, kappa needs at least 20 of them, punishes negative ones cubically,
        and is scaled down when fewer than 62.5% of the books are filled at all.
    Both legs want the same thing: fills whose price still holds 20 s later, on both sides of every book.

    On every book it keeps one passive bid and one passive ask around a fair price:
      - the fair price is the mid moved by `lean` times a predicted 20 s move: a linear blend of the gap to the
        book's published fundamental, order-flow imbalance at the touch over ~5 s (`ofi`), signed taker flow over
        ~20 s (`flow`), the top-`imbalance_depth` and top-of-book volume imbalance (own orders taken out), and the
        mid's own move over ~5 s and ~300 s (`mom5`, `mom300`), each with a per-class `coef_<input>`; the
        prediction is held within +/- `max_pred_bps`. Fit on a recorded local run (128 books, one hour): out of
        sample it correlates about 0.24 with the 20 s move on class 0 and 0.19 on class 1. The fundamental gap is
        the strongest single input (+0.21); fills against it marked out at -5.3 bps, fills with it at +1.9.
      - quotes sit a half spread either side of it: the largest of `half_spread`, `vol_mult` times the mid's
        volatility over the 20 s markout horizon (an exponential average over `vol_window` seconds) and
        `min_half_ticks` ticks, times the class's markout multiplier, plus the maker fee (a rebate tightens).
        On class 0 (spread ~1 bps, 20 s move ~10 bps) a quote near the touch is filled only by the moves that keep
        going: replaying the run, quotes 5 bps out marked out at -2.3 bps and kept none of their making credit,
        while 6 bps / 1.3 sigma with lean 2 kept 44% of it at +3.1 bps a fill. Class 1 (spread ~10 bps, 20 s
        move ~3 bps) is the reverse: the touch itself holds its spread.
      - the markout multiplier is the agent's own check on that: every maker fill is marked against the mid
        `markout_horizon` later, and the class's spread widens while its fills mark out below
        `markout_target_bps` and narrows while above, within `adapt_min`..`adapt_max`,
      - both quotes shift away from the side the agent is already heavy on (`skew` half spreads per unit of
        inventory gap: the BASE held beyond the book's initial BASE, as a share of wealth), and away from the side
        whose fills have run ahead over the last `fill_window` (`fill_skew`, a fraction of the half spread, weighted
        down until about `fill_confidence` quotes' worth of fills back it, so one fill cannot move it far); that
        shift is capped at `max_skew` of the half spread, and once the inventory gap leaves +/- `inventory_band`
        only the side that reduces it is quoted. The target is the initial holding, not half of wealth: initial
        allocations differ by book (about 6-50% of wealth in BASE), and a 50% target kept buying on every book that
        started light,
      - a quote never comes within `cross_guard_ticks` ticks of the opposite touch, so the touch moving during the
        response delay does not turn it into a refused post-only order,
      - each quote is `size_fraction` of the book's wealth (or `quantity` BASE when that is 0),
      - a resting quote that has drifted further from the fair price than its target is kept while within
        `requote_frac` of the half spread (never less than `requote_ticks` ticks); one that has drifted toward
        it is replaced once past `requote_toward_frac` of the half spread, or `requote_toward_ticks` ticks if more.
        On class 1 a tick is ~3.4 bps and the touch holds thousands of units, so a quote replaced every second
        sat at the back of the queue (90% of orders at the touch, 0.6% filled); there a one-tick wobble keeps it,
      - quoting stops on a book while its maker fee rate exceeds `max_fee_rate`, while its own fills run ahead
        of an even pace through the validator's daily volume cap (`turnover_cap` x miner wealth, at most
        `pace_burst` times the hourly share in any hour), and once its traded volume reaches `turnover_margin`
        of that cap, past which the validator drops every instruction except cancels.

    Parameters can differ by asset class: `<name>_c<k>` overrides `<name>` on the books of the k-th class
    (0 = books 0-95, 1 = books 96-127 on the multi-asset layout), e.g. `half_spread_c1=0.001`. Any later class starts
    from class 1's settings.

    The validator turns response time into order delay (10 ms up to 1 s, exponential in the share of its timeout), so
    the garbage collector is told to run rarely: its full passes over a parsed state were most of the slow responses.

    Local test (from the agents directory, with the proxy and simulator running):
        python MyAgentV3.py --port 8888 --agent_id 0 --params lean_c0=2 half_spread_c0=0.0006 cross_guard_ticks_c0=3
    """

    def initialize(self):
        """Reads strategy parameters; every value passed via --params arrives as a float."""
        self.quantity          = float(getattr(self.config, "quantity", 1.0))
        # In half spreads per unit of inventory gap; 4 matches the former absolute 0.002 at a 5 bps half spread.
        self.skew              = float(getattr(self.config, "skew", 4.0))
        self.max_skew          = float(getattr(self.config, "max_skew", 0.8))
        self.inventory_band    = float(getattr(self.config, "inventory_band", 0.25))
        self.imbalance_depth   = int(getattr(self.config, "imbalance_depth", 5))
        self.vol_window        = float(getattr(self.config, "vol_window", 60.0))
        self.vol_horizon       = float(getattr(self.config, "vol_horizon", 20.0))
        self.max_pred_bps      = float(getattr(self.config, "max_pred_bps", 15.0))
        self.fill_window       = float(getattr(self.config, "fill_window", 1800.0))
        self.requote_frac        = float(getattr(self.config, "requote_frac", 0.3))
        self.requote_toward_frac = float(getattr(self.config, "requote_toward_frac", 0.1))
        self.requote_ticks       = int(getattr(self.config, "requote_ticks", 1))
        self.max_fee_rate      = float(getattr(self.config, "max_fee_rate", 0.002))
        self.turnover_cap      = float(getattr(self.config, "turnover_cap", 10.0))
        self.turnover_margin   = float(getattr(self.config, "turnover_margin", 0.9))
        self.pace_burst        = float(getattr(self.config, "pace_burst", 2.0))
        self.expiry_period     = int(getattr(self.config, "expiry_period", 60_000_000_000))
        # Own markout check: each maker fill is marked against the mid this long after it.
        self.markout_horizon    = int(getattr(self.config, "markout_horizon", 20_000_000_000))
        self.markout_target_bps = float(getattr(self.config, "markout_target_bps", 3.0))
        self.markout_alpha      = float(getattr(self.config, "markout_alpha", 0.02))
        self.adapt_rate         = float(getattr(self.config, "adapt_rate", 0.002))
        self.adapt_scale_bps    = float(getattr(self.config, "adapt_scale_bps", 10.0))
        self.adapt_min          = float(getattr(self.config, "adapt_min", 0.7))
        self.adapt_max          = float(getattr(self.config, "adapt_max", 2.0))
        # Local score card: printed every `report_interval` simulation nanoseconds (default one minute).
        self.report_interval   = int(getattr(self.config, "report_interval", 60_000_000_000))
        # One instance serves every validator, each running its own simulation, so all state is keyed by
        # validator: per-book memory under (validator, book_id), fill statistics, pending markouts, spread
        # multipliers and report times by validator.
        self.memory            = {}
        self.maker_stats       = {}
        self.pending_markouts  = {}
        self.markout_stats     = {}
        self.spread_mult       = {}
        self.next_report       = {}
        self.class_params      = {}
        self.class_of_book     = {}
        # A parsed state is hundreds of thousands of small objects; default thresholds ran a full collection over
        # them every few states, which took the response from ~0.05 s to ~0.5 s.
        gc.set_threshold(50_000, 20, 100)

    def onTrade(self, event, validator=None):
        """Books this agent's fills into its hourly volume, counts its passive fills by side with the maker fees
        they cost (negative fees are rebates) and the notional they add to the book's fill balance, and queues
        each passive fill for its markout.

        `event.side` is the taker's side, so the maker's side is the opposite one.
        """
        if self.uid not in (event.makerAgentId, event.takerAgentId):
            return
        memory   = self.memory.get((validator, event.bookId))
        notional = event.price * event.quantity
        if memory is not None:
            hour = event.timestamp // HOUR
            memory["volume"][hour] = memory["volume"].get(hour, 0.0) + notional
        if event.makerAgentId != self.uid:
            return
        stats = self._stats(validator)
        side  = OrderDirection.SELL if event.side == OrderDirection.BUY else OrderDirection.BUY
        stats[side]   += 1
        stats["fees"] += event.makerFee
        if memory is not None:
            memory["fills"][side] += notional
            sign = 1 if side == OrderDirection.BUY else -1
            self.pending_markouts.setdefault(validator, []).append(
                (event.timestamp, event.bookId, sign, event.price, notional, memory["mid"]))

    def onOrderRejected(self, event):
        """Counts rejected placements for the score card, after the base class has logged the known causes."""
        super().onOrderRejected(event)
        stats = self._stats(None)
        stats["rejected"] = stats.get("rejected", 0) + 1

    def respond(self, state):
        """Builds one response covering every book; serves both simulation and exchange mode."""
        response  = self.make_response()
        validator = getattr(getattr(state, "dendrite", None), "hotkey", None)
        returns   = {}
        for book_id, book in (state.books or {}).items():
            if not book.bids or not book.asks or book_id not in self.accounts:
                continue
            try:
                memory = self._remember(validator, book_id, book, state.timestamp)
                self._quote_book(response, validator, book_id, book, memory)
                returns[book_id] = self._book_return(book_id, book, memory)
            except Exception as ex:
                # A problem on one book must not cost the instructions for every other book.
                bt.logging.error(f"BOOK {book_id} | quoting failed: {ex}")
        try:
            self._settle_markouts(validator, state.timestamp)
        except Exception as ex:
            bt.logging.error(f"markout check failed: {ex}")
        self._report(validator, state.timestamp, returns)
        return response

    def _class_index(self, book_id):
        """Index of the asset class `book_id` belongs to (0 when no class lists it)."""
        index = self.class_of_book.get(int(book_id))
        if index is None:
            index = next((k for k, cls in enumerate(self.asset_classes()) if int(book_id) in cls.books), 0)
            self.class_of_book[int(book_id)] = index
        return index

    def _params(self, book_id):
        """This book's class parameters: `<name>_c<k>` from --params, else the class default, else `<name>`."""
        classes = self.asset_classes()
        index   = self._class_index(book_id)
        params  = self.class_params.get((index, classes[index].name))
        if params is None:
            params   = {}
            defaults = CLASS_DEFAULTS.get(min(index, max(CLASS_DEFAULTS)), {}) if index > 0 else {}
            for name, default in CLASS_PARAMS.items():
                base  = float(getattr(self.config, name, default))
                value = getattr(self.config, f"{name}_c{index}", defaults.get(name, base))
                params[name] = float(value)
            self.class_params[(index, classes[index].name)] = params
        return params

    def _stats(self, validator):
        """This validator's maker fill counts by side and the maker fees paid, in QUOTE."""
        return self.maker_stats.setdefault(validator, {OrderDirection.BUY: 0, OrderDirection.SELL: 0, "fees": 0.0})

    def _remember(self, validator, book_id, book, timestamp):
        """This book's memory for this validator, carried forward to the new state: the volatility estimate, the
        decayed fill balance, and the decayed predictive inputs (order-flow imbalance, taker flow and momentum).

        A timestamp earlier than the last one seen means the validator started a new simulation, so the
        memory starts over.
        """
        best_bid, best_ask = book.bids[0], book.asks[0]
        mid    = (best_bid.price + best_ask.price) / 2
        memory = self.memory.get((validator, book_id))
        if memory is None or timestamp < memory["timestamp"]:
            memory = {"timestamp": timestamp, "mid": mid, "variance": None, "start_wealth": None, "start_base": None,
                      "volume": {}, "fills": {OrderDirection.BUY: 0.0, OrderDirection.SELL: 0.0},
                      "l1": None, "ofi": 0.0, "flow": 0.0, "mom5": 0.0, "mom300": 0.0, "pred": 0.0}
            self.memory[(validator, book_id)] = memory
        elapsed = (timestamp - memory["timestamp"]) / 1e9
        l1 = (best_bid.price, best_bid.quantity, best_ask.price, best_ask.quantity)
        if elapsed > 0 and mid > 0 and memory["mid"] > 0:
            ret = math.log(mid / memory["mid"])
            # Exponentially weighted squared log return of the mid, per second of simulation time.
            rate   = ret ** 2 / elapsed
            weight = 1 - math.exp(-elapsed / self.vol_window)
            previous = memory["variance"]
            memory["variance"] = rate if previous is None else previous + weight * (rate - previous)
            decay = math.exp(-elapsed / self.fill_window)
            for side in memory["fills"]:
                memory["fills"][side] *= decay
            # Inputs are sums decayed over a few seconds, in units of the top-of-book depth so books compare.
            depth = sum(lv.quantity for lv in book.bids[:5]) + sum(lv.quantity for lv in book.asks[:5])
            d5, d20, d300 = math.exp(-elapsed / 5), math.exp(-elapsed / 20), math.exp(-elapsed / 300)
            ofi = self._order_flow_imbalance(memory["l1"], l1)
            memory["ofi"]    = memory["ofi"] * d5 + (ofi / depth if depth > 0 else 0.0)
            memory["flow"]   = memory["flow"] * d20 + (self._taker_flow(book) / depth if depth > 0 else 0.0)
            memory["mom5"]   = memory["mom5"] * d5 + ret * 1e4
            memory["mom300"] = memory["mom300"] * d300 + ret * 1e4
        memory["elapsed"] = elapsed
        memory["timestamp"], memory["mid"], memory["l1"] = timestamp, mid, l1
        return memory

    @staticmethod
    def _order_flow_imbalance(previous, current):
        """Net BASE added to the bid side minus the ask side at the touch since the previous state (Cont, Kukanov
        and Stoikov): a raised or held bid adds its size, a lowered or held one takes the old size away, and
        the ask side counts the other way. 0 without a previous state."""
        if previous is None:
            return 0.0
        pbb, pqb, pba, pqa = previous
        bb, qb, ba, qa = current
        return ((qb if bb >= pbb else 0.0) - (pqb if bb <= pbb else 0.0)
                - (qa if ba <= pba else 0.0) + (pqa if ba >= pba else 0.0))

    @staticmethod
    def _taker_flow(book):
        """Signed taker BASE traded on the book since the previous state: buys positive, sells negative.

        Read from the raw events when the book is lazily parsed, so the orders and cancellations it carries are
        never built into models just to be skipped."""
        raw = getattr(book, "_raw", None)
        if isinstance(raw, dict):
            return sum((e["q"] if e["s"] == 0 else -e["q"]) for e in raw.get("e") or [] if e.get("y") == "t")
        return sum((e.quantity if e.side == 0 else -e.quantity) for e in book.events or [] if e.type == "t")

    def _book_return(self, book_id, book, memory):
        """Return on this book's wealth (marked at mid) since the agent first saw it in this simulation."""
        account = self.accounts[book_id]
        wealth  = account.base_balance.total * memory["mid"] + account.quote_balance.total
        if memory["start_wealth"] is None:
            memory["start_wealth"] = wealth
        start = memory["start_wealth"]
        return wealth / start - 1 if start > 0 else 0.0

    def _settle_markouts(self, validator, timestamp):
        """Marks every maker fill older than `markout_horizon` against its book's mid now, and moves the class's
        spread multiplier toward the width at which fills mark out at `markout_target_bps`.

        Also keeps, per class, the notional-weighted markout (realized) and the edge against the mid at the fill
        (captured), whose ratio is a rough stand-in for the validator's markout quality."""
        pending = self.pending_markouts.get(validator)
        if not pending:
            return
        due = timestamp - self.markout_horizon
        keep = []
        for fill in pending:
            fill_time, book_id, sign, price, notional, fill_mid = fill
            memory = self.memory.get((validator, book_id))
            if memory is None or fill_time > memory["timestamp"]:
                # a new simulation started after the fill: nothing to mark it against
                continue
            if fill_time > due:
                keep.append(fill)
                continue
            realized = sign * (memory["mid"] - price) / price * 1e4
            captured = sign * (fill_mid - price) / price * 1e4
            index = self._class_index(book_id)
            stats = self.markout_stats.setdefault((validator, index), {"realized": 0.0, "captured": 0.0, "notional": 0.0, "n": 0})
            stats["realized"] += realized * notional
            stats["captured"] += captured * notional
            stats["notional"] += notional
            stats["n"] += 1
            # Steered by the class's average markout, not each fill's: single fills are heavy-tailed, and stepping
            # on each one's clipped error settles where the median fill meets the target, wider than the mean
            # needs (the R5 run widened class 0 to x1.24 while its fills averaged +3.7 bps against a 3 bps target).
            average = stats.get("average", self.markout_target_bps)
            stats["average"] = average + self.markout_alpha * (realized - average)
            step = max(-1.0, min(1.0, (self.markout_target_bps - stats["average"]) / self.adapt_scale_bps))
            mult = self.spread_mult.get((validator, index), 1.0) * math.exp(self.adapt_rate * step)
            self.spread_mult[(validator, index)] = max(self.adapt_min, min(self.adapt_max, mult))
        self.pending_markouts[validator] = keep

    def _report(self, validator, timestamp, returns):
        """Prints a rough local stand-in for the validator score; the real score only exists on a network.

        The validator's skill gate counts books whose de-beta'd alpha clears a 2.3 bps hurdle, not books in
        profit, so for the real legs replay a recorded run through agents/eval/score_run.py.
        """
        due = self.next_report.get(validator)
        if due is None or timestamp + self.report_interval < due:
            if due is not None:
                # The clock went backwards: a new simulation, so its statistics start from zero.
                self.maker_stats.pop(validator, None)
                self.pending_markouts.pop(validator, None)
                for key in [k for k in self.markout_stats if k[0] == validator]:
                    del self.markout_stats[key]
                for key in [k for k in self.spread_mult if k[0] == validator]:
                    del self.spread_mult[key]
            self.next_report[validator] = timestamp + self.report_interval
        if timestamp < self.next_report[validator] or not returns:
            return
        self.next_report[validator] = timestamp + self.report_interval
        # Grouped by asset class name; a lookup miss lands in "other" rather than raising, because this runs
        # outside the per-book guard in respond and an exception here would cost the whole response.
        try:
            classes = self.asset_classes()
            names = {b: cls.name for cls in classes for b in cls.books}
        except Exception:
            classes, names = [], {}
        groups = {}
        for book_id, r in sorted(returns.items()):
            groups.setdefault(names.get(int(book_id), "other"), []).append(r)
        books = " | ".join(f"books up {name}: {sum(r > 0 for r in rs)}/{len(rs)}" for name, rs in groups.items())
        marks = []
        for index, cls in enumerate(classes):
            s = self.markout_stats.get((validator, index))
            if s and s["notional"] > 0:
                quality = max(0.0, min(1.0, s["realized"] / s["captured"])) if s["captured"] > 0 else 0.0
                marks.append(f"{cls.name}: markout {s['realized'] / s['notional']:+.2f} bps over {s['n']} fills, "
                             f"quality ~{quality:.2f}, spread x{self.spread_mult.get((validator, index), 1.0):.2f}")
        stats = self._stats(validator)
        bt.logging.info(
            f"SCORE CARD | avg return {100 * sum(returns.values()) / len(returns):+.4f}% | {books} | "
            f"maker fills buy {stats[OrderDirection.BUY]} / sell {stats[OrderDirection.SELL]} | "
            f"maker fees {stats['fees']:+.4f} | rejected {self._stats(None).get('rejected', 0)}"
            + (" | " + " | ".join(marks) if marks else "")
        )

    def _imbalances(self, book, account):
        """(bid - ask) / (bid + ask) volume over the top `imbalance_depth` levels and at the touch, with this agent's
        own resting quotes taken out, so the agent never leans on its own size; positive when bids are heavier."""
        own = {}
        for o in account.orders:
            key = (o.side, round(o.price, 10))
            own[key] = own.get(key, 0.0) + o.quantity

        def volume(levels, side, depth):
            return sum(max(0.0, lv.quantity - own.get((side, round(lv.price, 10)), 0.0)) for lv in levels[:depth])

        def ratio(depth):
            bid = volume(book.bids, OrderDirection.BUY, depth)
            ask = volume(book.asks, OrderDirection.SELL, depth)
            return (bid - ask) / (bid + ask) if bid + ask > 0 else 0.0
        return ratio(self.imbalance_depth), ratio(1)

    def _predict(self, book, account, memory, params):
        """Predicted move of the mid over the next 20 s, in bps, held within +/- `max_pred_bps`.

        A book without a published fundamental (the exchange mechanism) contributes no fundamental term."""
        mid = memory["mid"]
        fundamental = getattr(book, "fundamental", None)
        gap = (fundamental - mid) / mid * 1e4 if fundamental and fundamental > 0 else 0.0
        imb5, imb1 = self._imbalances(book, account)
        pred = (params["coef_fund"] * gap
                + params["coef_ofi"] * memory["ofi"]
                + params["coef_flow"] * memory["flow"]
                + params["coef_imb5"] * imb5
                + params["coef_imb1"] * imb1
                + params["coef_mom5"] * memory["mom5"]
                + params["coef_mom300"] * memory["mom300"])
        pred = max(-self.max_pred_bps, min(self.max_pred_bps, pred))
        memory["pred"] = pred
        return pred

    def _inventory_gap(self, account, memory, mid, wealth):
        """BASE held beyond the book's initial BASE, valued at mid as a share of wealth; positive when long.

        The initial BASE is the account's `initial` balance; an account without one (exchange mode) uses the
        balance first seen in this simulation instead.
        """
        initial = getattr(account.base_balance, "initial", None)
        if initial is None:
            if memory["start_base"] is None:
                memory["start_base"] = account.base_balance.total
            initial = memory["start_base"]
        return (account.base_balance.total - initial) * mid / wealth if wealth > 0 else 0.0

    def _fill_balance(self, memory, quote_notional, confidence_quotes):
        """(buy - sell) / (buy + sell) of this book's recent maker notional, scaled by how much fill evidence
        there is; positive when buys ran ahead.

        A single fill makes the raw ratio +/-1, which would push the quotes the full `fill_skew` on no evidence.
        The ratio is scaled by total / (total + `confidence_quotes` x the book's quote notional), so it reaches
        half strength after that many quotes' worth of fills. Measuring the evidence in quotes rather than in
        QUOTE keeps one setting right on both the ~300 and the ~30 books.
        """
        buys, sells = memory["fills"][OrderDirection.BUY], memory["fills"][OrderDirection.SELL]
        total = buys + sells
        if total <= 0:
            return 0.0
        prior = max(confidence_quotes, 0.0) * quote_notional
        return (buys - sells) / (total + prior)

    def _over_volume_pace(self, book_id, account, memory):
        """True while this book's volume would outrun the validator's daily cap.

        The cap is `turnover_cap` x miner wealth per book over a rolling day; past it the validator drops
        every new order. The agent stops at `turnover_margin` of it, both by the validator's own count and by
        its own fills over the last 24 hours, and keeps any one hour within `pace_burst` times an even
        hourly share, so the budget lasts the whole day instead of running out mid-run.
        """
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        if wealth <= 0:
            return False
        cap    = self.turnover_cap * wealth
        hour   = memory["timestamp"] // HOUR
        volume = memory["volume"]
        for old in [h for h in volume if h <= hour - DAY_HOURS]:
            del volume[old]
        day = sum(volume.values())
        return ((account.traded_volume or 0.0) >= self.turnover_margin * cap
                or day >= self.turnover_margin * cap
                or volume.get(hour, 0.0) >= self.pace_burst * cap / DAY_HOURS)

    def _quote_book(self, response, validator, book_id, book, memory):
        """Keeps one bid and one ask resting near their target prices on a single book."""
        account  = self.accounts[book_id]
        params   = self._params(book_id)
        best_bid = book.bids[0].price
        best_ask = book.asks[0].price
        mid      = memory["mid"]
        if mid <= 0:
            return

        bids = [o for o in account.orders if o.side == OrderDirection.BUY]
        asks = [o for o in account.orders if o.side == OrderDirection.SELL]
        # Every round, so the decayed inputs it reads stay current even while the book is not being quoted.
        pred = self._predict(book, account, memory, params)

        # Stop making while the maker fee is above tolerance (negative rates are rebates), and while the book's
        # volume is ahead of its pace through the cap: past the cap the validator drops new orders, so resting
        # quotes could not follow the market any more.
        fee_rate = account.fees.maker_fee_rate if account.fees is not None else 0.0
        if fee_rate > self.max_fee_rate or self._over_volume_pace(book_id, account, memory):
            self._cancel(response, book_id, bids)
            self._cancel(response, book_id, asks)
            return

        decimals = self.price_decimals(book_id)
        tick     = 10 ** -decimals
        fair     = mid * (1 + params["lean"] * pred / 1e4)
        # Half spread: wide enough for the mid's typical move over the markout horizon and for a whole tick of
        # edge on a coarse grid, scaled by the class's markout multiplier; the maker fee widens it and a rebate
        # narrows it, never below the tick floor.
        sigma = math.sqrt(memory["variance"] * self.vol_horizon) if memory["variance"] else 0.0
        floor = params["min_half_ticks"] * tick / mid
        half  = max(params["half_spread"], params["vol_mult"] * sigma, floor)
        half  = max(floor, half * self.spread_mult.get((validator, self._class_index(book_id)), 1.0) + fee_rate)

        # Inventory beyond the book's initial BASE, as a share of wealth: 0 is balanced, above it means long.
        wealth    = account.base_balance.total * mid + account.quote_balance.total
        inventory = self._inventory_gap(account, memory, mid, wealth)
        size      = params["size_fraction"] * wealth / mid if params["size_fraction"] > 0 else self.quantity
        # Lean away from the inventory and from the side whose fills ran ahead (making credit counts the smaller
        # side), by at most `max_skew` of the half spread, so the bid stays below the fair price and the ask above.
        shift = (- params["fill_skew"] * self._fill_balance(memory, size * mid, params["fill_confidence"]) * half
                 - self.skew * inventory * half)
        shift = max(-self.max_skew * half, min(self.max_skew * half, shift))

        # Never cross the book, with `cross_guard_ticks` of room: the order lands after the response delay, and a
        # post-only quote the touch has moved onto is refused after its predecessor was already cancelled. With
        # one tick of room the R5 run lost 486 class 0 asks this way (a ~3-tick spread), leaving those books with
        # no ask and the fills 2502 buys to 1771 sells.
        guard     = max(1.0, params["cross_guard_ticks"]) * tick
        bid_price = min(self._on_grid(fair * (1 + shift - half), decimals, True), round(best_ask - guard, decimals))
        ask_price = max(self._on_grid(fair * (1 + shift + half), decimals, False), round(best_bid + guard, decimals))
        if bid_price <= 0 or bid_price >= ask_price:
            return

        # One volume increment above the minimum: the engine rounds the float it receives down onto its
        # grid, so an order of exactly min_order_size (e.g. 2.5364 -> 2.53639999...) arrives as 2.5363.
        volume_decimals = self.volume_decimals(book_id)
        min_quantity    = self.min_order_size(book_id) + 10 ** -volume_decimals
        quantity        = round(max(size, min_quantity), volume_decimals)

        # A quote that drifted away from the fair price may rest a while; one that drifted toward it is the one
        # the market runs over, so it is replaced, unless within `requote_toward_ticks`, where its queue place is
        # worth more than the tick.
        away   = max(self.requote_frac * half * mid, self.requote_ticks * tick)
        toward = max(self.requote_toward_frac * half * mid, params["requote_toward_ticks"] * tick)
        # Far from balanced, quote only the side that brings inventory back.
        if inventory >= self.inventory_band:
            self._cancel(response, book_id, bids)
        elif self._needs_requote(response, book_id, bids, bid_price, OrderDirection.BUY, away, toward):
            # The cancels go ahead of the new order in the response, so the funds they hold count as free.
            free = account.quote_balance.free + sum(o.quantity * o.price for o in bids)
            if free >= quantity * bid_price:
                self._place(response, book_id, OrderDirection.BUY, quantity, bid_price)
        if inventory <= -self.inventory_band:
            self._cancel(response, book_id, asks)
        elif self._needs_requote(response, book_id, asks, ask_price, OrderDirection.SELL, away, toward):
            free = account.base_balance.free + sum(o.quantity for o in asks)
            if free >= quantity:
                self._place(response, book_id, OrderDirection.SELL, quantity, ask_price)

    @staticmethod
    def _on_grid(price, decimals, down):
        """`price` on the tick grid, rounded down for a bid and up for an ask, so rounding never moves a quote
        closer to the fair price. The 1e-9 keeps float noise (29.99 x 100 = 2998.9999...) from costing a tick."""
        units = price * 10 ** decimals
        units = math.floor(units + 1e-9) if down else math.ceil(units - 1e-9)
        return round(units / 10 ** decimals, decimals)

    def _needs_requote(self, response, book_id, orders, target, direction, away, toward):
        """True unless this side has exactly one quote near `target`: at most `away` further from the fair price
        than the target, or at most `toward` closer to it. Cancels any quotes it does not keep."""
        if len(orders) == 1:
            # Positive when the resting quote sits closer to the fair price than its target.
            closer = orders[0].price - target if direction == OrderDirection.BUY else target - orders[0].price
            if -away - 1e-9 <= closer <= toward + 1e-9:
                return False
        self._cancel(response, book_id, orders)
        return True

    def _cancel(self, response, book_id, orders):
        """Cancels every order in `orders`, as one instruction."""
        if orders:
            response.cancel_orders(book_id, [o.id for o in orders])

    def _place(self, response, book_id, direction, quantity, price):
        """Places a post-only, expiring limit order so it can only ever rest as a maker."""
        response.limit_order(
            book_id=book_id,
            direction=direction,
            quantity=quantity,
            price=price,
            stp=STP.CANCEL_BOTH,
            postOnly=True,
            timeInForce=TimeInForce.GTT,
            expiryPeriod=self.expiry_period,
        )


# --------------------------------------------------------------------------------------------------------------------
# MyAgent10_23_Gpt: the quote planner on top of MyAgentV3.
# --------------------------------------------------------------------------------------------------------------------

KEEP, REPLACE, CANCEL_ONLY, SKIP = "keep", "replace", "cancel_only", "skip"
DECISIONS = (KEEP, REPLACE, CANCEL_ONLY, SKIP)
SIDES = (OrderDirection.BUY, OrderDirection.SELL)

# The accounts of the request being served, per thread or task, so concurrent requests never mix them.
_REQUEST_ACCOUNTS = contextvars.ContextVar("my_agent_10_23_accounts", default=None)


class MyAgent10_23_Gpt(MyAgentV3):
    """MyAgentV3 with per-side quote plans that are sized, capped and checked before any cancel is sent."""

    def initialize(self):
        super().initialize()
        self.max_order_fraction        = max(0.0, float(getattr(self.config, "max_order_fraction", 0.02)))
        self.inventory_sizing          = max(0.0, min(1.0, float(getattr(self.config, "inventory_sizing", 0.8))))
        self.min_inventory_size_factor = max(0.0, min(1.0, float(getattr(self.config, "min_inventory_size_factor", 0.2))))
        self.post_fill_band            = bool(int(float(getattr(self.config, "post_fill_band", 1))))
        self.max_instructions_per_book = int(float(getattr(self.config, "max_instructions_per_book", 5)))
        # Plan and execution statistics by validator (reset with the simulation), rejections across validators
        # (the rejection notice does not say which validator it came from).
        self.plan_stats   = {}
        self.reject_stats = {}
        self.fill_stats   = {}

    # ── Request-scoped accounts ──────────────────────────────────────────────────────────────────────────────

    def update(self, state) -> None:
        """The framework's refresh, then this request's own accounts, built from its state rather than read
        back from `self.accounts`, which a concurrent request may already have replaced."""
        super().update(state)
        try:
            raw = (state.accounts or {}).get(self.uid, {})
            _REQUEST_ACCOUNTS.set({bid: a if isinstance(a, UnifiedAccount) else UnifiedAccount(a)
                                   for bid, a in raw.items()})
        except Exception:
            _REQUEST_ACCOUNTS.set(None)

    def _accounts(self):
        """The accounts of the request being served."""
        accounts = _REQUEST_ACCOUNTS.get()
        return accounts if accounts is not None else self.accounts

    def respond(self, state):
        """V3's response loop, with this request's accounts passed to every book."""
        response  = self.make_response()
        validator = getattr(getattr(state, "dendrite", None), "hotkey", None)
        accounts  = self._accounts()
        returns   = {}
        for book_id, book in (state.books or {}).items():
            if not book.bids or not book.asks or book_id not in accounts:
                continue
            try:
                memory  = self._remember(validator, book_id, book, state.timestamp)
                account = accounts[book_id]
                self._track_orders(validator, book_id, account, memory)
                self._quote_book(response, validator, book_id, book, memory, account)
                returns[book_id] = self._book_return(book_id, book, memory, account)
            except Exception as ex:
                # A problem on one book must not cost the instructions for every other book.
                bt.logging.error(f"BOOK {book_id} | quoting failed: {ex}")
        try:
            self._settle_markouts(validator, state.timestamp)
        except Exception as ex:
            bt.logging.error(f"markout check failed: {ex}")
        self._report(validator, state.timestamp, returns)
        return response

    def _book_return(self, book_id, book, memory, account=None):
        """V3's per-book return, on the given account."""
        account = account if account is not None else self._accounts()[book_id]
        wealth  = account.base_balance.total * memory["mid"] + account.quote_balance.total
        if memory["start_wealth"] is None:
            memory["start_wealth"] = wealth
        start = memory["start_wealth"]
        return wealth / start - 1 if start > 0 else 0.0

    # ── Quoting ──────────────────────────────────────────────────────────────────────────────────────────────

    def _quote_book(self, response, validator, book_id, book, memory, account=None):
        """V3's prices, then one plan per side, executed as KEEP, REPLACE, CANCEL_ONLY or SKIP."""
        account  = account if account is not None else self._accounts()[book_id]
        params   = self._params(book_id)
        best_bid = book.bids[0].price
        best_ask = book.asks[0].price
        mid      = memory["mid"]
        bids = [o for o in account.orders if o.side == OrderDirection.BUY]
        asks = [o for o in account.orders if o.side == OrderDirection.SELL]
        wealth = account.base_balance.total * mid + account.quote_balance.total if math.isfinite(mid) else math.nan
        if not (math.isfinite(mid) and mid > 0 and math.isfinite(wealth) and wealth > 0):
            self._cancel(response, book_id, bids + asks)
            self._count(validator, "non_finite")
            return
        # Every round, so the decayed inputs it reads stay current even while the book is not being quoted.
        pred = self._predict(book, account, memory, params)

        # V3's guards: stop making while the maker fee is above tolerance or the book's volume runs ahead of its
        # pace through the validator's cap (past the cap only cancels are accepted).
        fee_rate = account.fees.maker_fee_rate if account.fees is not None else 0.0
        if fee_rate > self.max_fee_rate or self._over_volume_pace(book_id, account, memory):
            self._cancel(response, book_id, bids)
            self._cancel(response, book_id, asks)
            self._count(validator, "paused")
            return

        # ---- V3's prices, unchanged ----
        decimals = self.price_decimals(book_id)
        tick     = 10 ** -decimals
        fair     = mid * (1 + params["lean"] * pred / 1e4)
        sigma = math.sqrt(memory["variance"] * self.vol_horizon) if memory["variance"] else 0.0
        floor = params["min_half_ticks"] * tick / mid
        half  = max(params["half_spread"], params["vol_mult"] * sigma, floor)
        half  = max(floor, half * self.spread_mult.get((validator, self._class_index(book_id)), 1.0) + fee_rate)
        inventory = self._inventory_gap(account, memory, mid, wealth)
        if not math.isfinite(inventory):
            self._cancel(response, book_id, bids + asks)
            self._count(validator, "non_finite")
            return
        size  = params["size_fraction"] * wealth / mid if params["size_fraction"] > 0 else self.quantity
        shift = (- params["fill_skew"] * self._fill_balance(memory, size * mid, params["fill_confidence"]) * half
                 - self.skew * inventory * half)
        shift = max(-self.max_skew * half, min(self.max_skew * half, shift))
        guard     = max(1.0, params["cross_guard_ticks"]) * tick
        bid_price = min(self._on_grid(fair * (1 + shift - half), decimals, True), round(best_ask - guard, decimals))
        ask_price = max(self._on_grid(fair * (1 + shift + half), decimals, False), round(best_bid + guard, decimals))
        if bid_price <= 0 or bid_price >= ask_price:
            return
        volume_decimals = self.volume_decimals(book_id)
        step            = 10 ** -volume_decimals
        minimum         = round(self.min_order_size(book_id) + step, volume_decimals)
        quantity        = round(max(size, minimum), volume_decimals)
        away   = max(self.requote_frac * half * mid, self.requote_ticks * tick)
        toward = max(self.requote_toward_frac * half * mid, params["requote_toward_ticks"] * tick)

        # ---- one plan per side, checked before anything is sent ----
        context = {"validator": validator, "book_id": book_id, "account": account, "wealth": wealth,
                   "inventory": inventory, "minimum": minimum, "step": step, "volume_decimals": volume_decimals,
                   "away": away, "toward": toward, "best_bid": best_bid, "best_ask": best_ask, "guard": guard,
                   "decimals": decimals}
        plans = [self._plan_side(OrderDirection.BUY, bids, bid_price, quantity, context),
                 self._plan_side(OrderDirection.SELL, asks, ask_price, quantity, context)]
        self._execute(response, book_id, plans, validator)

    def _plan_side(self, direction, orders, target, base_quantity, ctx):
        """One side's decision: (decision, orders to cancel, (quantity, price) to place or None)."""
        validator, inventory = ctx["validator"], ctx["inventory"]
        adds = direction == OrderDirection.BUY
        # Beyond the band, the side that adds to the gap is not quoted at all (V3's hard stop).
        if (inventory >= self.inventory_band) if adds else (inventory <= -self.inventory_band):
            self._count(validator, "hard_stop")
            return (CANCEL_ONLY, orders, None) if orders else (SKIP, [], None)

        # V3's keep test, then the risk limits on the quote that would stay.
        if len(orders) == 1:
            resting = orders[0]
            closer  = resting.price - target if adds else target - resting.price
            if -ctx["away"] - 1e-9 <= closer <= ctx["toward"] + 1e-9 and self._within_limits(direction, resting.quantity, resting.price, ctx):
                return KEEP, [], None

        quantity = self._size(direction, base_quantity, target, orders, ctx)
        if quantity is not None and self._crosses(direction, target, ctx):
            self._count(validator, "cross_blocked")
            quantity = None
        if quantity is not None:
            return REPLACE, orders, (quantity, target)
        if not orders:
            return SKIP, [], None
        # No valid replacement: keep a lone resting quote that is still inside every limit and has not drifted
        # toward the fair price; cancel anything else.
        if len(orders) == 1:
            resting = orders[0]
            closer  = resting.price - target if adds else target - resting.price
            if closer <= ctx["toward"] + 1e-9 and self._within_limits(direction, resting.quantity, resting.price, ctx):
                self._count(validator, "kept_without_replacement")
                return KEEP, [], None
        return CANCEL_ONLY, orders, None

    def _size(self, direction, base_quantity, price, cancelled, ctx):
        """The planned quantity after every limit, on the volume grid, or None when no legal order fits."""
        validator, wealth, inventory = ctx["validator"], ctx["wealth"], ctx["inventory"]
        account, step, minimum = ctx["account"], ctx["step"], ctx["minimum"]
        adds_gap = inventory if direction == OrderDirection.BUY else -inventory
        pressure = max(0.0, adds_gap)
        factor   = max(self.min_inventory_size_factor,
                       1.0 - self.inventory_sizing * pressure / max(self.inventory_band, 1e-9))
        size  = base_quantity * factor
        if factor < 1.0:
            self._count(validator, "shrunk_inventory")
        if self.max_order_fraction > 0:
            cap = self.max_order_fraction * wealth / price
            if cap < size:
                size = cap
                self._count(validator, "shrunk_cap")
        if self.post_fill_band:
            room = (self.inventory_band - adds_gap) * wealth / price
            if room < size:
                size = max(0.0, room)
                self._count(validator, "shrunk_post_fill")
        # Funds: a bid spends QUOTE, an ask BASE, each with this side's own resting orders cancelled first.
        if direction == OrderDirection.BUY:
            free = account.quote_balance.free + sum(o.quantity * o.price for o in cancelled)
            affordable = free / price
        else:
            affordable = account.base_balance.free + sum(o.quantity for o in cancelled)
        if affordable < size:
            size = max(0.0, affordable)
            self._count(validator, "shrunk_funds")
        units    = math.floor(size / step + 1e-9)
        quantity = round(units * step, ctx["volume_decimals"])
        if quantity + 1e-12 < minimum:
            self._count(validator, "below_minimum")
            return None
        return quantity

    def _within_limits(self, direction, quantity, price, ctx):
        """True when a quote of `quantity` at `price` respects the cap and, filled in full, the inventory band."""
        wealth, inventory = ctx["wealth"], ctx["inventory"]
        if self.max_order_fraction > 0 and quantity * price > self.max_order_fraction * wealth * (1 + 1e-9):
            return False
        if self.post_fill_band:
            adds_gap = inventory if direction == OrderDirection.BUY else -inventory
            if adds_gap + quantity * price / wealth > self.inventory_band + 1e-9:
                return False
        return True

    @staticmethod
    def _crosses(direction, price, ctx):
        """The final price, after rounding, skew and guard, would land within the guard of the opposite touch."""
        if direction == OrderDirection.BUY:
            return price > round(ctx["best_ask"] - ctx["guard"], ctx["decimals"]) + 1e-12
        return price < round(ctx["best_bid"] + ctx["guard"], ctx["decimals"]) - 1e-12

    def _execute(self, response, book_id, plans, validator):
        """Sends the plans: each side's cancel ahead of its order, within the per-book instruction limit."""
        instructions = sum((1 if cancel else 0) + (1 if order else 0) for _, cancel, order in plans)
        if instructions > self.max_instructions_per_book:
            self._count(validator, "instruction_limit")
            return
        for (decision, cancel, order), direction in zip(plans, SIDES):
            self._count(validator, decision)
            self._cancel(response, book_id, cancel)
            if order is not None:
                quantity, price = order
                self._place(response, book_id, direction, quantity, price)
                self._count(validator, "placed")
                self._count(None, "placed")

    # ── Statistics ───────────────────────────────────────────────────────────────────────────────────────────

    def _count(self, validator, key, amount=1):
        stats = self.plan_stats.setdefault(validator, {})
        stats[key] = stats.get(key, 0) + amount

    def onOrderRejected(self, event):
        """V3's count, plus the reason and side, for the post-only reject rate."""
        super().onOrderRejected(event)
        key = (getattr(event, "message", None) or "unknown", "buy" if event.side == 0 else "sell")
        self.reject_stats[key] = self.reject_stats.get(key, 0) + 1

    def onTrade(self, event, validator=None):
        """V3's bookkeeping, plus the maker order ids that saw a fill and the maker notional by side and class."""
        super().onTrade(event, validator)
        if event.makerAgentId != self.uid:
            return
        memory = self.memory.get((validator, event.bookId))
        if memory is not None:
            memory.setdefault("filled_orders", {}).setdefault(event.makerOrderId, event.timestamp)
        side = "buy" if event.side == OrderDirection.SELL else "sell"
        stats = self._fill_stats(validator, self._class_index(event.bookId), side)
        stats["notional"] += event.price * event.quantity
        stats["fills"]    += 1

    def _fill_stats(self, validator, index, side):
        return self.fill_stats.setdefault((validator, index, side), {
            "notional": 0.0, "realized": 0.0, "marked": 0.0, "n_marked": 0, "exposure_s": 0.0, "fills": 0,
            "ended": 0, "ended_filled": 0, "time_to_fill": []})

    def _track_orders(self, validator, book_id, account, memory):
        """Resting time per side (for fill intensity), and each order's end as filled or not (fill probability,
        time to the first fill). An order counts as filled if any part of it traded."""
        live   = memory.setdefault("live_orders", {})
        filled = memory.setdefault("filled_orders", {})
        index  = self._class_index(book_id)
        now    = memory["timestamp"]
        elapsed = memory.get("elapsed", 0.0) or 0.0
        current = {}
        for o in account.orders:
            side = "buy" if o.side == OrderDirection.BUY else "sell"
            current[o.id] = (side, getattr(o, "timestamp", None) or now)
        for side in ("buy", "sell"):
            if any(s == side for s, _ in current.values()) and elapsed > 0:
                self._fill_stats(validator, index, side)["exposure_s"] += elapsed
        for order_id, (side, placed) in list(live.items()):
            if order_id in current:
                continue
            stats = self._fill_stats(validator, index, side)
            stats["ended"] += 1
            if order_id in filled:
                stats["ended_filled"] += 1
                stats["time_to_fill"].append(max(0.0, (filled[order_id] - placed) / 1e9))
                del stats["time_to_fill"][:-500]
            filled.pop(order_id, None)
            del live[order_id]
        for order_id, value in current.items():
            live.setdefault(order_id, value)
        # Fills reported for orders this agent never saw resting (filled within one round) still count once.
        for order_id in [i for i in filled if i not in live]:
            if now - filled[order_id] > 60_000_000_000:
                del filled[order_id]

    def _settle_markouts(self, validator, timestamp):
        """V3's markout and spread adaptation, plus the same markout kept per class and side."""
        pending = self.pending_markouts.get(validator) or []
        due = timestamp - self.markout_horizon
        for fill_time, book_id, sign, price, notional, _ in pending:
            memory = self.memory.get((validator, book_id))
            if memory is None or fill_time > memory["timestamp"] or fill_time > due:
                continue
            stats = self._fill_stats(validator, self._class_index(book_id), "buy" if sign > 0 else "sell")
            stats["realized"] += sign * (memory["mid"] - price) / price * 1e4 * notional
            stats["marked"]   += notional
            stats["n_marked"] += 1
        super()._settle_markouts(validator, timestamp)

    def _report(self, validator, timestamp, returns):
        """V3's score card, then a line on the plans and on each class's fills by side."""
        due = self.next_report.get(validator)
        if due is not None and timestamp + self.report_interval < due:
            # A new simulation: its plan and fill statistics start from zero, as V3's do.
            self.plan_stats.pop(validator, None)
            for key in [k for k in self.fill_stats if k[0] == validator]:
                del self.fill_stats[key]
        super()._report(validator, timestamp, returns)
        if due is None or timestamp < due or not returns:
            return
        try:
            bt.logging.info(self._plan_card(validator))
        except Exception as ex:
            bt.logging.error(f"plan card failed: {ex}")

    def _plan_card(self, validator):
        stats = self.plan_stats.get(validator, {})
        decided = " ".join(f"{d} {stats.get(d, 0)}" for d in DECISIONS)
        limits  = " ".join(f"{k} {stats.get(k, 0)}" for k in ("shrunk_inventory", "shrunk_cap", "shrunk_post_fill",
                                                              "shrunk_funds", "below_minimum", "hard_stop",
                                                              "kept_without_replacement", "cross_blocked",
                                                              "instruction_limit", "non_finite", "paused"))
        placed   = self.plan_stats.get(None, {}).get("placed", 0)
        rejected = sum(self.reject_stats.values())
        reasons  = ", ".join(f"{r} {s} {n}" for (r, s), n in sorted(self.reject_stats.items(), key=lambda x: -x[1])[:4])
        parts = [f"PLAN CARD | {decided} | {limits} | placed {placed}, rejected {rejected}"
                 f" ({100 * rejected / placed:.2f}%)" if placed else f"PLAN CARD | {decided} | {limits} | placed 0"]
        if reasons:
            parts.append(f"rejects: {reasons}")
        try:
            names = [cls.name for cls in self.asset_classes()]
        except Exception:
            names = []
        for index, name in enumerate(names):
            sides = []
            for side in ("buy", "sell"):
                s = self.fill_stats.get((validator, index, side))
                if not s:
                    continue
                markout = s["realized"] / s["marked"] if s["marked"] > 0 else 0.0
                hours   = s["exposure_s"] / 3600
                ttf     = sorted(s["time_to_fill"])
                sides.append(f"{side} notional {s['notional']:.0f}, markout {markout:+.2f} bps/{s['n_marked']}, "
                             f"{s['fills'] / hours if hours > 0 else 0:.1f} fills/h resting, "
                             f"P(fill) {s['ended_filled'] / s['ended'] if s['ended'] else 0:.2f} of {s['ended']}, "
                             f"ttf {ttf[len(ttf) // 2] if ttf else 0:.1f}s")
            if sides:
                parts.append(f"{name}: " + "; ".join(sides))
        return " | ".join(parts)



# --------------------------------------------------------------------------------------------------------------------
# MyAgent10_23_1Gpt, embedded unchanged from agents/Agent_10_23/MyAgent10_23_1Gpt.py (23 October).
# --------------------------------------------------------------------------------------------------------------------

# Decayed inputs V3 keeps per book that a non-finite value would poison for good, and their neutral values.
MEMORY_INPUTS = {"variance": None, "ofi": 0.0, "flow": 0.0, "mom5": 0.0, "mom300": 0.0, "pred": 0.0}


class MyAgent10_23_1Gpt(MyAgent10_23_Gpt):
    """MyAgent10_23_Gpt with size-aware keeping, non-finite guards and per-validator statistics."""

    def initialize(self):
        super().initialize()
        self.keep_size_tolerance = max(0.0, float(getattr(self.config, "keep_size_tolerance", 0.3)))
        # (book_id, side, price, quantity) of recent placements -> validator, for attributing rejections.
        self._placed_by = {}

    # ── Non-finite guards ────────────────────────────────────────────────────────────────────────────────────

    def _quote_book(self, response, validator, book_id, book, memory, account=None):
        """10_23's quoting, with the book's decayed inputs checked first and any arithmetic failure turned into a
        cancel of the book's quotes. 10_23 sends its instructions only at the very end, so a failure before that
        has sent nothing yet."""
        for key, neutral in MEMORY_INPUTS.items():
            value = memory.get(key)
            if value is not None and not math.isfinite(value):
                memory[key] = neutral
                self._count(validator, "memory_reset")
        try:
            super()._quote_book(response, validator, book_id, book, memory, account)
        except (ValueError, ArithmeticError) as ex:
            account = account if account is not None else self._accounts()[book_id]
            self._cancel(response, book_id, list(account.orders))
            self._count(validator, "non_finite")
            bt.logging.warning(f"BOOK {book_id} | non-finite quote input ({ex}); quotes cancelled")

    def _predict(self, book, account, memory, params):
        """V3's prediction, or 0 when it is not a finite number."""
        pred = super()._predict(book, account, memory, params)
        if not math.isfinite(pred):
            memory["pred"] = 0.0
            return 0.0
        return pred

    # ── Size-aware keeping ───────────────────────────────────────────────────────────────────────────────────

    def _plan_side(self, direction, orders, target, base_quantity, ctx):
        """10_23's plan, except that a resting quote too large for the plan's current size is replaced, and a
        non-finite target cancels the side."""
        validator = ctx["validator"]
        if not math.isfinite(target) or target <= 0:
            self._count(validator, "non_finite")
            return (CANCEL_ONLY, orders, None) if orders else (SKIP, [], None)
        decision, cancel, order = super()._plan_side(direction, orders, target, base_quantity, ctx)
        if decision != KEEP or self.keep_size_tolerance <= 0 or len(orders) != 1:
            return decision, cancel, order
        resting = orders[0]
        planned = self._size(direction, base_quantity, target, orders, ctx, count=False)
        if planned is None or resting.quantity <= planned * (1 + self.keep_size_tolerance) + 1e-12:
            return decision, cancel, order
        self._count(validator, "replaced_oversize")
        return REPLACE, orders, (planned, target)

    def _size(self, direction, base_quantity, price, cancelled, ctx, count=True):
        """10_23's sizing; with `count` off, the limits it hits are not added to the statistics (used when only
        checking whether a resting quote still fits)."""
        if count:
            return super()._size(direction, base_quantity, price, cancelled, ctx)
        validator = ctx["validator"]
        saved = dict(self.plan_stats.get(validator, {}))
        try:
            return super()._size(direction, base_quantity, price, cancelled, ctx)
        finally:
            self.plan_stats[validator] = saved

    # ── Per-validator placements and rejections ──────────────────────────────────────────────────────────────

    def _execute(self, response, book_id, plans, validator):
        """10_23's execution, remembering which validator placed each order and when, for rejections and for
        orders filled before they are ever seen resting."""
        super()._execute(response, book_id, plans, validator)
        memory = self.memory.get((validator, book_id))
        for (decision, cancel, order), direction in zip(plans, (OrderDirection.BUY, OrderDirection.SELL)):
            if order is None:
                continue
            quantity, price = order
            self._placed_by[(int(book_id), int(direction), round(price, 8), round(quantity, 8))] = validator
            if memory is not None:
                placed = memory.setdefault("placed_at", {})
                placed[(int(direction), round(price, 8))] = memory["timestamp"]
                if len(placed) > 64:
                    for key in sorted(placed, key=placed.get)[:-32]:
                        del placed[key]
        if len(self._placed_by) > 20_000:
            for key in list(self._placed_by)[:10_000]:
                del self._placed_by[key]

    def onOrderRejected(self, event):
        """10_23's counts, plus the rejection booked against the validator that placed the order."""
        super().onOrderRejected(event)
        key = (int(event.bookId) if event.bookId is not None else -1, int(event.side),
               round(event.price or 0.0, 8), round(event.quantity or 0.0, 8))
        validator = self._placed_by.get(key)
        self._count(validator, "rejected")
        reason = getattr(event, "message", None) or "unknown"
        self._count(validator, f"rejected:{reason}:{'buy' if event.side == 0 else 'sell'}")

    def onTrade(self, event, validator=None):
        """10_23's bookkeeping, keeping the maker side and price of each filled order too."""
        super().onTrade(event, validator)
        if event.makerAgentId != self.uid:
            return
        memory = self.memory.get((validator, event.bookId))
        if memory is not None:
            side = int(OrderDirection.SELL if event.side == OrderDirection.BUY else OrderDirection.BUY)
            memory.setdefault("filled_detail", {}).setdefault(event.makerOrderId, (side, round(event.price, 8)))

    def _track_orders(self, validator, book_id, account, memory):
        """10_23's tracking, plus orders that were filled in full before any state showed them resting."""
        live    = memory.setdefault("live_orders", {})
        filled  = memory.setdefault("filled_orders", {})
        detail  = memory.setdefault("filled_detail", {})
        current = {o.id for o in account.orders}
        unseen  = [i for i in filled if i not in live and i not in current]
        index   = self._class_index(book_id)
        placed  = memory.get("placed_at", {})
        for order_id in unseen:
            side, price = detail.get(order_id, (None, None))
            if side is None:
                continue
            stats = self._fill_stats(validator, index, "buy" if side == int(OrderDirection.BUY) else "sell")
            stats["ended"] += 1
            stats["ended_filled"] += 1
            sent = placed.get((side, price))
            if sent is not None:
                stats["time_to_fill"].append(max(0.0, (filled[order_id] - sent) / 1e9))
                del stats["time_to_fill"][:-500]
            del filled[order_id]
            detail.pop(order_id, None)
        super()._track_orders(validator, book_id, account, memory)
        for order_id in [i for i in detail if i not in filled]:
            del detail[order_id]

    def _plan_card(self, validator):
        """10_23's card with this validator's own placements, rejections and reject rate."""
        stats = self.plan_stats.get(validator, {})
        card  = super()._plan_card(validator)
        placed, rejected = stats.get("placed", 0), stats.get("rejected", 0)
        extra = (f"this validator: placed {placed}, rejected {rejected}"
                 + (f" ({100 * rejected / placed:.2f}%)" if placed else "")
                 + f", oversize replaced {stats.get('replaced_oversize', 0)}, memory resets {stats.get('memory_reset', 0)}")
        reasons = sorted(((k.split(":", 1)[1], n) for k, n in stats.items() if k.startswith("rejected:")),
                         key=lambda x: -x[1])[:4]
        if reasons:
            extra += " (" + ", ".join(f"{r} {n}" for r, n in reasons) + ")"
        unattributed = self.plan_stats.get(None, {}).get("rejected", 0)
        if unattributed:
            extra += f", unattributed rejects {unattributed}"
        return card.replace(" | ", " | " + extra + " | ", 1)



# --------------------------------------------------------------------------------------------------------------------
# MyAgent10_23_2Gpt: what this file adds on top of the embedded MyAgent10_23_1Gpt.
# --------------------------------------------------------------------------------------------------------------------

# Per-class parameters 2Gpt adds; `<name>_c<k>` in --params overrides one for class k, as for V3's.
CLASS2_PARAMS = {
    "max_lean": 1.0,      # the prediction's lean on the fair price, at most this many half spreads
    "expiry_s": 60.0,     # resting quotes expire after this many simulation seconds
}
CLASS2_DEFAULTS = {1: {"expiry_s": 300.0}}
SCORE_WINDOW_NS = 3 * 3_600_000_000_000   # the validator's 3 h scoring window
S3_K = 4.0                                # miner-sourced making credit counts up to 4x the background-sourced


class MyAgent10_23_2Gpt(MyAgent10_23_1Gpt):
    """MyAgent10_23_1Gpt with grid-safe prices, fee weighting, 5-minute pacing, per-class expiry, a bounded lean,
    per-book markout widening and a counterparty monitor."""

    def initialize(self):
        super().initialize()
        # The validator nets only `making_fee_weight` (0.005) of each maker fee from making credit, and skill alpha
        # leaves fees out, so the spread carries that share of the fee and only a fee far beyond any spread pauses.
        self.fee_weight      = max(0.0, float(getattr(self.config, "fee_weight", 0.005)))
        self.max_fee_rate    = float(getattr(self.config, "max_fee_rate", 0.02))
        self.grid_nudge      = max(0.0, float(getattr(self.config, "grid_nudge", 1e-6)))
        self.pace_period_s   = max(1.0, float(getattr(self.config, "pace_period_s", 300.0)))
        self.book_adapt_rate = max(0.0, float(getattr(self.config, "book_adapt_rate", 0.02)))
        self.book_mult_max   = max(1.0, float(getattr(self.config, "book_mult_max", 2.0)))
        self.book_markout_alpha = min(1.0, max(0.0, float(getattr(self.config, "book_markout_alpha", 0.1))))
        self.class2_params   = {}
        self.counterparties  = {}

    def _params2(self, book_id):
        """This book's 2Gpt class parameters: `<name>_c<k>` from --params, else the class default, else `<name>`."""
        index  = self._class_index(book_id)
        params = self.class2_params.get(index)
        if params is None:
            defaults = CLASS2_DEFAULTS.get(min(index, max(CLASS2_DEFAULTS)), {}) if index > 0 else {}
            params = {}
            for name, default in CLASS2_PARAMS.items():
                base = float(getattr(self.config, name, default))
                params[name] = float(getattr(self.config, f"{name}_c{index}", defaults.get(name, base)))
            self.class2_params[index] = params
        return params

    # ── Quoting ──────────────────────────────────────────────────────────────────────────────────────────────

    def _quote_book(self, response, validator, book_id, book, memory, account=None):
        """1Gpt's guards (non-finite memory reset, arithmetic failure -> cancel) around 2Gpt's quoting."""
        for key, neutral in MEMORY_INPUTS.items():
            value = memory.get(key)
            if value is not None and not math.isfinite(value):
                memory[key] = neutral
                self._count(validator, "memory_reset")
        try:
            self._quote_book_2(response, validator, book_id, book, memory, account)
        except (ValueError, ArithmeticError) as ex:
            account = account if account is not None else self._accounts()[book_id]
            self._cancel(response, book_id, list(account.orders))
            self._count(validator, "non_finite")
            bt.logging.warning(f"BOOK {book_id} | non-finite quote input ({ex}); quotes cancelled")

    def _quote_book_2(self, response, validator, book_id, book, memory, account=None):
        """10_23_Gpt's quoting with three changes to the price: the maker fee enters the half spread at
        `fee_weight` and pauses the book only above `max_fee_rate`; the half spread is also scaled by the book's
        own markout multiplier; and the prediction's lean is held within `max_lean` half spreads."""
        account  = account if account is not None else self._accounts()[book_id]
        params   = self._params(book_id)
        params2  = self._params2(book_id)
        best_bid = book.bids[0].price
        best_ask = book.asks[0].price
        mid      = memory["mid"]
        bids = [o for o in account.orders if o.side == OrderDirection.BUY]
        asks = [o for o in account.orders if o.side == OrderDirection.SELL]
        wealth = account.base_balance.total * mid + account.quote_balance.total if math.isfinite(mid) else math.nan
        if not (math.isfinite(mid) and mid > 0 and math.isfinite(wealth) and wealth > 0):
            self._cancel(response, book_id, bids + asks)
            self._count(validator, "non_finite")
            return
        pred = self._predict(book, account, memory, params)

        fee_rate = account.fees.maker_fee_rate if account.fees is not None else 0.0
        if fee_rate > self.max_fee_rate or self._over_volume_pace(book_id, account, memory):
            self._cancel(response, book_id, bids)
            self._cancel(response, book_id, asks)
            self._count(validator, "paused")
            return

        decimals = self.price_decimals(book_id)
        tick     = 10 ** -decimals
        sigma = math.sqrt(memory["variance"] * self.vol_horizon) if memory["variance"] else 0.0
        floor = params["min_half_ticks"] * tick / mid
        half  = max(params["half_spread"], params["vol_mult"] * sigma, floor)
        mult  = self.spread_mult.get((validator, self._class_index(book_id)), 1.0) * memory.get("book_mult", 1.0)
        half  = max(floor, half * mult + self.fee_weight * fee_rate)
        # A lean of more than a half spread puts the quote on the far side of the mid, where the market runs it over.
        lean  = params["lean"] * pred / 1e4
        cap   = params2["max_lean"] * half
        if lean > cap or lean < -cap:
            lean = max(-cap, min(cap, lean))
            self._count(validator, "lean_capped")
        fair  = mid * (1 + lean)
        inventory = self._inventory_gap(account, memory, mid, wealth)
        if not math.isfinite(inventory):
            self._cancel(response, book_id, bids + asks)
            self._count(validator, "non_finite")
            return
        size  = params["size_fraction"] * wealth / mid if params["size_fraction"] > 0 else self.quantity
        shift = (- params["fill_skew"] * self._fill_balance(memory, size * mid, params["fill_confidence"]) * half
                 - self.skew * inventory * half)
        shift = max(-self.max_skew * half, min(self.max_skew * half, shift))
        # The lean and the skew together stay within max(max_skew, max_lean) half spreads, as in IOV, so their sum
        # cannot carry a quote across the mid either; the skew gives way, the lean having been bounded already.
        total = max(self.max_skew, params2["max_lean"]) * half
        if lean + shift > total or lean + shift < -total:
            shift = max(-total - lean, min(total - lean, shift))
        guard     = max(1.0, params["cross_guard_ticks"]) * tick
        bid_price = min(self._on_grid(fair * (1 + shift - half), decimals, True), round(best_ask - guard, decimals))
        ask_price = max(self._on_grid(fair * (1 + shift + half), decimals, False), round(best_bid + guard, decimals))
        if bid_price <= 0 or bid_price >= ask_price:
            return
        volume_decimals = self.volume_decimals(book_id)
        step            = 10 ** -volume_decimals
        minimum         = round(self.min_order_size(book_id) + step, volume_decimals)
        quantity        = round(max(size, minimum), volume_decimals)
        away   = max(self.requote_frac * half * mid, self.requote_ticks * tick)
        toward = max(self.requote_toward_frac * half * mid, params["requote_toward_ticks"] * tick)
        context = {"validator": validator, "book_id": book_id, "account": account, "wealth": wealth,
                   "inventory": inventory, "minimum": minimum, "step": step, "volume_decimals": volume_decimals,
                   "away": away, "toward": toward, "best_bid": best_bid, "best_ask": best_ask, "guard": guard,
                   "decimals": decimals}
        plans = [self._plan_side(OrderDirection.BUY, bids, bid_price, quantity, context),
                 self._plan_side(OrderDirection.SELL, asks, ask_price, quantity, context)]
        self._execute(response, book_id, plans, validator)

    def _place(self, response, book_id, direction, quantity, price):
        """A post-only GTT order whose price is nudged `grid_nudge` of a tick up before it is sent, and whose expiry
        is the class's `expiry_s`.

        The engine fits a price to the grid by truncation (OrderPlacementValidator.cpp, util::round), and about half
        of all two-decimal prices are stored as a double a hair below their decimal (299.71 is 299.70999...), so
        without the nudge they land one tick low: bids one tick further from the market, asks one tick closer to it
        (and into post-only refusals). The nudge is far below a tick, so it never moves a price onto the next one.
        """
        tick = 10 ** -self.price_decimals(book_id)
        response.limit_order(
            book_id=book_id,
            direction=direction,
            quantity=quantity,
            price=price + self.grid_nudge * tick,
            stp=STP.CANCEL_BOTH,
            postOnly=True,
            timeInForce=TimeInForce.GTT,
            expiryPeriod=int(self._params2(book_id)["expiry_s"] * 1e9),
        )

    def _over_volume_pace(self, book_id, account, memory):
        """V3's pacing through the validator's daily volume cap, in `pace_period_s` periods instead of hours.

        Each period may use `pace_burst` times its even share of the cap. With hourly periods the share went in
        the first few minutes of each hour and the book then sat dark for the rest of it, and almost every
        refused post-only order fell in those opening minutes; five-minute periods keep the book quoting."""
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        if wealth <= 0:
            return False
        cap     = self.turnover_cap * wealth
        period  = int(self.pace_period_s * 1e9)
        index   = memory["timestamp"] // period
        per_day = 86_400 / self.pace_period_s
        volume  = memory.setdefault("pvolume", {})
        for old in [i for i in volume if i <= index - per_day]:
            del volume[old]
        day = sum(volume.values())
        return ((account.traded_volume or 0.0) >= self.turnover_margin * cap
                or day >= self.turnover_margin * cap
                or volume.get(index, 0.0) >= self.pace_burst * cap / per_day)

    # ── Fills: pacing volume, counterparty mix, per-book markout ───────────────────────────────────────────────

    def onTrade(self, event, validator=None):
        """1Gpt's bookkeeping, plus this agent's volume by pacing period and, for each maker fill, whether the
        taker was the background market (negative agent ids) or another miner."""
        super().onTrade(event, validator)
        if self.uid not in (event.makerAgentId, event.takerAgentId):
            return
        memory   = self.memory.get((validator, event.bookId))
        notional = event.price * event.quantity
        if memory is not None:
            volume = memory.setdefault("pvolume", {})
            index  = event.timestamp // int(self.pace_period_s * 1e9)
            volume[index] = volume.get(index, 0.0) + notional
        if event.makerAgentId == self.uid:
            fills = self.counterparties.setdefault((validator, self._class_index(event.bookId)), [])
            fills.append((event.timestamp, notional, event.takerAgentId < 0))

    def _settle_markouts(self, validator, timestamp):
        """10_23_Gpt's markouts, plus each book's own multiplier: a book whose fills mark out below
        `markout_target_bps` widens, by up to `book_mult_max`, at `book_adapt_rate`, and relaxes back toward 1
        when they recover. Markout quality is one ratio over all of an agent's books, so one book the market
        runs over costs credit on every other book; only widening is done per book, narrowing stays per class."""
        if self.book_adapt_rate > 0:
            due = timestamp - self.markout_horizon
            for fill_time, book_id, sign, price, notional, _ in self.pending_markouts.get(validator) or []:
                memory = self.memory.get((validator, book_id))
                if memory is None or fill_time > memory["timestamp"] or fill_time > due:
                    continue
                realized = sign * (memory["mid"] - price) / price * 1e4
                average  = memory.get("book_markout", self.markout_target_bps)
                memory["book_markout"] = average + self.book_markout_alpha * (realized - average)
                step = max(-1.0, min(1.0, (self.markout_target_bps - memory["book_markout"]) / self.adapt_scale_bps))
                mult = memory.get("book_mult", 1.0) * math.exp(self.book_adapt_rate * step)
                memory["book_mult"] = max(1.0, min(self.book_mult_max, mult))
        super()._settle_markouts(validator, timestamp)

    # ── Score card ───────────────────────────────────────────────────────────────────────────────────────────

    def _report(self, validator, timestamp, returns):
        """1Gpt's cards; a new simulation also clears the counterparty record."""
        due = self.next_report.get(validator)
        if due is not None and timestamp + self.report_interval < due:
            for key in [k for k in self.counterparties if k[0] == validator]:
                del self.counterparties[key]
        super()._report(validator, timestamp, returns)

    def _counterparty_mix(self, validator, index, now):
        """Maker notional over the validator's 3 h window by taker kind, and the S3 factor it implies."""
        fills = self.counterparties.get((validator, index), [])
        cutoff = now - SCORE_WINDOW_NS
        while fills and fills[0][0] < cutoff:
            fills.pop(0)
        background = sum(n for _, n, bg in fills if bg)
        miners     = sum(n for _, n, bg in fills if not bg)
        total      = background + miners
        s3 = (background + min(miners, S3_K * background)) / total if total > 0 else 1.0
        return background, miners, s3

    def _plan_card(self, validator):
        """1Gpt's card, with per class the background share of maker notional over 3 h and the implied S3 factor,
        plus the 2Gpt adjustments made (lean caps, widest book multiplier)."""
        card  = super()._plan_card(validator)
        stats = self.plan_stats.get(validator, {})
        now   = max((m["timestamp"] for (v, _), m in self.memory.items() if v == validator), default=0)
        parts = []
        try:
            names = [cls.name for cls in self.asset_classes()]
        except Exception:
            names = []
        for index, name in enumerate(names):
            background, miners, s3 = self._counterparty_mix(validator, index, now)
            if background + miners > 0:
                parts.append(f"{name}: background {100 * background / (background + miners):.0f}% of "
                             f"{background + miners:.0f} maker notional, S3 ~{s3:.2f}")
        mults = [m.get("book_mult", 1.0) for (v, _), m in self.memory.items() if v == validator]
        widened = sum(x > 1.0 + 1e-9 for x in mults)
        parts.append(f"lean capped {stats.get('lean_capped', 0)}, books widened {widened}"
                     + (f" (max x{max(mults):.2f})" if mults else ""))
        return card + " | COUNTERPARTIES " + " | ".join(parts)


if __name__ == "__main__":
    launch(MyAgent10_23_2Gpt)
