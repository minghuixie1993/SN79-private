# SPDX-License-Identifier: MIT
"""
Predictive two-sided market maker that tracks its own validator score legs book by book.

What the score pays (taos/im/validator/debeta.py, reward.py, at the shipped defaults); the trading pool is paid
half on each leg, each in proportion to the agent's share of the field within an asset class (96 books weighted
0.75, 32 books 0.25):
  - making: per book 2 x min(buy capture, sell capture) against a centered trade mid, summed over the books, times
    the uid's markout quality: realized / captured over ALL its maker fills on all books, each fill's realized
    spread 20 s later clamped to +/- its captured spread, the ratio clamped to 0..1. One ratio for the whole uid,
    so a book the market runs over costs credit on every other book too.
  - skill: per book alpha = sum(inventory x price change) - mean inventory x drift over the 3 h window, from the
    book's trade prints; only books whose |alpha| clears 2.3 bps of the agent's filled notional there count. The
    agent qualifies in a class when the mean of those alphas is positive on at least 15 of the 96 or 5 of the 32
    books, and is then paid in proportion to their sum, so a counted book with negative alpha subtracts. Fees
    are not in the alpha, and only 0.5% of the maker fee is netted from making credit.
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
    `min_half_ticks` ticks, times the class's and the book's markout multipliers, plus the maker fee (a rebate
    tightens). On class 0 (spread ~1 bps, 20 s move ~10 bps) a quote near the touch is filled only by the moves
    that keep going: replaying the run, quotes 5 bps out marked out at -2.3 bps and kept none of their making
    credit, while 6 bps / 1.3 sigma with lean 2 kept 44% of it at +3.1 bps a fill. Class 1 (spread ~10 bps,
    20 s move ~3 bps) is the reverse: the touch itself holds its spread.
  - the markout multipliers are the agent's own check on that: every maker fill is marked against the mid
    `markout_horizon` later; the class's spread widens while its fills mark out below `markout_target_bps` and
    narrows while above, within `adapt_min`..`adapt_max`, and a book whose own fills mark out below the target
    widens further, by up to `book_mult_max`, at the faster `book_adapt_rate`, so one bad book stops dragging
    down the quality ratio every book's credit is multiplied by,
  - both quotes shift away from the side the agent is already heavy on (`skew` half spreads per unit of
    inventory gap: the BASE held beyond the book's initial BASE, as a share of wealth), and away from the side
    whose captured spread has run ahead (`fill_skew`, a fraction of the half spread), since only the smaller
    side's capture is credited. The capture is decayed over `score_window`, an exponential of the same mean age
    as the validator's flat 3 h window, so it balances what the validator actually counts. That shift is capped
    at `max_skew` of the half spread, and once the inventory gap leaves +/- `inventory_band` only the side that
    reduces it is quoted. The target is the initial holding, not half of wealth: initial allocations differ by
    book (about 6-50% of wealth in BASE), and a 50% target kept buying on every book that started light,
  - on a book whose own alpha (the validator's formula, replayed from the book's prints over `score_window`) is
    negative beyond the `skill_hurdle_bps` hurdle, the inventory skew is `negative_alpha_skew` times stronger:
    alpha is the inventory's deviation from its mean times the price change, so holding less there shrinks a
    loss the skill leg would otherwise subtract,
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

The score card prints, per class, the markout quality as the validator computes it (realized clamped per fill)
and the skill gate: books whose alpha clears the hurdle against the class's bar (`skill_bar_share` of its books),
how many of them are positive, and their net alpha.

Parameters can differ by asset class: `<name>_c<k>` overrides `<name>` on the books of the k-th class
(0 = books 0-95, 1 = books 96-127 on the multi-asset layout), e.g. `half_spread_c1=0.001`. Any later class starts
from class 1's settings.

The validator turns response time into order delay (10 ms up to 1 s, exponential in the share of its timeout), so
the garbage collector is told to run rarely: its full passes over a parsed state were most of the slow responses.

Local test (from the agents directory, with the proxy and simulator running):
    python MyAgentVValidator.py --port 8888 --agent_id 0 --params lean_c0=2 half_spread_c0=0.0006
"""
import gc
import math

import bittensor as bt
from taos.common.agents import launch
from taos.im.agents import FinanceAgent
from taos.im.protocol.models import OrderDirection, STP, TimeInForce

