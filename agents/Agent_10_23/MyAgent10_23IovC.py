# SPDX-License-Identifier: MIT
"""
MyAgent10_23IOV with MyAgent10_23_Comment's volume handling: IOV's quoting, markout multipliers and score replay
(the stronger of the two on the 23 October 10_23 network run: trading 0.686 against 0.324, the highest making on
every validator with half the notional), and from Comment the parts IOV lacked:
  - token-bucket pacing (`pace_tokens`, on by default): each book's allowance refills continuously at
    `turnover_margin` of the daily cap and holds `pace_bucket` seconds of it, so a burst pauses the book for
    about a minute instead of until the next five-minute period,
  - budget sizing (`budget_sizing`, on by default): each quote is sized so the book's fills spend the daily
    allowance at its average rate, the allowance per second over the book's maker fill rate while a quote rests
    (`budget_target` of it), never above `size_fraction` of wealth; a book with no measured rate starts at the
    minimum order. Smaller quotes rest longer at the same volume, which is time at the touch for the background
    flow the S3 tether credits,
  - a hard cap of `max_order_fraction` of the book's wealth on any order (a book whose minimum is larger is not
    quoted), and replacements sized to the funds free before the cancel, so a quote that cannot be replaced is
    kept rather than the side left empty, unless it has drifted toward the fair price,
  - optionally (`center_fundamental=1`) the predicted move reads the fundamental gap beyond its own `gap_window`
    average, which drops the per-book offset the raw gap carries for a whole run.

The rest of this description is MyAgent10_23IOV's.

Predictive two-sided market maker that tracks its own validator score legs book by book: MyAgentVValidator's
quoting and score replay, with MyAgentVInput's bounded lean and the pacing, cross and fee fixes both lacked.

Where the parts come from (net3x64 network run, 23 October: 3 validators x 64 books, 30 minutes, 16 agents):
  - MyAgentVValidator (then MyAgentV4) kept more of its spread: realized 227-267 against MyAgentVInput's 177-212,
    realized / captured ~0.44 against ~0.29. Its predictive fair price, class and book markout multipliers and
    replay of the skill legs are kept as they were.
  - MyAgentVInput (then MyAgent) was refused 33 post-only orders against 1015, because every lean it had stayed
    inside its half spread, so no quote was ever clamped onto the opposite touch. Here the predicted move is held
    within `max_lean` half spreads the same way (MyAgentVValidator leaned up to 30 bps on top of a ~13 bps half
    spread). It also kept a quote that drifted away from the fair price for 2 ticks (`requote_ticks`), which
    keeps its queue place.
  - Both spent each book's hourly volume budget in the first ~3 minutes of the hour and then sat dark for ~47, in
    the opening rush where 97% of refusals fell and most fills came from other miners: the budget is paced over
    `pace_period` (five minutes) instead.
  - Both stepped back from a high-fee book with the whole fee and went dark above 0.2%; the validator nets only
    `fee_weight` (0.5%) of a maker fee from making credit and leaves fees out of skill, so the quotes move by
    that share of the rate and the book stays quoted up to `max_fee_rate` (2%, above the dynamic policy's 1.5%).
  - A quote keeps `cross_guard_ticks` of room from the opposite touch (3 on class 0, 1 on class 1), so a touch that
    moves during the response delay does not turn it into a refused post-only order.
  - The score card adds the background-flow share of its maker volume and the validator's S3 tether on it.
  - The engine turns the price it receives into a 128-bit decimal and truncates it onto the tick grid, and about
    half of the grid prices are a hair below their decimal value as floats (300.07 is 300.06999...), so those
    orders rested a tick lower: bids a tick further out, asks a tick closer to the bid. On the 23 October 10_23
    network run 111 of this agent's 134 refused post-only orders were class 0 SELLs. With `grid_nudge` (on by
    default) a millionth of a tick goes on top, so every order rests at the price it was given.
  - Optionally (`take_edge_bps` > 0, off by default) a book also takes: an immediate-or-cancel order at the
    touch for one quote size when the predicted 20 s move beats half the spread by `take_edge_bps`, at most once
    per `take_cooldown` seconds and within `take_share` of the pacing period's volume. On 20 October the team said
    the skill half pays takers as well as makers and takers held just over half of it.
  - Quotes expire per class (`expiry_period`): 60 s on class 0, 300 s on class 1. On the 23 October 10_23
    network run (the first with this agent) its class 1 fills marked out at +8 to +9 bps with quality 0.62-0.67
    and the class's spread multiplier sat at its 0.70 floor, so class 1 wanted more fills but already quoted at
    the touch, where the queue decides them; a 60 s expiry sent every quote to the back of it once a minute.
    Requoting still replaces a class 1 quote whose target moves more than a tick toward it or two ticks away.

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
  - the fair price is the mid moved by `lean` times a predicted 20 s move, held within `max_lean` half spreads
    (with the inventory and capture skews, the whole shift stays within the larger of `max_lean` and `max_skew`,
    so no quote passes the mid): a linear blend of the gap to the
    book's published fundamental, order-flow imbalance at the touch over ~5 s (`ofi`), signed taker flow over
    ~20 s (`flow`), the top-`imbalance_depth` and top-of-book volume imbalance (own orders taken out), and the
    mid's own move over ~5 s and ~300 s (`mom5`, `mom300`), each with a per-class `coef_<input>`; the
    prediction is held within +/- `max_pred_bps`. Fit on a recorded local run (128 books, one hour): out of
    sample it correlates about 0.24 with the 20 s move on class 0 and 0.19 on class 1. The fundamental gap is
    the strongest single input (+0.21); fills against it marked out at -5.3 bps, fills with it at +1.9.
  - quotes sit a half spread either side of it: the largest of `half_spread`, `vol_mult` times the mid's
    volatility over the 20 s markout horizon (an exponential average over `vol_window` seconds) and
    `min_half_ticks` ticks, times the class's and the book's markout multipliers, plus `fee_weight` of the maker fee
    rate (a rebate tightens). On class 0 (spread ~1 bps, 20 s move ~10 bps) a quote near the touch is filled only by the moves
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
  - each quote is at most `size_fraction` of the book's wealth (or `quantity` BASE when that is 0), less under
    budget sizing (above),
  - a resting quote that has drifted further from the fair price than its target is kept while within
    `requote_frac` of the half spread (never less than `requote_ticks` ticks); one that has drifted toward
    it is replaced once past `requote_toward_frac` of the half spread, or `requote_toward_ticks` ticks if more.
    On class 1 a tick is ~3.4 bps and the touch holds thousands of units, so a quote replaced every second
    sat at the back of the queue (90% of orders at the touch, 0.6% filled); there a one-tick wobble keeps it,
  - quoting stops on a book while its maker fee rate exceeds `max_fee_rate`, while its own fills run ahead
    of an even pace through the validator's daily volume cap (`turnover_cap` x miner wealth, at most
    `pace_burst` times an even share in any `pace_period`), and once its traded volume reaches `turnover_margin`
    of that cap, past which the validator drops every instruction except cancels.

The score card prints, per class, the markout quality as the validator computes it (realized clamped per fill)
and the skill gate: books whose alpha clears the hurdle against the class's bar (`skill_bar_share` of its books),
how many of them are positive, and their net alpha, and the coverage rule (books with a fill of either kind in the
last 3 simulated hours, against the (1 - 0.375) of the class's books below which skill is scaled down); and over
all books the share of maker volume the background market filled, the S3 factor it implies (miner-sourced credit
counts up to `S3_K` times the market's) and the share the `P11_TOPK` largest miner takers supplied, the most the
validator's counterparty discount (applied to both legs since 0.6.2) could read from them.

Parameters can differ by asset class: `<name>_c<k>` overrides `<name>` on the books of the k-th class
(0 = books 0-95, 1 = books 96-127 on the multi-asset layout), e.g. `half_spread_c1=0.001`. Any later class starts
from class 1's settings.

The validator turns response time into order delay (10 ms up to 1 s, exponential in the share of its timeout), so
the garbage collector is told to run rarely: its full passes over a parsed state were most of the slow responses.

Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_23IovC.py --port 8888 --agent_id 0 --params lazy_load=1 lean_c0=2 max_lean_c0=1
"""
import gc
import math

