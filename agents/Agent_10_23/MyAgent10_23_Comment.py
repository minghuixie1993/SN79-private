# SPDX-License-Identifier: MIT
"""
Inventory-skewed two-sided market maker, written as a starting point for local proxy testing.

On every book it keeps one passive bid and one passive ask around a fair price:
  - the fair price is the mid moved by `lean` times a predicted 20 s move, held within `max_lean` half spreads:
    MyAgentV2's per-class linear blend (`coef_<input>`) of the gap to the book's published fundamental,
    order-flow imbalance at the touch (`ofi`), signed taker flow (`flow`), the top-`imbalance_depth` and
    top-of-book volume imbalance with the agent's own quotes taken out, and the mid's momentum, so quotes step
    out of the way of flow that is about to move the price. Only the lean comes from V2, not its width: V2's
    1.3 sigma and 6 bps quotes kept more of their spread (markout quality ~0.43 against ~0.33 here on the
    23 October network run) but sat away from the touch, where the background flow trades, and lost more of
    their making credit to the S3 tether than they gained. The older leans, toward the heavier side of the book
    (`imbalance_skew`, a fraction of the half spread) and toward the fundamental measured against its own
    `gap_window` average (`fundamental_weight`), are both inputs of the prediction and are off by default,
  - quotes sit a half spread either side of it: the largest of `half_spread`, `vol_mult` times the
    mid's volatility over the validator's 20 s markout horizon (`vol_horizon`) and `min_half_ticks`
    ticks, plus `fee_weight` of the maker fee rate (a rebate, a negative rate, narrows it, never inside the tick
    floor); prices are rounded away from the fair price
    onto the tick grid, so rounding never gives up edge (a tick is about 3 bps on the ~30 books),
  - each maker fill is marked against the mid `markout_horizon` (20 s) later, clamped to +/- the spread it captured
    as the validator clamps it, and a book whose clamped / captured ratio over `markout_window` stays below
    `book_quality_target` quotes up to `book_mult_max` times wider, so one book the market runs over stops
    lowering the markout quality that multiplies the making credit of every book; the score card prints that
    ratio per class, an estimate of the validator's markout quality,
  - both quotes shift away from the side the agent is already heavy on (`skew`), and away from the side
    whose fills have run ahead over the last `fill_window` (`fill_skew`, a fraction of the half spread);
    the combined shift is capped at `max_skew` of the half spread so neither quote ever reaches the fair
    price, and once the inventory (the BASE held beyond the book's initial BASE, as a share of wealth) leaves
    +/- `inventory_band` only the side that reduces it is quoted. The target is the initial holding, not half of
    wealth: initial allocations differ by book, all of them below half, and a half-of-wealth target leaned every
    book toward buying,
  - a quote never comes within `cross_guard_ticks` ticks of the opposite touch (3 on class 0, where the spread is
    ~3 ticks, 1 on class 1), so the touch moving during the response delay does not turn it into a refused
    post-only order and leave that side of the book unquoted,
  - each quote is sized so the book's fills spend the validator's daily volume allowance at its average rate:
    the allowance per second over the book's measured maker fill rate while quoting (`budget_target` of it),
    never below the book's minimum order nor above `size_fraction` of its wealth (or `quantity` BASE when that
    is 0); a book with no fill history yet starts at the minimum. At `size_fraction`'s 300 QUOTE quotes the books
    filled ~13 times faster than the allowance, so the pacing kept them dark ~90% of the time and the allowance went
    in bursts; smaller quotes stay in the book for longer at the same total volume (`budget_sizing=0` restores
    fixed sizes). No order exceeds `max_order_fraction` of the book's wealth, and a book whose minimum order is
    larger than that is not quoted rather than the minimum overriding the cap,
  - a resting quote that has drifted further from the fair price than its target is kept while within
    `requote_frac` of the half spread (never less than `requote_ticks` ticks), but one that has drifted
    toward the fair price is replaced once past `requote_toward_frac` of it, since that is the quote the
    market runs over; quotes expire after `expiry_period` so a forgotten one cannot be picked off for long. A
    replacement is sized to the funds free before anything is cancelled, so a quote whose replacement cannot be
    placed is kept rather than the side left empty, unless it is the one drifting toward the fair price,
  - quoting stops on a book while its maker fee rate exceeds `max_fee_rate`, while its own fills run ahead
    of an even pace through the validator's daily volume cap (`turnover_cap` x miner wealth: a token bucket that
    refills at `turnover_margin` of the cap per day and holds `pace_bucket` seconds of it, or with `pace_tokens=0`
    at most `pace_burst` times an even share in any `pace_period`), and once its traded volume reaches
    `turnover_margin` of that cap, past which the validator drops every instruction except cancels,
  - optionally (`take_edge_bps` > 0, off by default) a book also takes: when the predicted 20 s move beats half the
    book's spread by `take_edge_bps`, it sends an immediate-or-cancel order at the touch for one quote size, at most
    once per `take_cooldown` seconds and within `take_share` of the pacing period's volume. The skill half pays any
    miner for the drift-stripped alpha of its inventory, takers included, and on 20 October the team said takers
    held just over half of it; the alpha is the inventory marked through later prints, so the fill price enters only
    as the bounce back through the spread, and fees not at all.

Parameters can differ by asset class: `<name>_c<k>` overrides `<name>` on the books of the k-th class
(0 = books 0-95, 1 = books 96-127 on the multi-asset layout), e.g. `half_spread_c1=0.001`. Class 1 runs on its
own coefficients of the prediction (MyAgentV2's fit there); any later class starts from class 1's settings.
The raw gap to the fundamental carries a per-book offset that lasts the whole run: on the 23 October network
layout its per-book average was 29 bps (median, up to 84) on class 0, as large as its variation, enough at the
default coefficient and lean to tilt 23 of 48 books by half the lean cap or more for good. An earlier 15-hour
local replay found the gap measured against its own recent average kept nearly all of its 20 s predictive power
(+0.118) and lost the offset; `center_fundamental=1` predicts from that centered gap instead of the raw one.

Making credit is 2 x min(buy, sell) of the spread captured on each book, net of `making_fee_weight` (0.005 in
0.6.4) of each maker fee, scaled by how much of
it still holds 20 s later against the open market's prices (FAQ question 2). Since 0.6.2 both halves are paid in
proportion to what each account earned (making credit, and net alpha over the books it filled), not by rank. Volume
already runs at the daily turnover cap's pace (10 x miner wealth per book), so a bigger quote only reaches the cap
sooner; the share grows with edge per unit of volume instead. The skill half's alpha leaves fees out altogether, so
the dynamic maker fee (up to +/-1.5% as miners' passive share moves off 0.4) costs the score almost nothing and
only drains the simulated wealth, which no leg scores and every new simulation resets. `fee_weight` therefore
follows the validator's `making_fee_weight` rather than the full fee, and `max_fee_rate` sits above the policy's
1.5% ceiling: stopping on a costly book would give up its making credit and its skill coverage for nothing. The fair-price leans and the
volatility spread protect that markout, the inventory and fill-balance controls keep each book two-sided,
and the skill half strips out the drift on whatever inventory is left.

Local test (from the agents directory, with the proxy and simulator running):
    python MyAgent10_23_Comment.py --port 8888 --agent_id 0 --params half_spread=0.0005 skew=0.002
"""
import gc
import math

