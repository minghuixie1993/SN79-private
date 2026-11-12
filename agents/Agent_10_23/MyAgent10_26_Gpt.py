# SPDX-License-Identifier: MIT
"""
MyAgent10_26_Gpt: MyAgent10_23_9Gpt rewritten as one flat, readable class (26 October).

9Gpt was ten classes stacked in one file (MyAgentV3 -> 10_23_Gpt -> 1Gpt -> 2Gpt -> 3Gpt -> 4Gpt -> 5Gpt -> 6Gpt ->
7Gpt -> 9Gpt), each overriding a few methods of the one below, with context variables and proxy objects passing
values between them. This file does the same thing top to bottom in a single class. With the live parameter set it
sends exactly 9Gpt's instructions (checked offline against 9Gpt and on recorded mainnet rounds).

What one round does, per book (`respond` -> `_quote_book`):
  1. memory      `_remember`: mid, volatility, decayed inputs (order-flow imbalance, taker flow, 5 s / 300 s momentum)
  2. pending     the last reply's cancels count as done; when replies land a round late, a side whose last order
                 has not reached the book yet gets no second one
  3. shield      `shield_pre_s` before and `shield_post_s` after each 30 s fundamental reseed: cancel everything
  4. pause       maker fee above `max_fee_rate`, or the book's volume ahead of its pace through the daily cap: cancel
  5. price       `_prices`: fair = mid moved by the predicted 20 s move (capped), a half spread either side, skewed
                 away from inventory and from the side that filled more, kept off the opposite touch
  6. size        `_size`: size_fraction x wealth (x quiet_size_mult in the quiet 5-28 s window), cut by inventory,
                 the per-order cap, the inventory band and the funds
  7. plan        `_plan_side`: keep the resting quote if it is still close and legal, else replace or cancel it
  8. send        each side's cancel before its order, at most `max_instructions_per_book` instructions
After the books: every own fill 20 s old is marked against the mid and moves the class and book spread multipliers
(`_settle_markouts`), and a one-line CARD is printed every minute.

Parameters: every one can be passed in --params; a per-class one as `<name>_c<k>` for class k (0, 1, 2 = simulation_0,
1, 2). The defaults below are 9Gpt's effective defaults, quirks included (4Gpt's max_lean 0.5 and per-class
coef_fund, V3's class 1 settings for every later class).

Removed from 9Gpt (all off or without effect on the live set): shield_widen and shield_side, toxic_widen (it never
acted: it read a taker id parsed trades do not have), skew_convex, V3's hourly pacing (replaced by 2Gpt's 5-minute
periods), V3's expiry_period (replaced by expiry_s), added_delay_s, lazy_mode=auto detection (lazy books are always
read through _TopBook, which works for parsed books too), 6Gpt's seeding from the 5Gpt state file, and the reporting
only bookkeeping (order life tracking, rejection attribution, counterparty tables, the several PLAN CARD sections).
The learned state file is still MyAgent10_23_6Gpt_state.json, in the same format, so 9Gpt's learned spreads carry over.

Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_26_Gpt.py --port 8888 --agent_id 0
"""
import contextvars
import gc
import json
import math
import os
import sys
import time
from collections import namedtuple
from pathlib import Path

import bittensor as bt
from taos.common.agents import launch
from taos.im.agents import FinanceAgent, UnifiedAccount
from taos.im.protocol.models import OrderDirection, STP, TimeInForce

BUY, SELL = OrderDirection.BUY, OrderDirection.SELL
KEEP, REPLACE, CANCEL_ONLY, SKIP = "keep", "replace", "cancel_only", "skip"

# ── Parameters ───────────────────────────────────────────────────────────────────────────────────────────────────

# Per-class parameters: class 0's defaults (a plain `<name>` overrides them on every class without its own default).
CLASS_PARAMS = {
    # half spread = max(half_spread, vol_mult x sigma(20 s), min_half_ticks ticks)
    "half_spread":          0.0006,
    "vol_mult":             1.3,
    "min_half_ticks":       1.5,
    # predicted 20 s move, bps per unit of each input; fair = mid x (1 + lean x pred), capped at max_lean half spreads
    "lean":                 2.0,
    "max_lean":             0.5,
    "coef_fund":            0.009,
    "coef_ofi":             1.5,
    "coef_flow":            0.26,
    "coef_imb5":            1.1,
    "coef_imb1":            0.27,
    "coef_mom5":            0.15,
    "coef_mom300":          -0.04,
    # skew away from the side that filled more (fill_skew) and from inventory (skew), both in half spreads
    "fill_skew":            0.3,
    "fill_confidence":      3.0,
    "skew":                 4.0,
    "max_inv_ratio":        0.25,   # inventory gap (share of wealth) beyond which only the reducing side is quoted
    # size and order handling
    "size_fraction":        0.006,
    "requote_toward_ticks": 0.0,
    "cross_guard_ticks":    3.0,
    "expiry_s":             60.0,
    # reseed shield and quiet-window size, in sim seconds of the reseed cycle
    "shield_pre_s":         0.0,
    "shield_post_s":        0.0,
    "quiet_size_mult":      1.0,
    "quiet_from_s":         5.0,
    "quiet_to_s":           28.0,
}
# Class defaults that beat a plain `<name>` (only `<name>_c<k>` overrides them): V3's class 1 settings, which every
# later class inherits, and 4Gpt's mainnet coef_fund (simulation_2's fundamental sits ~600 bps off and gets none).
_LATER = {"half_spread": 0.0005, "vol_mult": 1.5, "lean": 1.0, "coef_fund": 0.0035, "coef_ofi": 0.0, "coef_flow": 8.0,
          "coef_imb5": 0.0, "coef_imb1": 0.27, "coef_mom5": -0.2, "coef_mom300": 0.025, "size_fraction": 0.012,
          "requote_toward_ticks": 1.0, "cross_guard_ticks": 1.0, "expiry_s": 300.0}
CLASS_DEFAULTS = {1: dict(_LATER, coef_fund=0.006), 2: dict(_LATER, coef_fund=0.0), 3: _LATER}

# Book inputs a non-finite value would poison for good, and their neutral values.
MEMORY_INPUTS = {"variance": None, "ofi": 0.0, "flow": 0.0, "mom5": 0.0, "mom300": 0.0, "pred": 0.0}
PHASES = ((0.0, 1.0, "0-1s"), (1.0, 5.0, "1-5s"), (5.0, 28.0, "5-28s"), (28.0, 30.0, "28-30s"))
STATE_FILE = "MyAgent10_23_6Gpt_state.json"      # 9Gpt's file name, so its learned state carries over
LEVEL_DEPTH = 5                                  # the deepest book level the strategy reads
Level = namedtuple("Level", "price quantity")