import bittensor as bt
from taos.common.agents import launch
from taos.im.agents import FinanceAgent
from taos.im.protocol.models import OrderDirection, STP, TimeInForce

DAY = 86_400_000_000_000
# The validator's skill bar never falls below four books (reward.SKILL_MIN_BOOKS_FLOOR).
SKILL_MIN_BOOKS = 4
# Taker key for every non-miner counterparty, as the validator's counterparty rules count it.
MARKET_FLOW = -1
# Making credit sourced from other miners counts only up to this multiple of the credit sourced from the market
# (`scoring.debeta.s3_k`, 4 since 0.6.3).
S3_K = 4.0
# How many of the largest miner takers the validator's counterparty discount reads (`scoring.debeta.p11_topk`).
P11_TOPK = 10
# The validator scales skill down once an agent's books with a fill in the last three simulated hours fall below
# (1 - `skill_max_inactive_books`) of the class's books.
SKILL_WINDOW = 3 * 3_600_000_000_000
SKILL_COVERAGE = 1 - 0.375

# Per-class strategy parameters and their defaults; `<name>_c<k>` in --params overrides one for class k.
# The coefficients are bps of predicted 20 s move per unit of their input (see _predict).
CLASS_PARAMS = {
    "half_spread":          0.0006,
    "vol_mult":             1.3,
    "lean":                 2.0,
    "max_lean":             1.0,
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
    "cross_guard_ticks":    3.0,
    # Nanoseconds a quote rests before the engine drops it; a dropped quote re-enters at the back of its queue.
    "expiry_period":        60_000_000_000,
    # Taking: the predicted move, in bps beyond half the book's spread, at which a book takes (0 = never).
    "take_edge_bps":        0.0,
}
# Class defaults by index; classes past the last one listed start from its settings.
CLASS_DEFAULTS = {1: {"half_spread": 0.0005, "vol_mult": 1.5, "lean": 1.0, "coef_fund": 0.0035, "coef_ofi": 0.0,
                      "coef_flow": 8.0, "coef_imb5": 0.0, "coef_imb1": 0.27, "coef_mom5": -0.2, "coef_mom300": 0.025,
                      "size_fraction": 0.012, "requote_toward_ticks": 1.0, "cross_guard_ticks": 1.0,
                      "expiry_period": 300_000_000_000}}


