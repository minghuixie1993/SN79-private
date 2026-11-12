# SPDX-License-Identifier: MIT
"""
Inventory-skewed two-sided market maker, written as a starting point for local proxy testing.

On every book it keeps one passive bid and one passive ask around a fair price:
  - the fair price is the mid shifted toward the heavier side of the top `imbalance_depth` levels
    (`imbalance_skew`, a fraction of the half spread) and toward the book's published fundamental
    (`fundamental_weight` of the gap between the fundamental and the mid, measured against its own
    `gap_window` average), so quotes step out of the way of flow that is about to move the price,
  - quotes sit a half spread either side of it: the largest of `half_spread`, `vol_mult` times the
    mid's volatility over the validator's 20 s markout horizon (`vol_horizon`) and `min_half_ticks`
    ticks, plus the maker fee when the book charges one; prices are rounded away from the fair price
    onto the tick grid, so rounding never gives up edge (a tick is about 3 bps on the ~30 books),
  - both quotes shift away from the side the agent is already heavy on (`skew` half spreads per unit of
    inventory gap: the BASE held beyond the book's initial BASE, as a share of wealth), and away from the
    side whose fills have run ahead over the last `fill_window` (`fill_skew`, a fraction of the half spread);
    the combined shift is capped at `max_skew` of the half spread so neither quote ever reaches the fair
    price, and once the inventory gap leaves +/- `inventory_band` only the side that reduces it is quoted.
    The target is the initial holding, not half of wealth: initial allocations differ by book (about 6-50%
    of wealth in BASE on the multi-asset layout), and a 50% target kept buying on every book that started light,
  - each quote is `size_fraction` of the book's wealth (or `quantity` BASE when that is 0), so every
    book risks the same value whatever its price,
  - a resting quote that has drifted further from the fair price than its target is kept while within
    `requote_frac` of the half spread (never less than `requote_ticks` ticks), but one that has drifted
    toward the fair price is replaced once past `requote_toward_frac` of it, since that is the quote the
    market runs over; quotes expire after `expiry_period` so a forgotten one cannot be picked off for long,
  - quoting stops on a book while its maker fee rate exceeds `max_fee_rate`, while its own fills run ahead
    of an even pace through the validator's daily volume cap (`turnover_cap` x miner wealth, at most
    `pace_burst` times the hourly share in any hour), and once its traded volume reaches `turnover_margin`
    of that cap, past which the validator drops every instruction except cancels.

Parameters can differ by asset class: `<name>_c<k>` overrides `<name>` on the books of the k-th class
(0 = books 0-95, 1 = books 96-127 on the multi-asset layout), e.g. `half_spread_c1=0.001`. Class 1 starts with
no imbalance or fundamental lean: replayed over a 15-hour local run, neither predicted its mid, while on
class 0 the gap to the fundamental did (20 s correlation about +0.12, the mid closing about 3% of the gap).
Any later class starts from class 1's settings, since neither lean has been measured there.
The raw gap also carries a per-book offset that lasts the whole run (up to +/-3 bps of lean at the default
weight), which would tilt a book's quotes to one side for good; measured against its own recent average the
gap keeps nearly all of its predictive power (+0.118) and loses the offset.

Making credit is 2 x min(buy, sell) of the spread captured on each book, net of fees, scaled by how much of
it still holds 20 s later against the open market's prices (FAQ question 2). The fair-price leans and the
volatility spread protect that markout, the inventory and fill-balance controls keep each book two-sided,
and the skill half strips out the drift on whatever inventory is left.

Local test (from the agents directory, with the proxy and simulator running):
    python MyAgentV2.py --port 8888 --agent_id 0 --params half_spread=0.0005 skew=4
"""
import math

import bittensor as bt
from taos.common.agents import launch
from taos.im.agents import FinanceAgent
from taos.im.protocol.models import OrderDirection, STP, TimeInForce

HOUR = 3_600_000_000_000
DAY_HOURS = 24