# The accounts of the request being served, per thread or task, so concurrent requests never mix them.
_REQUEST_ACCOUNTS = contextvars.ContextVar("my_agent_10_26_accounts", default=None)


class _TopBook:
    """The top levels of a lazily parsed book as plain lists, read from its raw wire dict (a LazyBook's levels take
    integer indexes only, so `book.bids[:5]` fails on them). Anything else comes from the lazy book itself."""
    __slots__ = ("bids", "asks", "_raw", "_book")

    def __init__(self, book, depth):
        raw = book._raw
        self._raw = raw
        self._book = book
        self.bids = [Level(level["p"], level["q"]) for level in (raw.get("b") or [])[:depth]]
        self.asks = [Level(level["p"], level["q"]) for level in (raw.get("a") or [])[:depth]]

    def __getattr__(self, name):          # fundamental, events, ...
        return getattr(self._book, name)


class _BookReply:
    """The instructions for one book, collected in order and then sent. With the pending-aware planner on, the
    placements on `skip` sides are dropped, and the ids cancelled and prices placed are remembered for next round."""

    def __init__(self, skip=()):
        self.calls = []
        self.skip = set(skip)
        self.cancelled = set()
        self.placed = {}
        self.dropped = 0

    def cancel(self, orders):
        if orders:
            ids = [o.id for o in orders]
            self.calls.append(("cancel", ids))
            self.cancelled.update(ids)

    def place(self, **order):
        side = int(order["direction"])
        if side in self.skip:
            self.dropped += 1
            return
        self.placed[side] = float(order["price"])
        self.calls.append(("place", order))

    def send(self, response, book_id):
        for kind, payload in self.calls:
            if kind == "cancel":
                response.cancel_orders(book_id, payload)
            else:
                response.limit_order(**payload)