import bittensor as bt
from taos.common.agents import launch
from taos.im.agents import FinanceAgent
from taos.im.protocol.models import OrderDirection, STP, TimeInForce

HOUR = 3_600_000_000_000
DAY = 24 * HOUR
# The validator scores skill over the last three simulated hours of fills, scaled down once the books filled in that
# window fall below (1 - 0.375) of the field's books.
SKILL_WINDOW = 3 * HOUR
SKILL_COVERAGE = 1 - 0.375
# A book's edge counts toward skill once it clears this share of the agent's own filled notional on the book, and the
# skill half pays only accounts with such books on at least SKILL_MIN_BOOKS_SHARE of an asset class's books (never
# fewer than four). Since 0.6.3 every asset class is scored and paid as its own pool, so both bars and the coverage
# rule apply per class: 15 of the 96 books and 5 of the 32 on the multi-asset layout.
SKILL_HURDLE = 2.3e-4
SKILL_MIN_BOOKS_SHARE = 20 / 128
SKILL_MIN_BOOKS_FLOOR = 4
# Taker key for every non-miner counterparty, as in the validator's counterparty-diversity discount, and how many of
# the largest miner takers that discount reads (`scoring.debeta.p11_topk`, 10 since 0.6.2).
MARKET_FLOW = -1
P11_TOPK = 10
# Making credit sourced from other miners counts only up to this multiple of the credit sourced from the market
# (`scoring.debeta.s3_k`, 4 since 0.6.3).
S3_K = 4.0

# Per-class strategy parameters and their defaults; `<name>_c<k>` in --params overrides one for class k.
CLASS_PARAMS = {
    "half_spread":        0.0005,
    "vol_mult":           0.5,
    # The older single-input leans; off by default, since the predicted move below carries both inputs.
    "imbalance_skew":     0.0,
    "fundamental_weight": 0.0,
    # The predicted 20 s move in bps (MyAgentV2's fit), and how far it moves the fair price: `lean` times the
    # prediction, held within `max_lean` half spreads.
    "lean":               2.0,
    "max_lean":           1.0,
    "coef_fund":          0.04,
    "coef_ofi":           1.5,
    "coef_flow":          0.26,
    "coef_imb5":          1.1,
    "coef_imb1":          0.27,
    "coef_mom5":          0.15,
    "coef_mom300":        -0.04,
    "fill_skew":          0.3,
    "size_fraction":      0.006,
    "min_half_ticks":     1.5,
    "cross_guard_ticks":  3.0,
    # Taking: the predicted move, in bps beyond half the book's spread, at which a book takes (0 = never).
    "take_edge_bps":      0.0,
}
# Class defaults by index; classes past the last one listed start from its settings. Class 1's coefficients are
# MyAgentV2's fit there: a weak fundamental term, taker flow the strongest input and short momentum reverting.
CLASS_DEFAULTS = {1: {"imbalance_skew": 0.0, "fundamental_weight": 0.0, "cross_guard_ticks": 1.0, "lean": 1.0,
                      "coef_fund": 0.0035, "coef_ofi": 0.0, "coef_flow": 8.0, "coef_imb5": 0.0, "coef_imb1": 0.27,
                      "coef_mom5": -0.2, "coef_mom300": 0.025}}