HOUR = 3_600_000_000_000
DAY_HOURS = 24
# The validator's skill bar never falls below four books (reward.SKILL_MIN_BOOKS_FLOOR).
SKILL_MIN_BOOKS = 4

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
    "size_fraction":        0.006,
    "min_half_ticks":       1.5,
    "requote_toward_ticks": 0.0,
}
# Class defaults by index; classes past the last one listed start from its settings.
CLASS_DEFAULTS = {1: {"half_spread": 0.0005, "vol_mult": 1.5, "lean": 1.0, "coef_fund": 0.0035, "coef_ofi": 0.0,
                      "coef_flow": 8.0, "coef_imb5": 0.0, "coef_imb1": 0.27, "coef_mom5": -0.2, "coef_mom300": 0.025,
                      "size_fraction": 0.012, "requote_toward_ticks": 1.0}}


class MyAgentVValidator(FinanceAgent):
    """Predictive passive market maker quoting every book, steered by its own replay of the score legs."""

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
        # Seconds; an exponential of the same mean age as the validator's flat 3 h scoring window.
        self.score_window      = float(getattr(self.config, "score_window", 5400.0))
        self.skill_hurdle_bps  = float(getattr(self.config, "skill_hurdle_bps", 2.3))
        self.skill_bar_share   = float(getattr(self.config, "skill_bar_share", 20 / 128))
        self.negative_alpha_skew = float(getattr(self.config, "negative_alpha_skew", 2.0))
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
        self.adapt_rate         = float(getattr(self.config, "adapt_rate", 0.002))
        self.adapt_scale_bps    = float(getattr(self.config, "adapt_scale_bps", 10.0))
        self.adapt_min          = float(getattr(self.config, "adapt_min", 0.7))
        self.adapt_max          = float(getattr(self.config, "adapt_max", 2.0))
        # A book has far fewer fills than its class, so its own multiplier moves faster, and only ever widens.
        self.book_adapt_rate    = float(getattr(self.config, "book_adapt_rate", 0.02))
        self.book_mult_max      = float(getattr(self.config, "book_mult_max", 2.0))
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
        they cost (negative fees are rebates) and the spread they captured against the mid, and queues each
        passive fill for its markout.

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
            sign = 1 if side == OrderDirection.BUY else -1
            memory["capture"][side] += sign * (memory["mid"] - event.price) * event.quantity
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
                self._track_alpha(memory, book, self.accounts[book_id])
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
        decayed captured spread by side, and the decayed predictive inputs (order-flow imbalance, taker flow and
        momentum).

        A timestamp earlier than the last one seen means the validator started a new simulation, so the
        memory starts over.
        """
        best_bid, best_ask = book.bids[0], book.asks[0]
        mid    = (best_bid.price + best_ask.price) / 2
        memory = self.memory.get((validator, book_id))
        if memory is None or timestamp < memory["timestamp"]:
            memory = {"timestamp": timestamp, "mid": mid, "variance": None, "start_wealth": None, "start_base": None,
                      "volume": {}, "capture": {OrderDirection.BUY: 0.0, OrderDirection.SELL: 0.0}, "mult": 1.0,
                      "l1": None, "ofi": 0.0, "flow": 0.0, "mom5": 0.0, "mom300": 0.0, "pred": 0.0,
                      "alpha": {"mtm": 0.0, "invsum": 0.0, "n": 0.0, "drift": 0.0, "notional": 0.0,
                                "price": None, "inventory": None}}
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
            decay = math.exp(-elapsed / self.score_window)
            for side in memory["capture"]:
                memory["capture"][side] *= decay
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
    def _prints(book):
        """The book's trades since the previous state, in order, as (price, quantity, side, maker id, taker id).

        Read from the raw events when the book is lazily parsed, so the orders and cancellations it carries are
        never built into models just to be skipped."""
        raw = getattr(book, "_raw", None)
        if isinstance(raw, dict):
            return [(e["p"], e["q"], e["s"], e.get("Ma"), e.get("Ta")) for e in raw.get("e") or [] if e.get("y") == "t"]
        return [(e.price, e.quantity, e.side, e.maker_agent_id, e.taker_agent_id)
                for e in book.events or [] if e.type == "t"]

    def _taker_flow(self, book):
        """Signed taker BASE traded on the book since the previous state: buys positive, sells negative."""
        return sum((q if s == 0 else -q) for _, q, s, _, _ in self._prints(book))

    def _book_return(self, book_id, book, memory):
        """Return on this book's wealth (marked at mid) since the agent first saw it in this simulation."""
        account = self.accounts[book_id]
        wealth  = account.base_balance.total * memory["mid"] + account.quote_balance.total
        if memory["start_wealth"] is None:
            memory["start_wealth"] = wealth
        start = memory["start_wealth"]
        return wealth / start - 1 if start > 0 else 0.0

    def _track_alpha(self, memory, book, account):
        """Replays the validator's skill accumulators (debeta.accumulate_book_mtm) over this state's prints.

        At each print the position held before it earns the price change since the previous print, the drift
        sums those changes, the agent's own fill then moves its position, and the position after the print is
        added to the inventory sum. The position is the BASE beyond the book's initial BASE, re-read from the
        account after every state so a missed event cannot leave it wrong for long. Sums decay over
        `score_window` where the validator drops prints older than 3 h."""
        alpha = memory["alpha"]
        if memory["elapsed"] > 0:
            decay = math.exp(-memory["elapsed"] / self.score_window)
            for key in ("mtm", "invsum", "n", "drift", "notional"):
                alpha[key] *= decay
        current = account.base_balance.total - self._base_initial(account, memory)
        position = current if alpha["inventory"] is None else alpha["inventory"]
        for price, quantity, side, maker, taker in self._prints(book):
            if alpha["price"] is not None and price != alpha["price"]:
                change = price - alpha["price"]
                alpha["mtm"]   += position * change
                alpha["drift"] += change
            alpha["price"] = price
            # side 0: the taker bought from the maker; side 1: the maker bought from the taker
            buyer, seller = (taker, maker) if side == 0 else (maker, taker)
            if buyer == self.uid and seller != self.uid:
                position += quantity
            elif seller == self.uid and buyer != self.uid:
                position -= quantity
            if self.uid in (buyer, seller):
                alpha["notional"] += price * quantity
            alpha["invsum"] += position
            alpha["n"]      += 1
        alpha["inventory"] = current

    def _alpha(self, memory):
        """This book's alpha as the validator computes it, and the hurdle it must clear to count, both in QUOTE."""
        alpha = memory["alpha"]
        if alpha["n"] <= 0:
            return 0.0, 0.0
        value = alpha["mtm"] - alpha["invsum"] / alpha["n"] * alpha["drift"]
        return value, self.skill_hurdle_bps / 1e4 * alpha["notional"]

    def _settle_markouts(self, validator, timestamp):
        """Marks every maker fill older than `markout_horizon` against its book's mid now, and moves the class's
        and the book's spread multipliers toward the width at which fills mark out at `markout_target_bps`.

        Also keeps, per class, the notional-weighted markout (realized), the edge against the mid at the fill
        (captured), and the realized spread clamped to +/- the captured one as the validator clamps it, whose
        ratio to captured stands in for the validator's markout quality."""
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
            stats = self.markout_stats.setdefault(
                (validator, index), {"realized": 0.0, "clamped": 0.0, "captured": 0.0, "notional": 0.0, "n": 0})
            stats["realized"] += realized * notional
            stats["clamped"]  += max(-abs(captured), min(abs(captured), realized)) * notional
            stats["captured"] += captured * notional
            stats["notional"] += notional
            stats["n"] += 1
            step = max(-1.0, min(1.0, (self.markout_target_bps - realized) / self.adapt_scale_bps))
            mult = self.spread_mult.get((validator, index), 1.0) * math.exp(self.adapt_rate * step)
            self.spread_mult[(validator, index)] = max(self.adapt_min, min(self.adapt_max, mult))
            memory["mult"] = max(1.0, min(self.book_mult_max, memory["mult"] * math.exp(self.book_adapt_rate * step)))
        self.pending_markouts[validator] = keep

    def _skill_gate(self, validator, books):
        """Over `books`: how many alphas clear their hurdle, how many of those are positive, and their sum."""
        counted = positive = 0
        net = 0.0
        for book_id in books:
            memory = self.memory.get((validator, book_id))
            if memory is None:
                continue
            value, hurdle = self._alpha(memory)
            if hurdle > 0 and abs(value) >= hurdle:
                counted += 1
                positive += value > 0
                net += value
        return counted, positive, net

    def _report(self, validator, timestamp, returns):
        """Prints a rough local stand-in for the validator score; the real score only exists on a network.

        The skill gate is the agent's replay of the validator's per-book alpha, so treat it as a guide; for the
        real legs replay a recorded run through agents/eval/score_run.py.
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
        classes_line = []
        for index, cls in enumerate(classes):
            parts = []
            s = self.markout_stats.get((validator, index))
            if s and s["notional"] > 0:
                quality = max(0.0, min(1.0, s["clamped"] / s["captured"])) if s["captured"] > 0 else 0.0
                parts.append(f"markout {s['realized'] / s['notional']:+.2f} bps over {s['n']} fills, "
                             f"quality {quality:.2f}, spread x{self.spread_mult.get((validator, index), 1.0):.2f}")
            bar = max(SKILL_MIN_BOOKS, math.ceil(self.skill_bar_share * len(cls.books)))
            counted, positive, net = self._skill_gate(validator, cls.books)
            parts.append(f"skill books {counted}/{bar} ({positive} positive), net alpha {net:+.2f}")
            classes_line.append(f"{cls.name}: " + ", ".join(parts))
        stats = self._stats(validator)
        bt.logging.info(
            f"SCORE CARD | avg return {100 * sum(returns.values()) / len(returns):+.4f}% | {books} | "
            f"maker fills buy {stats[OrderDirection.BUY]} / sell {stats[OrderDirection.SELL]} | "
            f"maker fees {stats['fees']:+.4f} | rejected {self._stats(None).get('rejected', 0)}"
            + (" | " + " | ".join(classes_line) if classes_line else "")
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

    @staticmethod
    def _base_initial(account, memory):
        """The book's initial BASE: the account's `initial` balance, or (exchange mode, where an account has none)
        the balance first seen in this simulation."""
        initial = getattr(account.base_balance, "initial", None)
        if initial is None:
            if memory["start_base"] is None:
                memory["start_base"] = account.base_balance.total
            initial = memory["start_base"]
        return initial

    def _inventory_gap(self, account, memory, mid, wealth):
        """BASE held beyond the book's initial BASE, valued at mid as a share of wealth; positive when long."""
        initial = self._base_initial(account, memory)
        return (account.base_balance.total - initial) * mid / wealth if wealth > 0 else 0.0

    def _capture_balance(self, memory):
        """(buy - sell) / (|buy| + |sell|) of this book's decayed captured spread; positive when buys ran ahead."""
        buys, sells = memory["capture"][OrderDirection.BUY], memory["capture"][OrderDirection.SELL]
        total = abs(buys) + abs(sells)
        return (buys - sells) / total if total > 0 else 0.0

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
        # edge on a coarse grid, scaled by the class's and the book's markout multipliers; the maker fee widens
        # it and a rebate narrows it, never below the tick floor.
        sigma = math.sqrt(memory["variance"] * self.vol_horizon) if memory["variance"] else 0.0
        floor = params["min_half_ticks"] * tick / mid
        half  = max(params["half_spread"], params["vol_mult"] * sigma, floor)
        mult  = self.spread_mult.get((validator, self._class_index(book_id)), 1.0) * memory["mult"]
        half  = max(floor, half * mult + fee_rate)

        # Inventory beyond the book's initial BASE, as a share of wealth: 0 is balanced, above it means long.
        wealth    = account.base_balance.total * mid + account.quote_balance.total
        inventory = self._inventory_gap(account, memory, mid, wealth)
        # A book whose alpha is negative beyond the hurdle subtracts from the skill leg's net alpha; holding less
        # inventory there shrinks that alpha toward the hurdle, under which it is not counted at all.
        value, hurdle = self._alpha(memory)
        skew = self.skew * (self.negative_alpha_skew if hurdle > 0 and value <= -hurdle else 1.0)
        # Lean away from the inventory and from the side whose capture ran ahead (making credit counts the smaller
        # side), by at most `max_skew` of the half spread, so the bid stays below the fair price and the ask above.
        shift = (- params["fill_skew"] * self._capture_balance(memory) * half
                 - skew * inventory * half)
        shift = max(-self.max_skew * half, min(self.max_skew * half, shift))

        # Never cross the book: a passive quote must stay on its own side of the spread.
        bid_price = min(self._on_grid(fair * (1 + shift - half), decimals, True), round(best_ask - tick, decimals))
        ask_price = max(self._on_grid(fair * (1 + shift + half), decimals, False), round(best_bid + tick, decimals))
        if bid_price <= 0 or bid_price >= ask_price:
            return

        # One volume increment above the minimum: the engine rounds the float it receives down onto its
        # grid, so an order of exactly min_order_size (e.g. 2.5364 -> 2.53639999...) arrives as 2.5363.
        volume_decimals = self.volume_decimals(book_id)
        min_quantity    = self.min_order_size(book_id) + 10 ** -volume_decimals
        size            = params["size_fraction"] * wealth / mid if params["size_fraction"] > 0 else self.quantity
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


if __name__ == "__main__":
    launch(MyAgentVValidator)