class MyAgent10_26_Gpt(FinanceAgent):
    """Predictive passive market maker on every book; see the module docstring."""

    # ── Setup ────────────────────────────────────────────────────────────────────────────────────────────────

    def initialize(self):
        """Reads the global parameters; every value passed via --params arrives as a float."""
        g = lambda name, default: float(getattr(self.config, name, default))
        on_miner = _running_under_miner()
        # prediction and volatility
        self.vol_window      = g("vol_window", 60.0)
        self.vol_horizon     = g("vol_horizon", 20.0)
        self.max_pred_bps    = g("max_pred_bps", 15.0)
        self.imbalance_depth = int(g("imbalance_depth", 5))
        self.fill_window     = g("fill_window", 1800.0)
        self.max_skew        = g("max_skew", 0.8)
        self.quantity        = g("quantity", 1.0)
        # requoting: a resting quote is kept within requote_frac of the half spread away from its target (at least
        # requote_ticks) and requote_toward_frac toward it (at least requote_toward_ticks)
        self.requote_frac        = g("requote_frac", 0.3)
        self.requote_toward_frac = g("requote_toward_frac", 0.1)
        self.requote_ticks       = int(g("requote_ticks", 1))
        self.keep_size_tolerance = max(0.0, g("keep_size_tolerance", 0.3))
        # sizing limits
        self.max_order_fraction        = max(0.0, g("max_order_fraction", 0.02))
        self.inventory_sizing          = max(0.0, min(1.0, g("inventory_sizing", 0.8)))
        self.min_inventory_size_factor = max(0.0, min(1.0, g("min_inventory_size_factor", 0.2)))
        self.post_fill_band            = bool(int(g("post_fill_band", 1)))
        self.max_instructions_per_book = int(g("max_instructions_per_book", 5))
        # fees: the validator nets only 0.005 of a maker fee from making credit, so the spread carries that share
        self.fee_weight   = max(0.0, g("fee_weight", 0.005))
        self.max_fee_rate = g("max_fee_rate", 0.02)
        self.grid_nudge   = max(0.0, g("grid_nudge", 1e-6))     # of a tick, so the engine's truncation keeps the tick
        # pacing through the validator's daily volume cap (turnover_cap x miner wealth per book per day)
        self.turnover_cap    = g("turnover_cap", 10.0)
        self.turnover_margin = g("turnover_margin", 0.9)
        self.pace_burst      = g("pace_burst", 2.0)
        self.pace_period_s   = max(1.0, g("pace_period_s", 300.0))
        # markout controllers: each own fill is marked markout_horizon later; the class multiplier (adapt_min ..
        # adapt_max) and each book's (1 .. book_mult_max) move toward the width where fills mark out at the target
        self.markout_horizon    = int(g("markout_horizon", 20_000_000_000))
        self.markout_target_bps = g("markout_target_bps", 3.0)
        self.markout_alpha      = g("markout_alpha", 0.02)
        self.adapt_rate         = g("adapt_rate", 0.002)
        self.adapt_scale_bps    = g("adapt_scale_bps", 10.0)
        self.adapt_min          = g("adapt_min", 0.7)
        self.adapt_max          = max(self.adapt_min, g("adapt_max", 1.25))
        self.book_adapt_rate    = max(0.0, g("book_adapt_rate", 0.02))
        self.book_mult_max      = max(1.0, g("book_mult_max", 1.25))
        self.book_markout_alpha = min(1.0, max(0.0, g("book_markout_alpha", 0.1)))
        # latency: each quote stays latency_guard x the mid's typical move over latency_s from the opposite touch
        self.latency_s     = max(0.0, g("latency_s", 0.72 if on_miner else 0.05))
        self.latency_guard = max(0.0, g("latency_guard", 1.0))
        # the fundamental reseed interval (from the validator's config unless reseed_s is given)
        self._reseed_given = getattr(self.config, "reseed_s", None) is not None
        self.reseed_s      = max(1.0, g("reseed_s", 30.0))
        # pending-aware planning: lagged when > lag_threshold of the last reply's cancels are still listed (median
        # over lag_window rounds)
        self.pending_aware = bool(int(g("pending_aware", 1)))
        self.lag_window    = max(1, int(g("lag_window", 10)))
        self.lag_threshold = g("lag_threshold", 0.2)
        # learned state across restarts
        self.persist_state     = bool(int(g("persist_state", 1)))
        self.persist_every_s   = max(5.0, g("persist_every_s", 120.0))
        self.persist_max_gap_s = g("persist_max_gap_s", 3600.0)
        self.report_interval   = int(g("report_interval", 60_000_000_000))

        # The miner reads lazy_load from this config on every request; lazy books are read through _TopBook.
        lazy_mode = str(getattr(self.config, "lazy_mode", "1")).strip().lower()
        self.config.lazy_load = lazy_mode not in ("0", "0.0", "false", "off")

        # One instance serves every validator, each with its own simulation, so state is keyed by validator.
        self.memory           = {}    # (validator, book) -> book memory
        self.pending_markouts = {}    # validator -> [(fill ns, book, sign, price, notional, mid at fill)]
        self.markout_stats    = {}    # (validator, class) -> realized / captured / notional / n / average
        self.spread_mult      = {}    # (validator, class) -> class spread multiplier
        self.next_report      = {}
        self.class_of_book    = {}
        self.class_params     = {}
        self.stats            = {}    # validator -> counters for the CARD
        self.phase_stats      = {}    # (validator, class, phase) -> realized / captured / notional / n
        self.reply_times      = {}
        self._lag_shares      = {}    # validator -> recent shares of last reply's cancels still listed
        self._lag_round       = {}    # validator -> [listed, cancelled] in the round being answered
        self._layout = self._sim_id = self._last_ts = None
        self._restore, self._restored, self._last_save = {}, set(), {}
        self._state_file = Path(getattr(self, "output_dir", ".")) / STATE_FILE
        # A parsed state is hundreds of thousands of small objects; default thresholds ran a full collection over
        # them every few states, which took the response from ~0.05 s to ~0.5 s.
        gc.set_threshold(50_000, 20, 100)
        p = self._params_index(0)
        bt.logging.info(f"10_26_Gpt: lazy_load {int(self.config.lazy_load)}, latency {self.latency_s:.2f} s, "
                        f"pending-aware {int(self.pending_aware)}, "
                        f"c0 shield {p['shield_pre_s']:g}/{p['shield_post_s']:g} s, "
                        f"quiet x{p['quiet_size_mult']:g}, persist {int(self.persist_state)}")

    def _params_index(self, index):
        """Class `index`'s parameters: `<name>_c<k>` from --params, else the class default, else `<name>`, else
        class 0's default."""
        params = self.class_params.get(index)
        if params is None:
            later = CLASS_DEFAULTS.get(min(index, 3), {}) if index > 0 else {}
            params = {}
            for name, default in CLASS_PARAMS.items():
                for value in (getattr(self.config, f"{name}_c{index}", None), later.get(name),
                              getattr(self.config, name, None), default):
                    if value is not None:
                        params[name] = float(value)
                        break
            # the band set as max_inv_ratio(_c<k>) is held above 0; V3's own name for it is inventory_band
            if getattr(self.config, f"max_inv_ratio_c{index}", None) is not None or \
                    getattr(self.config, "max_inv_ratio", None) is not None:
                params["max_inv_ratio"] = max(1e-6, params["max_inv_ratio"])
            elif getattr(self.config, "inventory_band", None) is not None:
                params["max_inv_ratio"] = float(self.config.inventory_band)
            self.class_params[index] = params
        return params

    def _class_index(self, book_id):
        """Index of the asset class `book_id` belongs to (0 when no class lists it)."""
        index = self.class_of_book.get(int(book_id))
        if index is None:
            index = next((k for k, cls in enumerate(self.asset_classes()) if int(book_id) in cls.books), 0)
            self.class_of_book[int(book_id)] = index
        return index

    def _count(self, validator, key, amount=1):
        stats = self.stats.setdefault(validator, {})
        stats[key] = stats.get(key, 0) + amount

    # ── The round ────────────────────────────────────────────────────────────────────────────────────────────

    def handle(self, state):
        """The framework's handling (update + respond), timed for the card."""
        start = time.perf_counter()
        response = super().handle(state)
        times = self.reply_times.setdefault(getattr(getattr(state, "dendrite", None), "hotkey", None), [])
        times.append(time.perf_counter() - start)
        del times[:-300]
        return response

    def update(self, state) -> None:
        """The framework's refresh, then this request's own accounts, built from its state rather than read back
        from `self.accounts`, which a concurrent request may already have replaced."""
        super().update(state)
        try:
            raw = (state.accounts or {}).get(self.uid, {})
            _REQUEST_ACCOUNTS.set({b: a if isinstance(a, UnifiedAccount) else UnifiedAccount(a)
                                   for b, a in raw.items()})
        except Exception:
            _REQUEST_ACCOUNTS.set(None)

    def _accounts(self):
        accounts = _REQUEST_ACCOUNTS.get()
        return accounts if accounts is not None else self.accounts

    def respond(self, state):
        """One response covering every book."""
        validator = getattr(getattr(state, "dendrite", None), "hotkey", None)
        self._lag_round[validator] = [0, 0]
        self._sync_config(state)
        sim_id = getattr(getattr(state, "config", None), "simulation_id", None)
        if self.persist_state and validator not in self._restored:
            self._restored.add(validator)
            self._load_state(validator, sim_id, state.timestamp)

        response = self.make_response()
        accounts = self._accounts()
        quoted = 0
        for book_id, book in (state.books or {}).items():
            if not book.bids or not book.asks or book_id not in accounts:
                continue
            try:
                book = self._plain(book)
                memory = self._remember(validator, book_id, book, state.timestamp)
                self._quote_book(response, validator, book_id, book, memory, accounts[book_id])
                quoted += 1
            except Exception as ex:
                # A problem on one book must not cost the instructions for every other book.
                bt.logging.error(f"BOOK {book_id} | quoting failed: {ex}")
        try:
            self._settle_markouts(validator, state.timestamp)
        except Exception as ex:
            bt.logging.error(f"markout check failed: {ex}")
        self._report(validator, state.timestamp, quoted)

        if self.persist_state and time.time() - self._last_save.get(validator, 0.0) >= self.persist_every_s:
            self._last_save[validator] = time.time()
            self._save_state(validator, sim_id, state.timestamp)
        listed, cancelled = self._lag_round.pop(validator, [0, 0])
        if cancelled:
            shares = self._lag_shares.setdefault(validator, [])
            shares.append(listed / cancelled)
            del shares[:-self.lag_window]
        return response

    @staticmethod
    def _plain(book):
        """A lazily parsed book as a _TopBook view; any other book unchanged."""
        if isinstance(getattr(book, "_raw", None), dict) and not isinstance(getattr(book, "bids", None), list):
            return _TopBook(book, LEVEL_DEPTH)
        return book

    def lagged(self, validator):
        """True while the validator applies replies a round late."""
        shares = sorted(self._lag_shares.get(validator) or [])
        return bool(shares) and shares[len(shares) // 2] > self.lag_threshold

    # ── 1. Book memory ───────────────────────────────────────────────────────────────────────────────────────

    def _remember(self, validator, book_id, book, timestamp):
        """This book's memory, carried forward to the new state: the volatility estimate, the decayed fill balance and
        the decayed predictive inputs. A timestamp earlier than the last one means a new simulation: start over."""
        best_bid, best_ask = book.bids[0], book.asks[0]
        mid = (best_bid.price + best_ask.price) / 2
        memory = self.memory.get((validator, book_id))
        if memory is None or timestamp < memory["timestamp"]:
            memory = {"timestamp": timestamp, "mid": mid, "variance": None, "start_base": None,
                      "fills": {BUY: 0.0, SELL: 0.0}, "pvolume": {},
                      "l1": None, "ofi": 0.0, "flow": 0.0, "mom5": 0.0, "mom300": 0.0, "pred": 0.0}
            self.memory[(validator, book_id)] = memory
        elapsed = (timestamp - memory["timestamp"]) / 1e9
        l1 = (best_bid.price, best_bid.quantity, best_ask.price, best_ask.quantity)
        if elapsed > 0 and mid > 0 and memory["mid"] > 0:
            ret = math.log(mid / memory["mid"])
            # exponentially weighted squared log return of the mid, per second of simulation time
            weight = 1 - math.exp(-elapsed / self.vol_window)
            rate = ret ** 2 / elapsed
            previous = memory["variance"]
            memory["variance"] = rate if previous is None else previous + weight * (rate - previous)
            decay = math.exp(-elapsed / self.fill_window)
            for side in memory["fills"]:
                memory["fills"][side] *= decay
            # inputs are sums decayed over a few seconds, in units of the top-5 depth so books compare
            depth = sum(lv.quantity for lv in book.bids[:5]) + sum(lv.quantity for lv in book.asks[:5])
            d5, d20, d300 = math.exp(-elapsed / 5), math.exp(-elapsed / 20), math.exp(-elapsed / 300)
            ofi = self._order_flow_imbalance(memory["l1"], l1)
            memory["ofi"] = memory["ofi"] * d5 + (ofi / depth if depth > 0 else 0.0)
            memory["flow"] = memory["flow"] * d20 + (self._taker_flow(book) / depth if depth > 0 else 0.0)
            memory["mom5"] = memory["mom5"] * d5 + ret * 1e4
            memory["mom300"] = memory["mom300"] * d300 + ret * 1e4
        memory["elapsed"] = elapsed
        memory["timestamp"], memory["mid"], memory["l1"] = timestamp, mid, l1
        restore = self._restore.get(validator, {}).pop(int(book_id), None)
        if restore:
            memory.update(restore)
        return memory

    @staticmethod
    def _order_flow_imbalance(previous, current):
        """Net BASE added to the bid side minus the ask side at the touch since the previous state (Cont, Kukanov and
        Stoikov). 0 without a previous state."""
        if previous is None:
            return 0.0
        pbb, pqb, pba, pqa = previous
        bb, qb, ba, qa = current
        return ((qb if bb >= pbb else 0.0) - (pqb if bb <= pbb else 0.0)
                - (qa if ba <= pba else 0.0) + (pqa if ba >= pba else 0.0))

    @staticmethod
    def _taker_flow(book):
        """Signed taker BASE traded on the book since the previous state: buys positive, sells negative."""
        raw = getattr(book, "_raw", None)
        if isinstance(raw, dict):
            return sum((e["q"] if e["s"] == 0 else -e["q"]) for e in raw.get("e") or [] if e.get("y") == "t")
        return sum((e.quantity if e.side == 0 else -e.quantity) for e in book.events or [] if e.type == "t")

    # ── 2-4. Quoting one book: pending view, shield, pause ───────────────────────────────────────────────────

    def _quote_book(self, response, validator, book_id, book, memory, account):
        """Quotes one book. With `pending_aware`, the last reply's still-listed cancels are left out of the orders
        the plan sees, and in a lagged regime a side whose last placement is not on the book yet gets none."""
        orders, skip = list(account.orders), set()
        if self.pending_aware:
            cancelled = memory.get("pend_cancel") or set()
            listed = {o.id for o in account.orders if o.id in cancelled}
            counts = self._lag_round.get(validator)
            if counts is not None:
                counts[0] += len(listed)
                counts[1] += len(cancelled)
            orders = [o for o in account.orders if o.id not in listed]
            placed = memory.get("pend_place") or {}
            if self.lagged(validator) and placed:
                tick = 10 ** -self.price_decimals(book_id)
                skip = {side for side, price in placed.items()
                        if not any(int(o.side) == side and abs(o.price - price) < tick / 2 for o in orders)}
            if listed:
                self._count(validator, "pending_hidden", len(listed))
        reply = _BookReply(skip)
        for key, neutral in MEMORY_INPUTS.items():
            value = memory.get(key)
            if value is not None and not math.isfinite(value):
                memory[key] = neutral
        try:
            self._quote(reply, validator, book_id, book, memory, account, orders)
        except (ValueError, ArithmeticError) as ex:
            reply.cancel(orders)
            bt.logging.warning(f"BOOK {book_id} | non-finite quote input ({ex}); quotes cancelled")
        finally:
            reply.send(response, book_id)
            if self.pending_aware:
                memory["pend_cancel"], memory["pend_place"] = reply.cancelled, reply.placed
                if reply.dropped:
                    self._count(validator, "pending_skip", reply.dropped)

    def _phase(self, timestamp):
        """Sim seconds since the last reseed boundary."""
        return (timestamp / 1e9) % self.reseed_s

    def _quote(self, reply, validator, book_id, book, memory, account, orders):
        index = self._class_index(book_id)
        p = self._params_index(index)
        phase = self._phase(memory["timestamp"])
        # 3. reseed shield: quotes resting across a reseed are run over at once, so none rest there
        if (p["shield_pre_s"] > 0 or p["shield_post_s"] > 0) and \
                (phase >= self.reseed_s - p["shield_pre_s"] - 1e-9 or phase < p["shield_post_s"] - 1e-9):
            reply.cancel(orders)
            self._count(validator, f"shield_c{index}")
            return
        bids = [o for o in orders if o.side == BUY]
        asks = [o for o in orders if o.side == SELL]
        mid = memory["mid"]
        wealth = account.base_balance.total * mid + account.quote_balance.total if math.isfinite(mid) else math.nan
        if not (math.isfinite(mid) and mid > 0 and math.isfinite(wealth) and wealth > 0):
            reply.cancel(bids + asks)
            return
        # every round, so the decayed inputs stay current even while the book is not quoted
        pred = self._predict(book, orders, memory, p)
        # 4. pause on a high maker fee, or while the book's volume runs ahead of its pace through the daily cap
        fee_rate = account.fees.maker_fee_rate if account.fees is not None else 0.0
        reason = "fee" if fee_rate > self.max_fee_rate else self._pace_reason(book_id, account, memory)
        if reason:
            reply.cancel(bids)
            reply.cancel(asks)
            self._count(validator, f"paused_{reason}_c{index}")
            return
        self._count(validator, f"active_c{index}")
        size_fraction = p["size_fraction"]
        if p["quiet_size_mult"] != 1.0 and p["quiet_from_s"] - 1e-9 <= phase < p["quiet_to_s"] - 1e-9 \
                and size_fraction > 0:
            size_fraction *= max(0.0, p["quiet_size_mult"])
        inventory = self._inventory_gap(account, memory, mid, wealth)
        if not math.isfinite(inventory):
            reply.cancel(bids + asks)
            return
        prices = self._prices(validator, book_id, book, memory, p, pred, fee_rate, wealth, inventory, size_fraction)
        if prices is None:
            return
        bid_price, ask_price, ctx, quantity = prices
        ctx.update(validator=validator, account=account)
        plans = [self._plan_side(BUY, bids, bid_price, quantity, ctx),
                 self._plan_side(SELL, asks, ask_price, quantity, ctx)]
        self._execute(reply, book_id, plans, validator)

    def _pace_reason(self, book_id, account, memory):
        """Why the book should pause for volume, or None: the validator's own traded_volume ("cap") or this agent's
        last 24 h ("day") at turnover_margin of the daily cap, or this period at pace_burst x its even share."""
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        if wealth <= 0:
            return None
        cap = self.turnover_cap * wealth
        period = memory["timestamp"] // int(self.pace_period_s * 1e9)
        per_day = 86_400 / self.pace_period_s
        volume = memory["pvolume"]
        for old in [i for i in volume if i <= period - per_day]:
            del volume[old]
        if (account.traded_volume or 0.0) >= self.turnover_margin * cap:
            return "cap"
        if sum(volume.values()) >= self.turnover_margin * cap:
            return "day"
        if volume.get(period, 0.0) >= self.pace_burst * cap / per_day:
            return "period"
        return None

    # ── 5. Prices ────────────────────────────────────────────────────────────────────────────────────────────

    def _predict(self, book, orders, memory, p):
        """Predicted move of the mid over the next 20 s, in bps, within +/- max_pred_bps (0 if not finite)."""
        mid = memory["mid"]
        fundamental = getattr(book, "fundamental", None)
        gap = (fundamental - mid) / mid * 1e4 if fundamental and fundamental > 0 else 0.0
        imb5, imb1 = self._imbalances(book, orders)
        pred = (p["coef_fund"] * gap + p["coef_ofi"] * memory["ofi"] + p["coef_flow"] * memory["flow"]
                + p["coef_imb5"] * imb5 + p["coef_imb1"] * imb1
                + p["coef_mom5"] * memory["mom5"] + p["coef_mom300"] * memory["mom300"])
        pred = max(-self.max_pred_bps, min(self.max_pred_bps, pred))
        if not math.isfinite(pred):
            pred = 0.0
        memory["pred"] = pred
        return pred

    def _imbalances(self, book, orders):
        """(bid - ask) / (bid + ask) volume over the top imbalance_depth levels and at the touch, own quotes left out;
        positive when bids are heavier."""
        own = {}
        for o in orders:
            key = (o.side, round(o.price, 10))
            own[key] = own.get(key, 0.0) + o.quantity

        def volume(levels, side, depth):
            return sum(max(0.0, lv.quantity - own.get((side, round(lv.price, 10)), 0.0)) for lv in levels[:depth])

        def ratio(depth):
            bid, ask = volume(book.bids, BUY, depth), volume(book.asks, SELL, depth)
            return (bid - ask) / (bid + ask) if bid + ask > 0 else 0.0
        return ratio(self.imbalance_depth), ratio(1)

    def _prices(self, validator, book_id, book, memory, p, pred, fee_rate, wealth, inventory, size_fraction):
        """(bid, ask, plan context, base quantity), or None when the two would cross."""
        mid = memory["mid"]
        decimals = self.price_decimals(book_id)
        tick = 10 ** -decimals
        # half spread: the larger of the class floor and the mid's typical 20 s move, never under min_half_ticks,
        # times the class and book multipliers, plus fee_weight of the maker fee
        sigma = math.sqrt(memory["variance"] * self.vol_horizon) if memory["variance"] else 0.0
        floor = p["min_half_ticks"] * tick / mid
        half = max(p["half_spread"], p["vol_mult"] * sigma, floor)
        mult = self.spread_mult.get((validator, self._class_index(book_id)), 1.0) * memory.get("book_mult", 1.0)
        half = max(floor, half * mult + self.fee_weight * fee_rate)
        # fair price: the mid leaned toward the prediction, at most max_lean half spreads
        cap = p["max_lean"] * half
        lean = max(-cap, min(cap, p["lean"] * pred / 1e4))
        fair = mid * (1 + lean)
        # skew away from the side that filled more and from inventory, at most max_skew half spreads; lean and skew
        # together stay within max(max_skew, max_lean) half spreads, so no quote crosses the mid
        size = size_fraction * wealth / mid if size_fraction > 0 else self.quantity
        shift = (- p["fill_skew"] * self._fill_balance(memory, size * mid, p["fill_confidence"]) * half
                 - p["skew"] * inventory * half)
        shift = max(-self.max_skew * half, min(self.max_skew * half, shift))
        total = max(self.max_skew, p["max_lean"]) * half
        if lean + shift > total or lean + shift < -total:
            shift = max(-total - lean, min(total - lean, shift))
        # never within the guard of the opposite touch: cross_guard_ticks, or the mid's typical move over the latency
        guard_ticks = p["cross_guard_ticks"]
        if memory.get("variance") and mid and self.latency_s > 0 and self.latency_guard > 0:
            latency_ticks = self.latency_guard * math.sqrt(memory["variance"] * self.latency_s) * mid / tick
            guard_ticks = max(guard_ticks, latency_ticks)
        guard = max(1.0, guard_ticks) * tick
        best_bid, best_ask = book.bids[0].price, book.asks[0].price
        bid = min(self._on_grid(fair * (1 + shift - half), decimals, True), round(best_ask - guard, decimals))
        ask = max(self._on_grid(fair * (1 + shift + half), decimals, False), round(best_bid + guard, decimals))
        if bid <= 0 or bid >= ask:
            return None
        volume_decimals = self.volume_decimals(book_id)
        step = 10 ** -volume_decimals
        minimum = round(self.min_order_size(book_id) + step, volume_decimals)   # the engine rounds a float down
        quantity = round(max(size, minimum), volume_decimals)
        ctx = {"book_id": book_id, "wealth": wealth, "inventory": inventory, "band": p["max_inv_ratio"],
               "minimum": minimum, "step": step, "volume_decimals": volume_decimals,
               "away": max(self.requote_frac * half * mid, self.requote_ticks * tick),
               "toward": max(self.requote_toward_frac * half * mid, p["requote_toward_ticks"] * tick),
               "best_bid": best_bid, "best_ask": best_ask, "guard": guard, "decimals": decimals}
        return bid, ask, ctx, quantity

    def _inventory_gap(self, account, memory, mid, wealth):
        """BASE held beyond the book's initial BASE, valued at mid, as a share of wealth; positive when long. An
        account without `initial` (exchange mode) uses the balance first seen in this simulation."""
        initial = getattr(account.base_balance, "initial", None)
        if initial is None:
            if memory["start_base"] is None:
                memory["start_base"] = account.base_balance.total
            initial = memory["start_base"]
        return (account.base_balance.total - initial) * mid / wealth if wealth > 0 else 0.0

    @staticmethod
    def _fill_balance(memory, quote_notional, confidence_quotes):
        """(buy - sell) / (buy + sell + confidence_quotes x quote notional) of the book's recent maker fills;
        positive when buys ran ahead, half strength after that many quotes' worth of fills."""
        buys, sells = memory["fills"][BUY], memory["fills"][SELL]
        total = buys + sells
        if total <= 0:
            return 0.0
        return (buys - sells) / (total + max(confidence_quotes, 0.0) * quote_notional)

    @staticmethod
    def _on_grid(price, decimals, down):
        """`price` on the tick grid, down for a bid and up for an ask (1e-9 absorbs float noise)."""
        units = price * 10 ** decimals
        units = math.floor(units + 1e-9) if down else math.ceil(units - 1e-9)
        return round(units / 10 ** decimals, decimals)

    # ── 6-8. Size, plan, send ────────────────────────────────────────────────────────────────────────────────

    def _plan_side(self, direction, orders, target, base_quantity, ctx):
        """(decision, orders to cancel, (quantity, price) or None) for one side."""
        if not math.isfinite(target) or target <= 0:
            return (CANCEL_ONLY, orders, None) if orders else (SKIP, [], None)
        inventory, band = ctx["inventory"], ctx["band"]
        adds = direction == BUY
        # beyond the band, the side that adds to the gap is not quoted at all
        if (inventory >= band) if adds else (inventory <= -band):
            return (CANCEL_ONLY, orders, None) if orders else (SKIP, [], None)

        def closer(o):          # positive when the resting quote sits closer to the fair price than its target
            return o.price - target if adds else target - o.price

        lone = orders[0] if len(orders) == 1 else None
        kept = (lone is not None and -ctx["away"] - 1e-9 <= closer(lone) <= ctx["toward"] + 1e-9
                and self._within_limits(direction, lone.quantity, lone.price, ctx))
        if not kept:
            quantity = self._size(direction, base_quantity, target, orders, ctx)
            if quantity is not None and self._crosses(direction, target, ctx):
                quantity = None
            if quantity is not None:
                return REPLACE, orders, (quantity, target)
            if not orders:
                return SKIP, [], None
            # no valid replacement: keep a lone quote still inside every limit that has not drifted toward fair
            kept = (lone is not None and closer(lone) <= ctx["toward"] + 1e-9
                    and self._within_limits(direction, lone.quantity, lone.price, ctx))
            if not kept:
                return CANCEL_ONLY, orders, None
        # a kept quote much larger than what would be placed now is replaced
        if self.keep_size_tolerance <= 0 or lone is None:
            return KEEP, [], None
        planned = self._size(direction, base_quantity, target, orders, ctx)
        if planned is None or lone.quantity <= planned * (1 + self.keep_size_tolerance) + 1e-12:
            return KEEP, [], None
        return REPLACE, orders, (planned, target)

    def _size(self, direction, base_quantity, price, cancelled, ctx):
        """The quantity after every limit, on the volume grid, or None when no legal order fits: smaller the more it
        adds to the inventory gap, at most max_order_fraction of wealth, within the band once filled, and affordable
        with this side's own resting orders cancelled first."""
        wealth, inventory, band, account = ctx["wealth"], ctx["inventory"], ctx["band"], ctx["account"]
        adds_gap = inventory if direction == BUY else -inventory
        factor = max(self.min_inventory_size_factor, 1.0 - self.inventory_sizing * max(0.0, adds_gap) / max(band, 1e-9))
        size = base_quantity * factor
        if self.max_order_fraction > 0:
            size = min(size, self.max_order_fraction * wealth / price)
        if self.post_fill_band:
            size = min(size, max(0.0, (band - adds_gap) * wealth / price))
        if direction == BUY:
            affordable = (account.quote_balance.free + sum(o.quantity * o.price for o in cancelled)) / price
        else:
            affordable = account.base_balance.free + sum(o.quantity for o in cancelled)
        size = min(size, max(0.0, affordable))
        quantity = round(math.floor(size / ctx["step"] + 1e-9) * ctx["step"], ctx["volume_decimals"])
        return None if quantity + 1e-12 < ctx["minimum"] else quantity

    def _within_limits(self, direction, quantity, price, ctx):
        """True when a quote respects the per-order cap and, filled in full, the inventory band."""
        wealth = ctx["wealth"]
        if self.max_order_fraction > 0 and quantity * price > self.max_order_fraction * wealth * (1 + 1e-9):
            return False
        if self.post_fill_band:
            adds_gap = ctx["inventory"] if direction == BUY else -ctx["inventory"]
            if adds_gap + quantity * price / wealth > ctx["band"] + 1e-9:
                return False
        return True

    @staticmethod
    def _crosses(direction, price, ctx):
        """The price would land within the guard of the opposite touch."""
        if direction == BUY:
            return price > round(ctx["best_ask"] - ctx["guard"], ctx["decimals"]) + 1e-12
        return price < round(ctx["best_bid"] + ctx["guard"], ctx["decimals"]) - 1e-12

    def _execute(self, reply, book_id, plans, validator):
        """Each side's cancel ahead of its order, unless the book would exceed max_instructions_per_book."""
        if sum(bool(cancel) + (order is not None) for _, cancel, order in plans) > self.max_instructions_per_book:
            return
        tick = 10 ** -self.price_decimals(book_id)
        expiry = int(self._params_index(self._class_index(book_id))["expiry_s"] * 1e9)
        for (decision, cancel, order), direction in zip(plans, (BUY, SELL)):
            reply.cancel(cancel)
            if order is not None:
                quantity, price = order
                reply.place(book_id=book_id, direction=direction, quantity=quantity,
                            price=price + self.grid_nudge * tick, stp=STP.CANCEL_BOTH, postOnly=True,
                            timeInForce=TimeInForce.GTT, expiryPeriod=expiry)
                self._count(validator, "placed")

    # ── Fills, markouts and the spread controllers ───────────────────────────────────────────────────────────

    def onTrade(self, event, validator=None):
        """Books every own fill into the pacing volume; for maker fills also the fill balance, the markout queue and
        the card. `event.side` is the taker's side, so the maker's side is the opposite one."""
        if self.uid not in (event.makerAgentId, event.takerAgentId):
            return
        memory = self.memory.get((validator, event.bookId))
        notional = event.price * event.quantity
        if memory is not None:
            period = event.timestamp // int(self.pace_period_s * 1e9)
            memory["pvolume"][period] = memory["pvolume"].get(period, 0.0) + notional
        if event.makerAgentId != self.uid:
            return
        side = SELL if event.side == BUY else BUY
        index = self._class_index(event.bookId)
        self._count(validator, f"fills_{'buy' if side == BUY else 'sell'}_c{index}")
        self._count(validator, f"notional_c{index}", notional)
        if event.takerAgentId is not None and event.takerAgentId < 0:
            self._count(validator, f"background_c{index}", notional)
        if memory is not None:
            memory["fills"][side] += notional
            self.pending_markouts.setdefault(validator, []).append(
                (event.timestamp, event.bookId, 1 if side == BUY else -1, event.price, notional, memory["mid"]))

    def onOrderRejected(self, event):
        super().onOrderRejected(event)
        self._count(None, "rejected")

    def _settle_markouts(self, validator, timestamp):
        """Marks every maker fill older than markout_horizon against its book's mid now, and steps the book's and the
        class's spread multipliers toward the width at which fills mark out at markout_target_bps."""
        pending = self.pending_markouts.get(validator)
        if not pending:
            return
        due = timestamp - self.markout_horizon
        keep = []
        for fill in pending:
            fill_time, book_id, sign, price, notional, fill_mid = fill
            memory = self.memory.get((validator, book_id))
            if memory is None or fill_time > memory["timestamp"]:
                continue                       # a new simulation started after the fill
            if fill_time > due:
                keep.append(fill)
                continue
            realized = sign * (memory["mid"] - price) / price * 1e4
            captured = sign * (fill_mid - price) / price * 1e4
            index = self._class_index(book_id)
            # the book's own multiplier only widens (1 .. book_mult_max)
            if self.book_adapt_rate > 0:
                average = memory.get("book_markout", self.markout_target_bps)
                memory["book_markout"] = average + self.book_markout_alpha * (realized - average)
                step = max(-1.0, min(1.0, (self.markout_target_bps - memory["book_markout"]) / self.adapt_scale_bps))
                mult = memory.get("book_mult", 1.0) * math.exp(self.book_adapt_rate * step)
                memory["book_mult"] = max(1.0, min(self.book_mult_max, mult))
            # the class multiplier follows the class's average markout (adapt_min .. adapt_max)
            stats = self.markout_stats.setdefault((validator, index),
                                                  {"realized": 0.0, "captured": 0.0, "notional": 0.0, "n": 0})
            stats["realized"] += realized * notional
            stats["captured"] += captured * notional
            stats["notional"] += notional
            stats["n"] += 1
            average = stats.get("average", self.markout_target_bps)
            stats["average"] = average + self.markout_alpha * (realized - average)
            step = max(-1.0, min(1.0, (self.markout_target_bps - stats["average"]) / self.adapt_scale_bps))
            mult = self.spread_mult.get((validator, index), 1.0) * math.exp(self.adapt_rate * step)
            self.spread_mult[(validator, index)] = max(self.adapt_min, min(self.adapt_max, mult))
            # by reseed phase, for the card
            phase = self._phase(fill_time) * 30.0 / self.reseed_s
            name = next((n for lo, hi, n in PHASES if lo <= phase < hi), PHASES[-1][2])
            s = self.phase_stats.setdefault((validator, index, name), [0.0, 0.0, 0.0, 0])
            s[0] += realized * notional
            s[1] += captured * notional
            s[2] += notional
            s[3] += 1
        self.pending_markouts[validator] = keep

    # ── The validator's config, learned state ────────────────────────────────────────────────────────────────

    def _sync_config(self, state):
        """Takes a new asset-class layout only when a new run starts (new simulation_id or the clock going back): a
        layout can be announced early, mid-run. Reads the reseed interval from the config unless reseed_s is given."""
        try:
            layout = tuple((cls.name, len(cls.books), min(cls.books), max(cls.books)) for cls in self.asset_classes())
        except Exception:
            return
        sim_id = getattr(getattr(self, "simulation_config", None), "simulation_id", None)
        timestamp = getattr(state, "timestamp", None)
        new_run = ((sim_id is not None and self._sim_id is not None and sim_id != self._sim_id)
                   or (timestamp is not None and self._last_ts is not None and timestamp < self._last_ts))
        if sim_id is not None:
            self._sim_id = sim_id
        if timestamp is not None:
            self._last_ts = timestamp
        if layout != self._layout and (self._layout is None or new_run):
            if self._layout is not None:
                self.class_of_book.clear()
                self.class_params.clear()
                self.spread_mult.clear()
                self.markout_stats.clear()
                bt.logging.warning(f"asset class layout changed {self._layout} -> {layout}; class settings reset")
            self._layout = layout
        if not self._reseed_given:
            interval = getattr(getattr(self, "simulation_config", None), "fp_seed_interval", None)
            if interval and interval > 0:
                self.reseed_s = interval / 1e9

    def _save_state(self, validator, sim_id, timestamp):
        """The class and book multipliers and their markout averages, to <output_dir>/MyAgent10_23_6Gpt_state.json."""
        try:
            data = json.loads(self._state_file.read_text()) if self._state_file.exists() else {}
            classes = {str(i): {"mult": m, "average": self.markout_stats.get((v, i), {}).get("average")}
                       for (v, i), m in self.spread_mult.items() if v == validator}
            books = {str(b): {k: mem[k] for k in ("book_mult", "book_markout") if k in mem}
                     for (v, b), mem in self.memory.items() if v == validator}
            data[str(validator)] = {"sim_id": sim_id, "timestamp": int(timestamp), "saved_at": time.time(),
                                    "classes": classes, "books": {b: x for b, x in books.items() if x}}
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self._state_file)
        except Exception as ex:
            bt.logging.warning(f"could not save learned state: {ex}")

    def _load_state(self, validator, sim_id, timestamp):
        """Restores the saved multipliers on the first round, if it is the same simulation and at most
        persist_max_gap_s behind; book values are applied when each book is first seen (_remember)."""
        try:
            if not self._state_file.exists():
                return
            saved = json.loads(self._state_file.read_text()).get(str(validator))
            if not saved:
                return
            same_sim = saved.get("sim_id") == sim_id
            behind = (int(timestamp) - int(saved.get("timestamp", 0))) / 1e9
            if not same_sim or behind < 0 or behind > self.persist_max_gap_s:
                bt.logging.info(f"saved state not restored (same simulation {same_sim}, {behind:.0f} s behind)")
                return
            for index, c in (saved.get("classes") or {}).items():
                self.spread_mult[(validator, int(index))] = max(self.adapt_min, min(self.adapt_max, float(c["mult"])))
                if c.get("average") is not None:
                    stats = self.markout_stats.setdefault((validator, int(index)),
                                                          {"realized": 0.0, "captured": 0.0, "notional": 0.0, "n": 0})
                    stats["average"] = float(c["average"])
            books = {}
            for b, x in (saved.get("books") or {}).items():
                values = {k: float(v) for k, v in x.items()}
                if "book_mult" in values:
                    values["book_mult"] = max(1.0, min(self.book_mult_max, values["book_mult"]))
                books[int(b)] = values
            self._restore[validator] = books
            bt.logging.info(f"restored learned state for {str(validator)[:8]} ({len(saved.get('classes') or {})} "
                            f"classes, {len(books)} books)")
        except Exception as ex:
            bt.logging.warning(f"could not restore learned state: {ex}")

    # ── Card ─────────────────────────────────────────────────────────────────────────────────────────────────

    def _report(self, validator, timestamp, quoted):
        """Once per report_interval, one CARD line. A clock going back means a new simulation: its fill statistics,
        pending markouts and class multipliers start from zero."""
        due = self.next_report.get(validator)
        if due is None or timestamp + self.report_interval < due:
            if due is not None:
                self.pending_markouts.pop(validator, None)
                self.stats.pop(validator, None)
                for table in (self.markout_stats, self.spread_mult, self.phase_stats):
                    for key in [k for k in table if k[0] == validator]:
                        del table[key]
            self.next_report[validator] = timestamp + self.report_interval
        if timestamp < self.next_report[validator] or not quoted:
            return
        self.next_report[validator] = timestamp + self.report_interval
        try:
            bt.logging.info(self._card(validator))
        except Exception as ex:
            bt.logging.error(f"card failed: {ex}")

    def _card(self, validator):
        s = self.stats.get(validator, {})
        times = sorted(self.reply_times.get(validator, []))
        reply = f"reply p50 {times[len(times) // 2] * 1000:.0f} ms" if times else "reply n/a"
        shares = sorted(self._lag_shares.get(validator) or [])
        late = shares[len(shares) // 2] if shares else 0.0
        rejected = self.stats.get(None, {}).get('rejected', 0)
        parts = [f"CARD | placed {s.get('placed', 0)}, rejected {rejected} | {reply} | "
                 f"{'LAGGED' if self.lagged(validator) else 'not lagged'} (late cancels {late:.2f}), "
                 f"hidden {s.get('pending_hidden', 0)}, skipped {s.get('pending_skip', 0)}"]
        try:
            names = [cls.name for cls in self.asset_classes()]
        except Exception:
            names = []
        for index, name in enumerate(names):
            m = self.markout_stats.get((validator, index), {})
            notional = s.get(f"notional_c{index}", 0.0)
            text = (f"{name}: fills {s.get(f'fills_buy_c{index}', 0)}/{s.get(f'fills_sell_c{index}', 0)}"
                    f" bg {100 * s.get(f'background_c{index}', 0.0) / notional if notional else 0:.0f}%")
            if m.get("notional"):
                quality = max(0.0, min(1.0, m["realized"] / m["captured"])) if m["captured"] > 0 else 0.0
                text += f", markout {m['realized'] / m['notional']:+.2f} bps q {quality:.2f}"
            mults = [mem.get("book_mult", 1.0) for (v, b), mem in self.memory.items()
                     if v == validator and self._class_index(b) == index]
            text += (f", spread x{self.spread_mult.get((validator, index), 1.0):.2f}"
                     f" books widened {sum(x > 1.0 + 1e-9 for x in mults)}")
            active = s.get(f"active_c{index}", 0)
            paused = {r: s.get(f"paused_{r}_c{index}", 0) for r in ("fee", "cap", "day", "period")}
            total = active + sum(paused.values()) + s.get(f"shield_c{index}", 0)
            if total:
                shielded = s.get(f'shield_c{index}', 0)
                text += f", active {100 * active / total:.0f}% shield {100 * shielded / total:.0f}%" + \
                        "".join(f" {r} {100 * n / total:.0f}%" for r, n in paused.items() if n)
            phases = []
            for _, _, phase in PHASES:
                ps = self.phase_stats.get((validator, index, phase))
                if ps and ps[2] > 0:
                    phases.append(f"{phase} {ps[3]} @ {ps[0] / ps[2]:+.2f}")
            if phases:
                text += " [" + ", ".join(phases) + "]"
            parts.append(text)
        return " | ".join(parts)


def _running_under_miner():
    """True inside taos/im/neurons/miner.py (also through agents/mainnet/recorded_miner.py)."""
    main = getattr(sys.modules.get("__main__"), "__file__", "") or ""
    return "--netuid" in sys.argv or Path(sys.argv[0]).name == "miner.py" or Path(main).name == "miner.py"


if __name__ == "__main__":
    launch(MyAgent10_26_Gpt)