class MyAgent10_23_Comment(FinanceAgent):
    """Inventory-skewed passive market maker quoting every book."""

    def initialize(self):
        """Reads strategy parameters; every value passed via --params arrives as a float."""
        self.quantity          = float(getattr(self.config, "quantity", 1.0))
        self.skew              = float(getattr(self.config, "skew", 0.002))
        self.max_skew          = float(getattr(self.config, "max_skew", 0.8))
        self.max_pred_bps      = float(getattr(self.config, "max_pred_bps", 15.0))
        self.inventory_band    = float(getattr(self.config, "inventory_band", 0.25))
        self.imbalance_depth   = int(getattr(self.config, "imbalance_depth", 5))
        self.vol_alpha         = float(getattr(self.config, "vol_alpha", 0.05))
        self.vol_horizon       = float(getattr(self.config, "vol_horizon", 20.0))
        self.gap_window        = float(getattr(self.config, "gap_window", 600.0))
        self.fill_window       = float(getattr(self.config, "fill_window", 1800.0))
        self.requote_frac        = float(getattr(self.config, "requote_frac", 0.3))
        self.requote_toward_frac = float(getattr(self.config, "requote_toward_frac", 0.1))
        self.requote_ticks       = int(getattr(self.config, "requote_ticks", 1))
        self.max_fee_rate      = float(getattr(self.config, "max_fee_rate", 0.02))
        self.fee_weight        = float(getattr(self.config, "fee_weight", 0.005))
        self.turnover_cap      = float(getattr(self.config, "turnover_cap", 10.0))
        self.turnover_margin   = float(getattr(self.config, "turnover_margin", 0.9))
        self.pace_burst        = float(getattr(self.config, "pace_burst", 2.0))
        # The pacing period, in simulation nanoseconds, given in --params in seconds (3600 is the old hourly pace).
        self.pace_period       = max(1, int(float(getattr(self.config, "pace_period", 300.0)) * 1e9))
        # Token-bucket pacing (`pace_tokens`, on by default; 0 restores the per-period budget above): each book's
        # allowance refills continuously at `turnover_margin` of the daily cap per day and holds at most
        # `pace_bucket` seconds of it, so a burst can spend only that much before the book waits for the refill.
        self.pace_tokens       = float(getattr(self.config, "pace_tokens", 1.0)) > 0
        self.pace_bucket       = float(getattr(self.config, "pace_bucket", 60.0))
        # Hard cap on one order, as a share of the book's wealth: a book whose minimum order is larger is not quoted,
        # rather than the minimum overriding the cap (a small exchange pool can have a minimum above it).
        self.max_order_fraction = float(getattr(self.config, "max_order_fraction", 0.02))
        # The fundamental input of the predicted move: 0 uses the raw gap to the fundamental (the gap MyAgentV2's
        # coefficient was fit on), 1 the gap beyond its own `gap_window` average. On the 23 October network layout
        # the raw gap's per-book average over the run was 29 bps (median, up to 84) on class 0, as large as its
        # variation, so at the default coefficient and lean 23 of 48 books leaned half the cap or more one way for
        # the whole run.
        self.center_fundamental = float(getattr(self.config, "center_fundamental", 0.0)) > 0
        # Budget sizing: quote size from the daily allowance per second over the book's maker fill rate while
        # quoting, measured over `fill_rate_window` seconds once `fill_rate_min_time` seconds of quoting back it.
        self.budget_sizing      = float(getattr(self.config, "budget_sizing", 1.0)) > 0
        self.budget_target      = float(getattr(self.config, "budget_target", 1.0))
        self.fill_rate_window   = float(getattr(self.config, "fill_rate_window", 600.0))
        self.fill_rate_min_time = float(getattr(self.config, "fill_rate_min_time", 60.0))
        self.expiry_period     = int(getattr(self.config, "expiry_period", 60_000_000_000))
        # Taking (see `take_edge_bps`): seconds between takes on one book, the share of a pacing period's volume budget
        # takes may use, and the taker fee rate above which a book does not take (fees only drain unscored wealth).
        self.take_cooldown      = float(getattr(self.config, "take_cooldown", 20.0))
        self.take_share         = float(getattr(self.config, "take_share", 0.25))
        self.max_take_fee_rate  = float(getattr(self.config, "max_take_fee_rate", 0.005))
        # Own markout check, the validator's way: each maker fill is marked against the mid `markout_horizon` later,
        # the move clamped to +/- the spread captured at the fill. A book whose clamped / captured ratio over the
        # last `markout_window` seconds stays below `book_quality_target` (with at least `book_min_fills` fills)
        # quotes wider, by up to `book_mult_max` (1 turns it off), at `book_adapt_rate` per fill.
        self.markout_horizon     = int(getattr(self.config, "markout_horizon", 20_000_000_000))
        self.markout_window      = float(getattr(self.config, "markout_window", 1800.0))
        self.book_quality_target = float(getattr(self.config, "book_quality_target", 0.2))
        self.book_quality_scale  = float(getattr(self.config, "book_quality_scale", 0.5))
        self.book_min_fills      = int(getattr(self.config, "book_min_fills", 5))
        self.book_adapt_rate     = float(getattr(self.config, "book_adapt_rate", 0.02))
        self.book_mult_max       = float(getattr(self.config, "book_mult_max", 2.0))
        # At most about this share of a class's books quotes wider: the effective target is the lower of
        # `book_quality_target` and the class's quality at this quantile, so a market where most books mark out
        # badly cannot widen most of the agent away from the touch, and the background flow (S3) with it.
        self.book_wide_share     = float(getattr(self.config, "book_wide_share", 0.25))
        # Local score card: printed every `report_interval` simulation nanoseconds (default one minute).
        self.report_interval   = int(getattr(self.config, "report_interval", 60_000_000_000))
        # One instance serves every validator, each running its own simulation, so all state is keyed by
        # validator: per-book memory under (validator, book_id), fill statistics and report times by validator.
        self.memory            = {}
        self.maker_stats       = {}
        self.next_report       = {}
        self.class_params      = {}
        # Maker fills waiting for their markout, by validator, and the settled markouts by (validator, class name).
        self.pending_markouts  = {}
        self.markout_stats     = {}
        # A parsed state is hundreds of thousands of small objects; default thresholds ran a full collection over
        # them every few states, which took the response from ~0.05 s to ~0.5 s. Both scoring legs need the agent
        # answering the validator, and an agent that is not answering is not scored.
        gc.set_threshold(50_000, 20, 100)

    def onTrade(self, event, validator=None):
        """Books this agent's fills into its hourly volume, and counts its passive fills by side with the
        maker fees they cost (negative fees are rebates) and the notional they add to the book's fill balance.

        `event.side` is the taker's side, so the maker's side is the opposite one.
        """
        if self.uid not in (event.makerAgentId, event.takerAgentId):
            return
        memory   = self.memory.get((validator, event.bookId))
        notional = event.price * event.quantity
        if memory is not None:
            period = event.timestamp // self.pace_period
            memory["volume"][period] = memory["volume"].get(period, 0.0) + notional
            memory["last_fill"] = event.timestamp
            memory["notional"] += notional
            if event.takerAgentId == self.uid:
                memory["take_volume"][period] = memory["take_volume"].get(period, 0.0) + notional
            if memory["tokens"] is not None:
                memory["tokens"] -= notional
        if event.takerAgentId == self.uid:
            self._stats(validator)["taken"] += 1
        if event.makerAgentId != self.uid:
            return
        stats = self._stats(validator)
        side  = OrderDirection.SELL if event.side == OrderDirection.BUY else OrderDirection.BUY
        stats[side]   += 1
        stats["fees"] += event.makerFee
        # Maker volume by taker, with every non-miner taker (negative or missing id) in one market bucket, the
        # way the validator's counterparty-diversity discount counts it.
        taker = event.takerAgentId if event.takerAgentId is not None and event.takerAgentId >= 0 else MARKET_FLOW
        stats["takers"][taker] = stats["takers"].get(taker, 0.0) + event.quantity
        if memory is not None:
            memory["fills"][side] += notional
            memory["q_fills"] += 1
            sign = 1 if side == OrderDirection.BUY else -1
            self.pending_markouts.setdefault(validator, []).append(
                (event.timestamp, event.bookId, sign, event.price, notional, memory["mid"]))

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
                self._quote_book(response, book_id, book, memory)
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

    def _params(self, book_id):
        """This book's class parameters: `<name>_c<k>` from --params, else the class default, else `<name>`."""
        classes = self.asset_classes()
        index   = next((k for k, cls in enumerate(classes) if int(book_id) in cls.books), 0)
        params  = self.class_params.get((index, classes[index].name))
        if params is None:
            params   = {}
            defaults = CLASS_DEFAULTS.get(min(index, max(CLASS_DEFAULTS)), {})
            for name, default in CLASS_PARAMS.items():
                base  = float(getattr(self.config, name, default))
                value = getattr(self.config, f"{name}_c{index}", defaults.get(name, base))
                params[name] = float(value)
            self.class_params[(index, classes[index].name)] = params
        return params

    def _stats(self, validator):
        """This validator's maker fill counts by side, the maker fees paid, in QUOTE, and the maker volume by taker."""
        return self.maker_stats.setdefault(
            validator, {OrderDirection.BUY: 0, OrderDirection.SELL: 0, "fees": 0.0, "takers": {}, "taken": 0})

    @staticmethod
    def _top_taker_share(takers, topk=P11_TOPK):
        """Share of maker volume that came from the `topk` largest miner takers.

        The validator discounts both making and skill by (this share minus those takers' share of every other
        maker's flow). Other makers' flow is not visible here, so this is the most the discount could be:
        near 0 when the fills come from the background market, near 1 when a few miners feed every fill."""
        total = sum(takers.values())
        if total <= 0:
            return 0.0
        top = sorted((v for t, v in takers.items() if t != MARKET_FLOW), reverse=True)[:topk]
        return sum(top) / total

    def _remember(self, validator, book_id, book, timestamp):
        """This book's memory for this validator, with the volatility estimate updated for the new mid, the
        fill balance decayed over `fill_window`, and the decayed inputs of the predicted move (order-flow imbalance
        at the touch, taker flow and momentum).

        A timestamp earlier than the last one seen means the validator started a new simulation, so the
        memory starts over.
        """
        best_bid, best_ask = book.bids[0], book.asks[0]
        mid    = (best_bid.price + best_ask.price) / 2
        memory = self.memory.get((validator, book_id))
        if memory is None or timestamp < memory["timestamp"]:
            memory = {"timestamp": timestamp, "mid": mid, "variance": None, "start_wealth": None, "volume": {},
                      "gap_mean": None, "last_fill": None, "notional": 0.0, "snapshots": [],
                      "fills": {OrderDirection.BUY: 0.0, OrderDirection.SELL: 0.0},
                      "mult": 1.0, "mk_clamped": 0.0, "mk_captured": 0.0, "mk_fills": 0.0, "mk_time": None,
                      "l1": None, "ofi": 0.0, "flow": 0.0, "mom5": 0.0, "mom300": 0.0, "pred": 0.0,
                      "class": self._class_name(book_id), "start_base": None,
                      "quoting": False, "q_time": 0.0, "q_fills": 0.0,
                      "take_volume": {}, "last_take": None, "tokens": None, "token_time": None, "gap_dev": 0.0}
            self.memory[(validator, book_id)] = memory
        elapsed = (timestamp - memory["timestamp"]) / 1e9
        l1 = (best_bid.price, best_bid.quantity, best_ask.price, best_ask.quantity)
        if elapsed > 0 and mid > 0 and memory["mid"] > 0:
            ret = math.log(mid / memory["mid"])
            # Exponentially weighted squared log return of the mid, per second of simulation time.
            rate = ret ** 2 / elapsed
            previous = memory["variance"]
            memory["variance"] = rate if previous is None else (1 - self.vol_alpha) * previous + self.vol_alpha * rate
            decay = math.exp(-elapsed / self.fill_window)
            for side in memory["fills"]:
                memory["fills"][side] *= decay
            # Maker fill rate while quoting: fills and quoting seconds, both decayed over `fill_rate_window`.
            decay = math.exp(-elapsed / self.fill_rate_window)
            memory["q_fills"] *= decay
            memory["q_time"]   = memory["q_time"] * decay + (elapsed if memory["quoting"] else 0.0)
            # Predictor inputs: sums decayed over a few seconds, in units of the top-five-level depth so books
            # compare, the way MyAgentV2's coefficients were fit.
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

    @staticmethod
    def _s3_factor(takers, k=S3_K):
        """The validator's tether on miner-sourced making credit, (market + min(miner, k x market)) / total of this
        agent's maker volume. Unlike the counterparty discount it needs only this agent's own fills, so it is exact:
        1.0 while at least 1 / (1 + k) of the volume comes from the market, 0 with none."""
        market = takers.get(MARKET_FLOW, 0.0)
        miner  = sum(v for t, v in takers.items() if t != MARKET_FLOW)
        total  = market + miner
        return (market + min(miner, k * market)) / total if total > 0 else 1.0

    @staticmethod
    def _market_share(takers):
        """Share of maker volume filled by the market's background flow rather than by other miners. The validator
        measures it on every board (77% for makers at large on 29 September, 7% for a feeding ring) and may weight
        making credit by it; 1.0 before the first fill."""
        total = sum(takers.values())
        return takers.get(MARKET_FLOW, 0.0) / total if total > 0 else 1.0

    def _class_name(self, book_id):
        """The asset class name of `book_id`, "other" when the layout does not list it."""
        return next((cls.name for cls in self.asset_classes() if int(book_id) in cls.books), "other")

    def _book_targets(self, validator):
        """Per class, the quality below which a book widens: `book_quality_target`, lowered to the class's
        `book_wide_share` quantile of book quality when more books than that fall below it.

        Widening a book costs it fills and moves it away from the touch where the background flow trades, so the
        rule only ever reaches the class's worst books; a whole class marking out badly is the predictor's and
        the base spread's problem, not this one's."""
        qualities = {}
        for (v, book_id), memory in self.memory.items():
            if v == validator and memory["mk_fills"] >= self.book_min_fills and memory["mk_captured"] > 0:
                qualities.setdefault(memory["class"], []).append(memory["mk_clamped"] / memory["mk_captured"])
        targets = {}
        for name, values in qualities.items():
            values.sort()
            quantile = values[min(len(values) - 1, int(self.book_wide_share * len(values)))]
            targets[name] = min(self.book_quality_target, quantile)
        return targets

    def _settle_markouts(self, validator, timestamp):
        """Marks every maker fill older than `markout_horizon` against its book's mid now, the validator's way, and
        moves the book's spread multiplier.

        Per fill, captured is the edge against the mid at the fill and realized the edge against the mid
        `markout_horizon` later, clamped to +/- captured as the validator clamps it; its making credit is the
        captured spread times the uid's clamped / captured ratio over every fill. Unclamped, a few large winners
        read as a good average markout while most fills give their spread back (MyAgentV2 and MyAgentVValidator
        averaged +5 bps a fill and narrowed to their floors at a validator quality of ~0.43), so the multiplier is
        driven by the clamped ratio. Each book keeps that ratio decayed over `markout_window`; one below
        `book_quality_target` widens the book, by up to `book_mult_max`, and one above it narrows back toward 1.
        Only bad books move: the class's width is what keeps its quotes near the touch, where the background flow
        that sets the S3 tether trades."""
        pending = self.pending_markouts.get(validator)
        if not pending:
            return
        due     = timestamp - self.markout_horizon
        keep    = []
        targets = self._book_targets(validator)
        for fill in pending:
            fill_time, book_id, sign, price, notional, fill_mid = fill
            memory = self.memory.get((validator, book_id))
            if memory is None or fill_time > memory["timestamp"]:
                # A new simulation started after the fill: nothing to mark it against.
                continue
            if fill_time > due:
                keep.append(fill)
                continue
            captured = sign * (fill_mid - price) / price
            clamped  = max(-abs(captured), min(abs(captured), sign * (memory["mid"] - price) / price))
            name     = memory["class"]
            stats    = self.markout_stats.setdefault((validator, name), {"clamped": 0.0, "captured": 0.0, "n": 0})
            stats["clamped"]  += clamped * notional
            stats["captured"] += captured * notional
            stats["n"]        += 1
            if memory["mk_time"] is not None:
                decay = math.exp(-max(0.0, fill_time - memory["mk_time"]) / 1e9 / self.markout_window)
                memory["mk_clamped"] *= decay
                memory["mk_captured"] *= decay
                memory["mk_fills"] *= decay
            memory["mk_time"]      = max(fill_time, memory["mk_time"] or fill_time)
            memory["mk_clamped"]  += clamped * notional
            memory["mk_captured"] += captured * notional
            memory["mk_fills"]    += 1
            if memory["mk_fills"] >= self.book_min_fills and memory["mk_captured"] > 0:
                quality = memory["mk_clamped"] / memory["mk_captured"]
                target  = targets.get(name, self.book_quality_target)
                step = max(-1.0, min(1.0, (target - quality) / self.book_quality_scale))
                memory["mult"] = max(1.0, min(self.book_mult_max, memory["mult"] * math.exp(self.book_adapt_rate * step)))
        self.pending_markouts[validator] = keep

    def _book_return(self, book_id, book, memory):
        """Return on this book's wealth (marked at mid) since the agent first saw it in this simulation."""
        account = self.accounts[book_id]
        wealth  = account.base_balance.total * memory["mid"] + account.quote_balance.total
        if memory["start_wealth"] is None:
            memory["start_wealth"] = wealth
        start = memory["start_wealth"]
        return wealth / start - 1 if start > 0 else 0.0

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
                for key in [k for k in self.markout_stats if k[0] == validator]:
                    del self.markout_stats[key]
            self.next_report[validator] = timestamp + self.report_interval
        if timestamp < self.next_report[validator] or not returns:
            return
        self.next_report[validator] = timestamp + self.report_interval
        # Grouped by asset class name; a lookup miss lands in "other" rather than raising, because this runs
        # outside the per-book guard in respond and an exception here would cost the whole response.
        try:
            names = {b: cls.name for cls in self.asset_classes() for b in cls.books}
        except Exception:
            names = {}
        groups = {}
        for book_id, r in sorted(returns.items()):
            groups.setdefault(names.get(int(book_id), "other"), []).append(r)
        books = " | ".join(f"books up {name}: {sum(r > 0 for r in rs)}/{len(rs)}" for name, rs in groups.items())
        stats = self._stats(validator)
        # Per asset class, as the validator scores them: books with a fill of either kind inside the skill window
        # (the coverage rule), books whose estimated edge clears the hurdle (the skill bar) and the net edge.
        classes = {}
        for book_id in returns:
            memory = self.memory[(validator, book_id)]
            edge, notional = self._window_edge(memory, self.accounts[book_id], timestamp)
            c = classes.setdefault(names.get(int(book_id), "other"),
                                   {"books": 0, "filled": 0, "skill": 0, "edge": 0.0, "wide": 0, "mult": 1.0})
            c["books"]  += 1
            c["wide"]   += memory["mult"] > 1.05
            c["mult"]    = max(c["mult"], memory["mult"])
            c["filled"] += (memory["last_fill"] or -SKILL_WINDOW) > timestamp - SKILL_WINDOW
            if notional > 0:
                c["skill"] += edge >= SKILL_HURDLE * notional
                c["edge"]  += edge
        def markout(name, c):
            """The class's clamped / captured markout ratio, the validator's markout quality measured against the
            book mid, and how many of its books quote wider for a ratio below target."""
            s = self.markout_stats.get((validator, name))
            if not s or s["captured"] <= 0:
                return "markout quality n/a"
            return (f"markout quality ~{max(0.0, min(1.0, s['clamped'] / s['captured'])):.2f} over {s['n']} fills, "
                    f"{c['wide']} books wider (max x{c['mult']:.2f})")

        skill = " | ".join(
            f"{name}: {markout(name, c)}, "
            f"filled in 3 h {c['filled']}/{c['books']} (coverage needs {math.ceil(SKILL_COVERAGE * c['books'])}), "
            f"est. skill books {c['skill']}/"
            f"{max(SKILL_MIN_BOOKS_FLOOR, math.ceil(round(SKILL_MIN_BOOKS_SHARE * c['books'], 9)))}, "
            f"est. net edge {c['edge']:+.2f}"
            for name, c in classes.items())
        bt.logging.info(
            f"SCORE CARD | avg return {100 * sum(returns.values()) / len(returns):+.4f}% | {books} | "
            f"maker fills buy {stats[OrderDirection.BUY]} / sell {stats[OrderDirection.SELL]} | "
            f"maker fees {stats['fees']:+.4f} | taker fills {stats['taken']} | "
            f"top-{P11_TOPK} miner takers {100 * self._top_taker_share(stats['takers']):.1f}% of maker volume | "
            f"S3 factor {self._s3_factor(stats['takers']):.2f} | "
            f"open-market share {100 * self._market_share(stats['takers']):.0f}% of maker volume | "
            f"{skill}"
        )

    @staticmethod
    def _window_edge(memory, account, timestamp):
        """(edge, filled notional) on this book over the skill window: what the trades since the window opened made
        beyond holding the position the agent had then, marked at the current mid. A rough stand-in for the
        validator's drift-stripped per-book alpha, for the score card only.

        Takes a snapshot of the account each call and measures against the oldest one inside the window."""
        snapshots = memory["snapshots"]
        snapshots.append((timestamp, account.base_balance.total, account.quote_balance.total, memory["notional"]))
        while len(snapshots) > 1 and snapshots[1][0] <= timestamp - SKILL_WINDOW:
            snapshots.pop(0)
        _, base, quote, notional = snapshots[0]
        mid  = memory["mid"]
        edge = (account.base_balance.total - base) * mid + (account.quote_balance.total - quote)
        return edge, memory["notional"] - notional

    def _imbalance(self, book):
        """(bid - ask) / (bid + ask) volume over the top `imbalance_depth` levels; positive when bids are heavier."""
        bid_volume = sum(level.quantity for level in book.bids[:self.imbalance_depth])
        ask_volume = sum(level.quantity for level in book.asks[:self.imbalance_depth])
        total      = bid_volume + ask_volume
        return (bid_volume - ask_volume) / total if total > 0 else 0.0

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
        """Predicted move of the mid over the next 20 s, in bps, held within +/- `max_pred_bps`: MyAgentV2's linear
        blend of the gap to the fundamental, order-flow imbalance at the touch, taker flow, the book's volume
        imbalance and the mid's own momentum. Out of sample on a recorded local run it correlated about 0.24 with
        the 20 s move on class 0 and 0.19 on class 1; fills against the fundamental gap alone marked out at
        -5.3 bps, fills with it at +1.9.

        A book without a published fundamental (the exchange mechanism) contributes no fundamental term."""
        mid = memory["mid"]
        fundamental = getattr(book, "fundamental", None)
        gap = (fundamental - mid) / mid * 1e4 if fundamental and fundamental > 0 else 0.0
        if self.center_fundamental:
            # The gap beyond its own `gap_window` average (set by `_fundamental_lean`, called just before).
            gap = memory.get("gap_dev", 0.0) * 1e4 if fundamental and fundamental > 0 else 0.0
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

    def _fundamental_lean(self, book, memory, mid):
        """The gap between the fundamental and the mid (as a fraction of the mid) beyond its own recent average.

        The average is exponentially weighted over `gap_window` seconds and taken before this round's gap is
        folded in; a book without a published fundamental leans nowhere.
        """
        fundamental = getattr(book, "fundamental", None)
        if not fundamental or fundamental <= 0:
            return 0.0
        gap      = (fundamental - mid) / mid
        previous = memory["gap_mean"]
        weight   = 1 - math.exp(-memory["elapsed"] / self.gap_window) if memory["elapsed"] > 0 else 0.0
        memory["gap_mean"] = gap if previous is None else previous + weight * (gap - previous)
        # Kept for the predicted move, which reads it under `center_fundamental`.
        memory["gap_dev"] = 0.0 if previous is None else gap - previous
        return memory["gap_dev"]

    def _budget_size(self, book_id, memory, mid):
        """The quote size, in BASE, at which this book's fills would spend the daily volume allowance at its
        average rate: `budget_target` x the allowance per second over the maker fills per second while quoting.
        0 (so the minimum order) until `fill_rate_min_time` seconds of quoting have measured the rate; no limit
        on a market without an allowance."""
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        if wealth <= 0:
            return float("inf")
        if memory["q_time"] < self.fill_rate_min_time or memory["q_fills"] <= 0:
            return 0.0
        allowance = self.turnover_cap * wealth / (DAY / 1e9)   # QUOTE per second
        fill_rate = memory["q_fills"] / memory["q_time"]        # maker fills per second while quoting
        return self.budget_target * allowance / fill_rate / mid

    def _inventory_gap(self, account, memory, mid, wealth):
        """BASE held beyond the book's initial BASE, valued at mid as a share of wealth; positive when long.

        The initial BASE is the account's `initial` balance; an account without one (exchange mode) uses the
        balance first seen in this simulation instead."""
        initial = getattr(account.base_balance, "initial", None)
        if initial is None:
            if memory.get("start_base") is None:
                memory["start_base"] = account.base_balance.total
            initial = memory["start_base"]
        return (account.base_balance.total - initial) * mid / wealth if wealth > 0 else 0.0

    def _fill_balance(self, memory):
        """(buy - sell) / (buy + sell) of this book's recent maker notional; positive when buys ran ahead."""
        buys, sells = memory["fills"][OrderDirection.BUY], memory["fills"][OrderDirection.SELL]
        return (buys - sells) / (buys + sells) if buys + sells > 0 else 0.0

    def _over_volume_pace(self, book_id, account, memory):
        """True while this book's volume would outrun the validator's daily cap.

        The cap is `turnover_cap` x miner wealth per book over a rolling day; past it the validator drops
        every new order. The agent stops at `turnover_margin` of it, both by the validator's own count and by
        its own fills over the last 24 hours, and keeps any `pace_period` within `pace_burst` times an even
        share of the day, so the budget lasts the whole day instead of running out mid-run.

        The period was an hour, and an hour's share went in its first few minutes: on the 23 October network run
        every book reached it about three simulated minutes into the hour and then sat dark for the other ~47,
        and those minutes were the opening rush, where 97% of the refused post-only orders fell and most fills
        came from other miners rather than the background flow the S3 tether credits. Five-minute periods spend
        the same daily budget at the same average rate, spread over the hour.
        """
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        if wealth <= 0:
            return False
        cap     = self.turnover_cap * wealth
        period  = memory["timestamp"] // self.pace_period
        volume  = memory["volume"]
        per_day = DAY // self.pace_period
        for old in [p for p in volume if p <= period - per_day]:
            del volume[old]
        day = sum(volume.values())
        if (account.traded_volume or 0.0) >= self.turnover_margin * cap or day >= self.turnover_margin * cap:
            return True
        if not self.pace_tokens:
            return volume.get(period, 0.0) >= self.pace_burst * cap * self.pace_period / DAY
        # Token bucket: the allowance accrues at `turnover_margin` of the cap per day, up to `pace_bucket` seconds of
        # it, and every fill spends its notional (onTrade). Even at the minimum order a quoting book fills about
        # three times faster than the allowance, so it still pauses; a five-minute period let it spend the period's
        # budget in its first seconds and then wait out the rest, while the bucket spreads the pauses over
        # about a minute.
        rate     = self.turnover_margin * cap / DAY                    # QUOTE per simulation nanosecond
        capacity = rate * self.pace_bucket * 1e9
        now      = memory["timestamp"]
        if memory["tokens"] is None:
            memory["tokens"] = capacity
        else:
            memory["tokens"] = min(capacity, memory["tokens"] + rate * max(0, now - memory["token_time"]))
        memory["token_time"] = now
        return memory["tokens"] <= 0

    def _quote_book(self, response, book_id, book, memory):
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
        # Folded in every round, so the average and the predictor's inputs stay current even while the book is not
        # being quoted.
        fundamental_lean = self._fundamental_lean(book, memory, mid)
        pred = self._predict(book, account, memory, params)

        # Stop making while the maker fee is above tolerance (negative rates are rebates; the default tolerance is
        # above anything the dynamic fee policy charges), and while the book's
        # volume is ahead of its pace through the cap: past the cap the validator drops new orders, so resting
        # quotes could not follow the market any more.
        fee_rate = account.fees.maker_fee_rate if account.fees is not None else 0.0
        memory["quoting"] = not (fee_rate > self.max_fee_rate or self._over_volume_pace(book_id, account, memory))
        if not memory["quoting"]:
            self._cancel(response, book_id, bids)
            self._cancel(response, book_id, asks)
            return

        decimals = self.price_decimals(book_id)
        tick     = 10 ** -decimals
        # Half spread: wide enough for the mid's typical move over the markout horizon and for a whole tick of
        # edge on a coarse grid, plus the part of the maker fee the score sees.
        sigma = math.sqrt(memory["variance"] * self.vol_horizon) if memory["variance"] else 0.0
        floor = params["min_half_ticks"] * tick / mid
        # A book whose fills keep less of their spread than the target quotes wider (`_settle_markouts`).
        half  = max(params["half_spread"], params["vol_mult"] * sigma, floor) * memory["mult"]
        # The validator nets `making_fee_weight` of each maker fee from making credit (a rebate adds) and leaves fees
        # out of skill, so the quotes move by that share of the rate only, and a rebate never pulls them inside
        # the tick floor.
        half = max(half + self.fee_weight * fee_rate, floor)

        # Inventory: the BASE held beyond the book's initial BASE, as a share of wealth; 0 is balanced, above it
        # means long. The target was half of wealth, but every book starts below it (7-50% in BASE, median 42%,
        # on the 23 October network layout), so the skew leaned every book toward buying and the seven books at
        # 25% or less never quoted an ask at all: this agent's maker fills ran 752 buys to 276 sells, and making
        # credit pays only the smaller side of each book.
        wealth    = account.base_balance.total * mid + account.quote_balance.total
        inventory = self._inventory_gap(account, memory, mid, wealth)
        # Lean toward the heavier side of the book and toward the fundamental, and away from the inventory and
        # from the side whose fills ran ahead (making credit counts the smaller side), by at most `max_skew` of
        # the half spread, so the bid always stays below the fair price and the ask above it. The mid closes
        # only a few per cent of its gap to the fundamental over 20 s, hence the small weight.
        shift = (params["imbalance_skew"] * self._imbalance(book) * half
                 + params["fundamental_weight"] * fundamental_lean
                 - params["fill_skew"] * self._fill_balance(memory) * half
                 - self.skew * inventory)
        shift = max(-self.max_skew * half, min(self.max_skew * half, shift))
        # The fair price moves `lean` times the predicted 20 s move, within `max_lean` half spreads: at the default
        # one, a strong prediction brings the quote on its side to the mid and the other one a whole spread out,
        # so the quote the market is about to run over steps away while both stay at this agent's own width.
        # MyAgentV2 leaned up to 30 bps on its wider quotes and clamped many of them onto the opposite touch. With
        # the inventory and fill skews on top, the whole shift stays within the larger of the two caps, so no
        # quote passes the mid.
        lean  = params["max_lean"] * half
        shift += max(-lean, min(lean, params["lean"] * pred / 1e4))
        total = max(self.max_skew, params["max_lean"]) * half
        shift = max(-total, min(total, shift))

        # Never cross the book, with `cross_guard_ticks` of room: the order lands after the response delay, and a
        # post-only quote the touch has moved onto is refused after its predecessor was already cancelled. On the
        # 23 October network run this agent lost 1083 class 0 asks that way with one tick of room, and its fills
        # ran 6288 buys to 4579 sells; MyAgentV3's 3 ticks halved the same refusals against MyAgentV2.
        guard     = max(1.0, params["cross_guard_ticks"]) * tick
        bid_price = min(self._on_grid(mid * (1 + shift - half), decimals, True), round(best_ask - guard, decimals))
        ask_price = max(self._on_grid(mid * (1 + shift + half), decimals, False), round(best_bid + guard, decimals))
        if bid_price <= 0 or bid_price >= ask_price:
            memory["quoting"] = bool(bids or asks)
            return

        # One volume increment above the minimum: the engine rounds the float it receives down onto its
        # grid, so an order of exactly min_order_size (e.g. 2.5364 -> 2.53639999...) arrives as 2.5363.
        volume_decimals = self.volume_decimals(book_id)
        step            = 10 ** -volume_decimals
        min_quantity    = self.min_order_size(book_id) + step
        size            = params["size_fraction"] * wealth / mid if params["size_fraction"] > 0 else self.quantity
        if self.budget_sizing:
            size = min(size, self._budget_size(book_id, memory, mid))
        quantity        = round(max(size, min_quantity), volume_decimals)
        # The hard cap holds over the minimum order: a book whose smallest legal order is larger is not quoted.
        if self.max_order_fraction > 0:
            cap_quantity = math.floor(self.max_order_fraction * wealth / mid / step + 1e-9) * step
            if min_quantity > cap_quantity:
                self._cancel(response, book_id, bids)
                self._cancel(response, book_id, asks)
                memory["quoting"] = False
                return
            quantity = round(min(quantity, cap_quantity), volume_decimals)

        # A quote that drifted away from the fair price may rest a while; one that drifted toward it is the one
        # the market runs over, so it is replaced almost at once.
        away   = max(self.requote_frac * half * mid, self.requote_ticks * tick)
        toward = self.requote_toward_frac * half * mid
        resting = 0
        # Far from balanced, quote only the side that brings inventory back. The cancels go ahead of the new order
        # in the response, so the funds a side's own resting orders hold count as free for its replacement.
        if inventory >= self.inventory_band:
            self._cancel(response, book_id, bids)
        else:
            free = account.quote_balance.free + sum(o.quantity * o.price for o in bids)
            resting += self._quote_side(response, book_id, bids, bid_price, OrderDirection.BUY, quantity,
                                        min_quantity, volume_decimals, away, toward, free)
        if inventory <= -self.inventory_band:
            self._cancel(response, book_id, asks)
        else:
            free = self._sellable(account) + sum(o.quantity for o in asks)
            resting += self._quote_side(response, book_id, asks, ask_price, OrderDirection.SELL, quantity,
                                        min_quantity, volume_decimals, away, toward, free)
        # Quoting time for the fill rate behind budget sizing counts only while a quote rests on the book.
        memory["quoting"] = resting > 0
        self._take(response, book_id, book, account, memory, params, pred, inventory, quantity, min_quantity,
                   volume_decimals)

    def _take(self, response, book_id, book, account, memory, params, pred, inventory, quantity, min_quantity,
              volume_decimals):
        """Takes one quote size at the touch when the predicted 20 s move beats half the spread by `take_edge_bps`.

        Never more often than `take_cooldown`, never past `take_share` of the pacing period's volume budget, never
        while the taker fee rate exceeds `max_take_fee_rate`, and never in the direction the inventory band already
        forbids. The order is immediate-or-cancel, and self-trade prevention cancels it rather than this agent's own
        resting quote. The position it opens is unwound by the quotes: the lean and inventory skew move with it."""
        edge = params["take_edge_bps"]
        if edge <= 0:
            return
        best_bid, best_ask = book.bids[0], book.asks[0]
        mid       = memory["mid"]
        half_bps  = (best_ask.price - best_bid.price) / 2 / mid * 1e4
        if pred - half_bps >= edge and inventory < self.inventory_band:
            direction, level = OrderDirection.BUY, best_ask
        elif -pred - half_bps >= edge and inventory > -self.inventory_band:
            direction, level = OrderDirection.SELL, best_bid
        else:
            return
        now = memory["timestamp"]
        if memory["last_take"] is not None and now - memory["last_take"] < self.take_cooldown * 1e9:
            return
        fee_rate = account.fees.taker_fee_rate if account.fees is not None else 0.0
        if fee_rate > self.max_take_fee_rate:
            return
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        if wealth > 0:
            budget = self.take_share * self.pace_burst * self.turnover_cap * wealth * self.pace_period / DAY
            if memory["take_volume"].get(now // self.pace_period, 0.0) >= budget:
                return
        size = round(min(quantity, level.quantity), volume_decimals)
        if size < min_quantity:
            return
        # Funds left after this round's quotes, which may have drawn on the same balance.
        if direction == OrderDirection.BUY:
            if account.quote_balance.free < 2 * size * level.price:
                return
        elif self._sellable(account) < 2 * size:
            return
        memory["last_take"] = now
        tick = 10 ** -self.price_decimals(book_id)
        response.limit_order(
            book_id=book_id,
            direction=direction,
            quantity=size,
            price=level.price + 1e-6 * tick,
            stp=STP.CANCEL_NEWEST,
            timeInForce=TimeInForce.IOC,
        )

    def _sellable(self, account):
        """BASE one sell can draw on. On the exchange a sell swaps against a single delegate, so the cap is the
        largest delegate's stake (`sellable_alpha`), not the free sum; an empty `delegate_stakes` means not
        reported, so fall back to the free balance, as in simulation."""
        if self.exchange_mode and getattr(account, "delegate_stakes", None):
            return min(account.base_balance.free, getattr(account, "sellable_alpha", account.base_balance.free))
        return account.base_balance.free

    @staticmethod
    def _on_grid(price, decimals, down):
        """`price` on the tick grid, rounded down for a bid and up for an ask, so rounding never moves a quote
        closer to the fair price. The 1e-9 keeps float noise (29.99 x 100 = 2998.9999...) from costing a tick."""
        units = price * 10 ** decimals
        units = math.floor(units + 1e-9) if down else math.ceil(units - 1e-9)
        return round(units / 10 ** decimals, decimals)

    def _quote_side(self, response, book_id, orders, target, direction, quantity, min_quantity, volume_decimals,
                    away, toward, free):
        """Keeps or replaces this side's quote; True when a quote rests on the side after this round.

        The replacement is sized before anything is cancelled: `quantity`, cut to what `free` (QUOTE for a bid,
        BASE for an ask, this side's resting orders included) pays for. When no legal order fits, a single resting
        quote is kept rather than the side left empty, unless it has drifted toward the fair price, where the
        market runs it over."""
        replace, risky = self._needs_requote(orders, target, direction, away, toward)
        if not replace:
            return True
        step       = 10 ** -volume_decimals
        affordable = math.floor(free / (target if direction == OrderDirection.BUY else 1.0) / step + 1e-9) * step
        size       = round(min(quantity, affordable), volume_decimals)
        if size >= min_quantity:
            self._cancel(response, book_id, orders)
            self._place(response, book_id, direction, size, target)
            return True
        if len(orders) == 1 and not risky:
            return True
        self._cancel(response, book_id, orders)
        return False

    @staticmethod
    def _needs_requote(orders, target, direction, away, toward):
        """(replace, risky): replace unless this side has exactly one quote near `target`, at most `away` further from
        the fair price than the target or at most `toward` closer to it; risky when that single quote has drifted
        closer than `toward`. Several quotes on a side are always replaced."""
        if len(orders) == 1:
            # Positive when the resting quote sits closer to the fair price than its target.
            closer = orders[0].price - target if direction == OrderDirection.BUY else target - orders[0].price
            if -away - 1e-9 <= closer <= toward + 1e-9:
                return False, False
            return True, closer > toward + 1e-9
        return True, False

    def _cancel(self, response, book_id, orders):
        """Cancels every order in `orders`, as one instruction."""
        if orders:
            response.cancel_orders(book_id, [o.id for o in orders])

    def _place(self, response, book_id, direction, quantity, price):
        """Places a post-only, expiring limit order so it can only ever rest as a maker.

        The engine truncates the price it receives onto the tick grid, and many grid prices are a hair below
        their decimal value as floats (0.0068 is 0.00679999...), which would rest the order a whole tick lower:
        a bid further from the fair price, an ask closer to it. A millionth of a tick on top keeps the float
        above the grid point without reaching the next one, so the order rests where it was priced."""
        tick = 10 ** -self.price_decimals(book_id)
        response.limit_order(
            book_id=book_id,
            direction=direction,
            quantity=quantity,
            price=price + 1e-6 * tick,
            stp=STP.CANCEL_BOTH,
            postOnly=True,
            timeInForce=TimeInForce.GTT,
            expiryPeriod=self.expiry_period,
        )


if __name__ == "__main__":
    launch(MyAgent10_23_Comment)