class MyAgent10_23IovC(FinanceAgent):
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
        self.requote_ticks       = int(getattr(self.config, "requote_ticks", 2))
        # Above the dynamic fee policy's 1.5% ceiling: the score nets only `fee_weight` of a maker fee (the
        # validator's scoring.debeta.making_fee_weight), so a dark book would lose its making and coverage for it.
        self.max_fee_rate      = float(getattr(self.config, "max_fee_rate", 0.02))
        self.fee_weight        = float(getattr(self.config, "fee_weight", 0.005))
        self.turnover_cap      = float(getattr(self.config, "turnover_cap", 10.0))
        self.turnover_margin   = float(getattr(self.config, "turnover_margin", 0.9))
        self.pace_burst        = float(getattr(self.config, "pace_burst", 2.0))
        self.pace_period       = max(1, int(float(getattr(self.config, "pace_period", 300.0)) * 1e9))
        # Token-bucket pacing (`pace_tokens`, on by default; 0 restores the per-period budget above): each book's
        # allowance refills continuously at `turnover_margin` of the daily cap per day and holds at most
        # `pace_bucket` seconds of it, so a burst can spend only that much before the book waits for the refill.
        self.pace_tokens       = float(getattr(self.config, "pace_tokens", 1.0)) > 0
        self.pace_bucket       = float(getattr(self.config, "pace_bucket", 60.0))
        # Budget sizing: quote size from the daily allowance per second over the book's maker fill rate while a
        # quote rests, measured over `fill_rate_window` seconds once `fill_rate_min_time` seconds of quoting back it.
        self.budget_sizing      = float(getattr(self.config, "budget_sizing", 1.0)) > 0
        self.budget_target      = float(getattr(self.config, "budget_target", 1.0))
        self.fill_rate_window   = float(getattr(self.config, "fill_rate_window", 600.0))
        self.fill_rate_min_time = float(getattr(self.config, "fill_rate_min_time", 60.0))
        # Hard cap on one order, as a share of the book's wealth: a book whose minimum order is larger is not quoted,
        # rather than the minimum overriding the cap (a small exchange pool can have a minimum above it).
        self.max_order_fraction = float(getattr(self.config, "max_order_fraction", 0.02))
        # The fundamental input of the predicted move: 0 the raw gap (the gap the coefficients were fit on), 1 the
        # gap beyond its own `gap_window` average. On the 23 October network layout the raw gap's per-book average
        # over the run was 29 bps (median, up to 84) on class 0, as large as its variation.
        self.center_fundamental = float(getattr(self.config, "center_fundamental", 0.0)) > 0
        self.gap_window         = float(getattr(self.config, "gap_window", 600.0))
        # Orders priced a millionth of a tick above their grid point, so the engine's truncation keeps them on it.
        self.grid_nudge        = float(getattr(self.config, "grid_nudge", 1.0)) > 0
        # Taking (see `take_edge_bps`): seconds between takes on one book, the share of a pacing period's volume budget
        # takes may use, and the taker fee rate above which a book does not take (fees only drain unscored wealth).
        self.take_cooldown     = float(getattr(self.config, "take_cooldown", 20.0))
        self.take_share        = float(getattr(self.config, "take_share", 0.25))
        self.max_take_fee_rate = float(getattr(self.config, "max_take_fee_rate", 0.005))
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
        # The base class copies every state into self.history unless told not to; nothing here reads it.
        self.history_len       = int(getattr(self.config, "history_len", 0))
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
        """Books this agent's fills into its paced volume, counts its passive fills by side with the maker fees
        they cost (negative fees are rebates) and the spread they captured against the mid, and queues each
        passive fill for its markout.

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
        # Maker volume by taker, every non-miner taker (negative or missing id) in one market bucket.
        taker = event.takerAgentId if event.takerAgentId is not None and event.takerAgentId >= 0 else MARKET_FLOW
        stats["takers"][taker] = stats["takers"].get(taker, 0.0) + event.quantity
        if memory is not None:
            memory["q_fills"] += 1
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
        """This validator's maker fill counts by side, the maker fees paid, in QUOTE, and the maker volume by taker."""
        return self.maker_stats.setdefault(
            validator, {OrderDirection.BUY: 0, OrderDirection.SELL: 0, "fees": 0.0, "takers": {}, "taken": 0})

    @staticmethod
    def _s3_factor(takers, k=S3_K):
        """The validator's tether on miner-sourced making credit, (market + min(miner, k x market)) / total of this
        agent's maker volume, and the market's share of that volume: 1.0 and 1.0 before the first fill."""
        market = takers.get(MARKET_FLOW, 0.0)
        total  = sum(takers.values())
        if total <= 0:
            return 1.0, 1.0
        return (market + min(total - market, k * market)) / total, market / total

    @staticmethod
    def _top_taker_share(takers, topk=P11_TOPK):
        """Share of maker volume the `topk` largest miner takers supplied. The validator discounts making and skill
        by this share minus those takers' share of every other maker's flow, which is not visible here, so this is
        the most the discount could be: near 0 when the background market fills the quotes."""
        total = sum(takers.values())
        if total <= 0:
            return 0.0
        return sum(sorted((v for t, v in takers.items() if t != MARKET_FLOW), reverse=True)[:topk]) / total

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
                                "price": None, "inventory": None},
                      "last_fill": None, "take_volume": {}, "last_take": None, "tokens": None, "token_time": None,
                      "quoting": False, "q_time": 0.0, "q_fills": 0.0, "gap_mean": None, "gap_dev": 0.0}
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
            # Maker fill rate while a quote rests: fills and quoting seconds, both decayed over `fill_rate_window`.
            decay = math.exp(-elapsed / self.fill_rate_window)
            memory["q_fills"] *= decay
            memory["q_time"]   = memory["q_time"] * decay + (elapsed if memory["quoting"] else 0.0)
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

    def _skill_gate(self, validator, books, timestamp):
        """Over `books`: how many alphas clear their hurdle, how many of those are positive, their sum, and how many
        books had a fill of either kind in the last three simulated hours (the coverage rule)."""
        counted = positive = filled = 0
        net = 0.0
        for book_id in books:
            memory = self.memory.get((validator, book_id))
            if memory is None:
                continue
            filled += memory["last_fill"] is not None and memory["last_fill"] > timestamp - SKILL_WINDOW
            value, hurdle = self._alpha(memory)
            if hurdle > 0 and abs(value) >= hurdle:
                counted += 1
                positive += value > 0
                net += value
        return counted, positive, net, filled

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
            counted, positive, net, filled = self._skill_gate(validator, cls.books, timestamp)
            parts.append(f"skill books {counted}/{bar} ({positive} positive), net alpha {net:+.2f}, "
                         f"filled in 3 h {filled}/{len(cls.books)} (coverage needs "
                         f"{math.ceil(SKILL_COVERAGE * len(cls.books))})")
            classes_line.append(f"{cls.name}: " + ", ".join(parts))
        stats = self._stats(validator)
        s3, market = self._s3_factor(stats["takers"])
        bt.logging.info(
            f"SCORE CARD | avg return {100 * sum(returns.values()) / len(returns):+.4f}% | {books} | "
            f"maker fills buy {stats[OrderDirection.BUY]} / sell {stats[OrderDirection.SELL]} | "
            f"maker fees {stats['fees']:+.4f} | taker fills {stats['taken']} | "
            f"rejected {self._stats(None).get('rejected', 0)} | market share {market:.2f}, S3 {s3:.2f}, "
            f"top-{P11_TOPK} miner takers {self._top_taker_share(stats['takers']):.2f}"
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
        if fundamental and fundamental > 0:
            # The gap's own `gap_window` average, taken before this round's gap is folded in.
            previous = memory["gap_mean"]
            weight   = 1 - math.exp(-memory["elapsed"] / self.gap_window) if memory["elapsed"] > 0 else 0.0
            memory["gap_mean"] = gap if previous is None else previous + weight * (gap - previous)
            memory["gap_dev"]  = 0.0 if previous is None else gap - previous
            if self.center_fundamental:
                gap = memory["gap_dev"]
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
        its own fills over the last 24 hours, and keeps any `pace_period` within `pace_burst` times an even
        share of the day, so the budget lasts the whole day instead of running out mid-run.

        With clock hours, on the 23 October network run every book used its hour's share about three minutes in
        and then sat dark for the rest of the hour; five-minute periods spend the same budget at the same rate.
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
        # it, and every fill spends its notional (onTrade). A five-minute period let a book spend the period's
        # budget in its first seconds and then wait out the rest; the bucket spreads the pauses over about a minute.
        rate     = self.turnover_margin * cap / DAY                    # QUOTE per simulation nanosecond
        capacity = rate * self.pace_bucket * 1e9
        now      = memory["timestamp"]
        if memory["tokens"] is None:
            memory["tokens"] = capacity
        else:
            memory["tokens"] = min(capacity, memory["tokens"] + rate * max(0, now - memory["token_time"]))
        memory["token_time"] = now
        return memory["tokens"] <= 0

    def _budget_size(self, book_id, memory, mid):
        """The quote size, in BASE, at which this book's fills would spend the daily volume allowance at its
        average rate: `budget_target` x the allowance per second over the maker fills per second while a quote
        rests. 0 (so the minimum order) until `fill_rate_min_time` seconds of quoting have measured the rate; no
        limit on a market without an allowance."""
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        if wealth <= 0:
            return float("inf")
        if memory["q_time"] < self.fill_rate_min_time or memory["q_fills"] <= 0:
            return 0.0
        allowance = self.turnover_cap * wealth / (DAY / 1e9)   # QUOTE per second
        fill_rate = memory["q_fills"] / memory["q_time"]        # maker fills per second while quoting
        return self.budget_target * allowance / fill_rate / mid

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
            memory["quoting"] = False
            return

        decimals = self.price_decimals(book_id)
        tick     = 10 ** -decimals
        # Half spread: wide enough for the mid's typical move over the markout horizon and for a whole tick of
        # edge on a coarse grid, scaled by the class's and the book's markout multipliers; `fee_weight` of the
        # maker fee widens it (the share the making credit nets) and a rebate narrows it, never below the tick floor.
        sigma = math.sqrt(memory["variance"] * self.vol_horizon) if memory["variance"] else 0.0
        floor = params["min_half_ticks"] * tick / mid
        half  = max(params["half_spread"], params["vol_mult"] * sigma, floor)
        mult  = self.spread_mult.get((validator, self._class_index(book_id)), 1.0) * memory["mult"]
        half  = max(floor, half * mult + self.fee_weight * fee_rate)

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
        # The fair price moves `lean` times the predicted move, within `max_lean` half spreads, the way every lean of
        # MyAgentVInput stayed inside its half spread: at the default one, a strong prediction brings the quote on
        # its side to the mid and the other a whole spread out, and no quote is clamped onto the opposite touch.
        # With the skews on top, the whole shift stays within the larger of the two caps, so no quote passes the mid.
        lean  = params["max_lean"] * half
        shift += max(-lean, min(lean, params["lean"] * pred / 1e4))
        total = max(self.max_skew, params["max_lean"]) * half
        shift = max(-total, min(total, shift))

        # Never cross the book, with `cross_guard_ticks` of room: the order lands after the response delay, and a
        # post-only quote the touch has moved onto is refused after its predecessor was already cancelled.
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
        # the market runs over, so it is replaced, unless within `requote_toward_ticks`, where its queue place is
        # worth more than the tick.
        away   = max(self.requote_frac * half * mid, self.requote_ticks * tick)
        toward = max(self.requote_toward_frac * half * mid, params["requote_toward_ticks"] * tick)
        expiry  = params["expiry_period"]
        resting = 0
        # Far from balanced, quote only the side that brings inventory back. The cancels go ahead of the new order
        # in the response, so the funds a side's own resting orders hold count as free for its replacement.
        if inventory >= self.inventory_band:
            self._cancel(response, book_id, bids)
        else:
            free = account.quote_balance.free + sum(o.quantity * o.price for o in bids)
            resting += self._quote_side(response, book_id, bids, bid_price, OrderDirection.BUY, quantity,
                                        min_quantity, volume_decimals, away, toward, free, expiry)
        if inventory <= -self.inventory_band:
            self._cancel(response, book_id, asks)
        else:
            free = self._sellable(account) + sum(o.quantity for o in asks)
            resting += self._quote_side(response, book_id, asks, ask_price, OrderDirection.SELL, quantity,
                                        min_quantity, volume_decimals, away, toward, free, expiry)
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
        resting quote; the position it opens is unwound by the quotes, which lean and skew with it."""
        edge = params["take_edge_bps"]
        if edge <= 0:
            return
        best_bid, best_ask = book.bids[0], book.asks[0]
        half_bps = (best_ask.price - best_bid.price) / 2 / memory["mid"] * 1e4
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
        response.limit_order(
            book_id=book_id,
            direction=direction,
            quantity=size,
            price=self._nudged(book_id, level.price),
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

    def _nudged(self, book_id, price):
        """`price` plus a millionth of a tick when `grid_nudge` is on: the engine truncates the 128-bit decimal of
        the float it receives onto the grid, and 300.07 arrives as 300.06999..., a tick lower. The nudge keeps the
        float above its grid point without reaching the next one."""
        return price + 1e-6 * 10 ** -self.price_decimals(book_id) if self.grid_nudge else price

    @staticmethod
    def _on_grid(price, decimals, down):
        """`price` on the tick grid, rounded down for a bid and up for an ask, so rounding never moves a quote
        closer to the fair price. The 1e-9 keeps float noise (29.99 x 100 = 2998.9999...) from costing a tick."""
        units = price * 10 ** decimals
        units = math.floor(units + 1e-9) if down else math.ceil(units - 1e-9)
        return round(units / 10 ** decimals, decimals)

    def _quote_side(self, response, book_id, orders, target, direction, quantity, min_quantity, volume_decimals,
                    away, toward, free, expiry):
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
            self._place(response, book_id, direction, size, target, expiry)
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

    def _place(self, response, book_id, direction, quantity, price, expiry):
        """Places a post-only limit order expiring after `expiry` ns, so it can only ever rest as a maker, at the
        price it was given (`_nudged`)."""
        response.limit_order(
            book_id=book_id,
            direction=direction,
            quantity=quantity,
            price=self._nudged(book_id, price),
            stp=STP.CANCEL_BOTH,
            postOnly=True,
            timeInForce=TimeInForce.GTT,
            expiryPeriod=int(expiry),
        )


if __name__ == "__main__":
    launch(MyAgent10_23IovC)