# Per-class strategy parameters and their defaults; `<name>_c<k>` in --params overrides one for class k.
CLASS_PARAMS = {
    "half_spread":        0.0005,
    "vol_mult":           0.5,
    "imbalance_skew":     0.5,
    "fundamental_weight": 0.04,
    "fill_skew":          0.3,
    "size_fraction":      0.006,
    "min_half_ticks":     1.5,
}
# Class defaults by index; classes past the last one listed start from its settings.
CLASS_DEFAULTS = {1: {"imbalance_skew": 0.0, "fundamental_weight": 0.0}}


class MyAgentV2(FinanceAgent):
    """Inventory-skewed passive market maker quoting every book."""

    def initialize(self):
        """Reads strategy parameters; every value passed via --params arrives as a float."""
        self.quantity          = float(getattr(self.config, "quantity", 1.0))
        # In half spreads per unit of inventory gap; 4 matches the former absolute 0.002 at a 5 bps half spread.
        self.skew              = float(getattr(self.config, "skew", 4.0))
        self.max_skew          = float(getattr(self.config, "max_skew", 0.8))
        self.inventory_band    = float(getattr(self.config, "inventory_band", 0.25))
        self.imbalance_depth   = int(getattr(self.config, "imbalance_depth", 5))
        self.vol_alpha         = float(getattr(self.config, "vol_alpha", 0.05))
        self.vol_horizon       = float(getattr(self.config, "vol_horizon", 20.0))
        self.gap_window        = float(getattr(self.config, "gap_window", 600.0))
        self.fill_window       = float(getattr(self.config, "fill_window", 1800.0))
        self.requote_frac        = float(getattr(self.config, "requote_frac", 0.3))
        self.requote_toward_frac = float(getattr(self.config, "requote_toward_frac", 0.1))
        self.requote_ticks       = int(getattr(self.config, "requote_ticks", 1))
        self.max_fee_rate      = float(getattr(self.config, "max_fee_rate", 0.002))
        self.turnover_cap      = float(getattr(self.config, "turnover_cap", 10.0))
        self.turnover_margin   = float(getattr(self.config, "turnover_margin", 0.9))
        self.pace_burst        = float(getattr(self.config, "pace_burst", 2.0))
        self.expiry_period     = int(getattr(self.config, "expiry_period", 60_000_000_000))
        # Local score card: printed every `report_interval` simulation nanoseconds (default one minute).
        self.report_interval   = int(getattr(self.config, "report_interval", 60_000_000_000))
        # One instance serves every validator, each running its own simulation, so all state is keyed by
        # validator: per-book memory under (validator, book_id), fill statistics and report times by validator.
        self.memory            = {}
        self.maker_stats       = {}
        self.next_report       = {}
        self.class_params      = {}

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
        """This validator's maker fill counts by side and the maker fees paid, in QUOTE."""
        return self.maker_stats.setdefault(validator, {OrderDirection.BUY: 0, OrderDirection.SELL: 0, "fees": 0.0})

    def _remember(self, validator, book_id, book, timestamp):
        """This book's memory for this validator, with the volatility estimate updated for the new mid and the
        fill balance decayed over `fill_window`.

        A timestamp earlier than the last one seen means the validator started a new simulation, so the
        memory starts over.
        """
        mid    = (book.bids[0].price + book.asks[0].price) / 2
        memory = self.memory.get((validator, book_id))
        if memory is None or timestamp < memory["timestamp"]:
            memory = {"timestamp": timestamp, "mid": mid, "variance": None, "start_wealth": None, "start_base": None,
                      "volume": {}, "gap_mean": None, "fills": {OrderDirection.BUY: 0.0, OrderDirection.SELL: 0.0}}
            self.memory[(validator, book_id)] = memory
        elapsed = (timestamp - memory["timestamp"]) / 1e9
        if elapsed > 0 and mid > 0 and memory["mid"] > 0:
            # Exponentially weighted squared log return of the mid, per second of simulation time.
            rate = math.log(mid / memory["mid"]) ** 2 / elapsed
            previous = memory["variance"]
            memory["variance"] = rate if previous is None else (1 - self.vol_alpha) * previous + self.vol_alpha * rate
            decay = math.exp(-elapsed / self.fill_window)
            for side in memory["fills"]:
                memory["fills"][side] *= decay
        memory["elapsed"] = elapsed
        memory["timestamp"], memory["mid"] = timestamp, mid
        return memory

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
        bt.logging.info(
            f"SCORE CARD | avg return {100 * sum(returns.values()) / len(returns):+.4f}% | {books} | "
            f"maker fills buy {stats[OrderDirection.BUY]} / sell {stats[OrderDirection.SELL]} | "
            f"maker fees {stats['fees']:+.4f}"
        )

    def _imbalance(self, book):
        """(bid - ask) / (bid + ask) volume over the top `imbalance_depth` levels; positive when bids are heavier."""
        bid_volume = sum(level.quantity for level in book.bids[:self.imbalance_depth])
        ask_volume = sum(level.quantity for level in book.asks[:self.imbalance_depth])
        total      = bid_volume + ask_volume
        return (bid_volume - ask_volume) / total if total > 0 else 0.0

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
        return 0.0 if previous is None else gap - previous

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

    def _fill_balance(self, memory):
        """(buy - sell) / (buy + sell) of this book's recent maker notional; positive when buys ran ahead."""
        buys, sells = memory["fills"][OrderDirection.BUY], memory["fills"][OrderDirection.SELL]
        return (buys - sells) / (buys + sells) if buys + sells > 0 else 0.0

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
        # Folded in every round, so the average stays current even while the book is not being quoted.
        fundamental_lean = self._fundamental_lean(book, memory, mid)

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
        # Half spread: wide enough for the mid's typical move over the markout horizon and for a whole tick of
        # edge on a coarse grid, plus any maker fee.
        sigma = math.sqrt(memory["variance"] * self.vol_horizon) if memory["variance"] else 0.0
        half  = max(params["half_spread"], params["vol_mult"] * sigma, params["min_half_ticks"] * tick / mid)
        half += max(fee_rate, 0.0)

        # Inventory beyond the book's initial BASE, as a share of wealth: 0 is balanced, above it means long.
        wealth    = account.base_balance.total * mid + account.quote_balance.total
        inventory = self._inventory_gap(account, memory, mid, wealth)
        # Lean toward the heavier side of the book and toward the fundamental, and away from the inventory and
        # from the side whose fills ran ahead (making credit counts the smaller side), by at most `max_skew` of
        # the half spread, so the bid always stays below the fair price and the ask above it. The mid closes
        # only a few per cent of its gap to the fundamental over 20 s, hence the small weight.
        shift = (params["imbalance_skew"] * self._imbalance(book) * half
                 + params["fundamental_weight"] * fundamental_lean
                 - params["fill_skew"] * self._fill_balance(memory) * half
                 - self.skew * inventory * half)
        shift = max(-self.max_skew * half, min(self.max_skew * half, shift))

        # Never cross the book: a passive quote must stay on its own side of the spread.
        bid_price = min(self._on_grid(mid * (1 + shift - half), decimals, True), round(best_ask - tick, decimals))
        ask_price = max(self._on_grid(mid * (1 + shift + half), decimals, False), round(best_bid + tick, decimals))
        if bid_price <= 0 or bid_price >= ask_price:
            return

        # One volume increment above the minimum: the engine rounds the float it receives down onto its
        # grid, so an order of exactly min_order_size (e.g. 2.5364 -> 2.53639999...) arrives as 2.5363.
        volume_decimals = self.volume_decimals(book_id)
        min_quantity    = self.min_order_size(book_id) + 10 ** -volume_decimals
        size            = params["size_fraction"] * wealth / mid if params["size_fraction"] > 0 else self.quantity
        quantity        = round(max(size, min_quantity), volume_decimals)

        # A quote that drifted away from the fair price may rest a while; one that drifted toward it is the one
        # the market runs over, so it is replaced almost at once.
        away   = max(self.requote_frac * half * mid, self.requote_ticks * tick)
        toward = self.requote_toward_frac * half * mid
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
    launch(MyAgentV2)
