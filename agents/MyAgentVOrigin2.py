# SPDX-License-Identifier: MIT
"""
MyAgentVOrigin with three textbook market-making fixes, written to be compared against it.

Same skeleton as MyAgentVOrigin: one passive bid and one passive ask per book around the mid, an inventory
skew, a requote threshold, post-only expiring orders. What changes:
  - fee-aware spread: instead of quoting at `half_spread` until the maker fee passes `max_fee_rate` and then
    stopping, the half spread is never below the maker fee plus `min_edge`, so a fill always clears its fee;
    the hard stop at `max_fee_rate` stays as a safety net,
  - volatility-scaled spread: an exponentially weighted estimate of the mid's volatility (half-life
    `vol_halflife_s` seconds), scaled to the `vol_horizon_s` markout horizon, widens the half spread to
    `vol_mult` times that move when the market is busy, so quotes are not run over by ordinary noise,
  - inventory band: once the BASE share of wealth leaves 0.5 +/- `inventory_band`, only the side that brings
    inventory back is quoted (the other side is cancelled), instead of skewing both sides forever.

Local test (from the agents directory, with the proxy and simulator running):
    python MyAgentVOrigin2.py --port 8889 --agent_id 1 --params quantity=1.0 half_spread=0.0005 skew=0.002
"""
import math

import bittensor as bt
from taos.common.agents import launch
from taos.im.agents import FinanceAgent
from taos.im.protocol.models import OrderDirection, STP, TimeInForce


class MyAgentVOrigin2(FinanceAgent):
    """Fee-aware, volatility-scaled, inventory-banded passive market maker quoting every book."""

    def initialize(self):
        """Reads strategy parameters; every value passed via --params arrives as a float."""
        self.quantity          = float(getattr(self.config, "quantity", 1.0))
        self.half_spread       = float(getattr(self.config, "half_spread", 0.0005))
        self.skew              = float(getattr(self.config, "skew", 0.002))
        self.requote_threshold = float(getattr(self.config, "requote_threshold", 0.0005))
        self.max_fee_rate      = float(getattr(self.config, "max_fee_rate", 0.005))
        self.expiry_period     = int(getattr(self.config, "expiry_period", 200_000_000_000))
        # Spread floor above the maker fee, as a fraction of mid.
        self.min_edge          = float(getattr(self.config, "min_edge", 0.0001))
        # Volatility estimate: EWMA of squared mid log-returns per second, scaled to the markout horizon.
        self.vol_mult          = float(getattr(self.config, "vol_mult", 1.0))
        self.vol_horizon_s     = float(getattr(self.config, "vol_horizon_s", 20.0))
        self.vol_halflife_s    = float(getattr(self.config, "vol_halflife_s", 60.0))
        # Only the inventory-reducing side is quoted outside 0.5 +/- this BASE share of wealth.
        self.inventory_band    = float(getattr(self.config, "inventory_band", 0.2))
        # Local score card: printed every `report_interval` simulation nanoseconds (default one minute).
        self.report_interval   = int(getattr(self.config, "report_interval", 60_000_000_000))
        self.start_wealth      = {}
        self.maker_fills       = {OrderDirection.BUY: 0, OrderDirection.SELL: 0}
        self.next_report       = None
        self.last_mid          = {}   # book_id -> (timestamp ns, mid)
        self.var_per_s         = {}   # book_id -> EWMA of squared log-return per second

    def onTrade(self, event, validator=None):
        """Counts this agent's passive fills by side; `event.side` is the taker's side, so a maker gets the opposite."""
        if event.makerAgentId == self.uid:
            side = OrderDirection.SELL if event.side == OrderDirection.BUY else OrderDirection.BUY
            self.maker_fills[side] += 1

    def respond(self, state):
        """Builds one response covering every book; serves both simulation and exchange mode."""
        response = self.make_response()
        returns  = {}
        for book_id, book in (state.books or {}).items():
            if not book.bids or not book.asks or book_id not in self.accounts:
                continue
            try:
                self._update_vol(book_id, book, state.timestamp)
                self._quote_book(response, book_id, book)
                returns[book_id] = self._book_return(book_id, book)
            except Exception as ex:
                # A problem on one book must not cost the instructions for every other book.
                bt.logging.error(f"BOOK {book_id} | quoting failed: {ex}")
        self._report(state.timestamp, returns)
        return response

    def _update_vol(self, book_id, book, timestamp):
        """Folds the mid's log-return since the last state into this book's per-second variance estimate."""
        mid = (book.bids[0].price + book.asks[0].price) / 2
        last = self.last_mid.get(book_id)
        self.last_mid[book_id] = (timestamp, mid)
        if last is None or mid <= 0 or last[1] <= 0:
            return
        dt_s = (timestamp - last[0]) / 1e9
        if dt_s <= 0:
            return
        ret   = math.log(mid / last[1])
        alpha = 1 - 0.5 ** (dt_s / self.vol_halflife_s)
        prev  = self.var_per_s.get(book_id)
        obs   = ret * ret / dt_s
        self.var_per_s[book_id] = obs if prev is None else prev + alpha * (obs - prev)

    def _horizon_vol(self, book_id):
        """Expected size of the mid's move over the markout horizon, as a fraction of mid (0 until measured)."""
        var = self.var_per_s.get(book_id)
        return math.sqrt(var * self.vol_horizon_s) if var else 0.0

    def _book_return(self, book_id, book):
        """Return on this book's wealth (marked at mid) since the agent first saw it."""
        account = self.accounts[book_id]
        mid     = (book.bids[0].price + book.asks[0].price) / 2
        wealth  = account.base_balance.total * mid + account.quote_balance.total
        start   = self.start_wealth.setdefault(book_id, wealth)
        return wealth / start - 1 if start > 0 else 0.0

    def _report(self, timestamp, returns):
        """Prints a rough local stand-in for the validator score; the real score only exists on a network."""
        if self.next_report is None:
            self.next_report = timestamp + self.report_interval
        if timestamp < self.next_report or not returns:
            return
        self.next_report = timestamp + self.report_interval
        low  = [r for b, r in returns.items() if b < 96]
        high = [r for b, r in returns.items() if b >= 96]
        buys, sells = self.maker_fills[OrderDirection.BUY], self.maker_fills[OrderDirection.SELL]
        bt.logging.info(
            f"SCORE CARD | avg return {100 * sum(returns.values()) / len(returns):+.4f}% | "
            f"profitable books 0-95: {sum(r > 0 for r in low)}/{len(low)} (skill needs 15) | "
            f"96-127: {sum(r > 0 for r in high)}/{len(high)} (skill needs 5) | "
            f"maker fills buy {buys} / sell {sells}"
        )

    def _target_half_spread(self, book_id, maker_fee_rate):
        """The widest of the base half spread, the fee plus a minimum edge, and the horizon volatility move."""
        fee = max(maker_fee_rate, 0.0)  # a rebate never narrows the quote below `half_spread`
        return max(self.half_spread, fee + self.min_edge, self.vol_mult * self._horizon_vol(book_id))

    def _quote_book(self, response, book_id, book):
        """Keeps up to one bid and one ask resting near their target prices on a single book."""
        account  = self.accounts[book_id]
        best_bid = book.bids[0].price
        best_ask = book.asks[0].price
        mid      = (best_bid + best_ask) / 2
        if mid <= 0:
            return

        bids = [o for o in account.orders if o.side == OrderDirection.BUY]
        asks = [o for o in account.orders if o.side == OrderDirection.SELL]

        # Safety net: stop making while the maker fee is above tolerance (negative rates are rebates).
        if account.fees.maker_fee_rate > self.max_fee_rate:
            for orders in (bids, asks):
                if orders:
                    response.cancel_orders(book_id, [o.id for o in orders])
            return

        # Share of wealth held in BASE: 0.5 is balanced, above it means long inventory.
        base_value  = account.base_balance.total * mid
        wealth      = base_value + account.quote_balance.total
        base_share  = base_value / wealth if wealth > 0 else 0.5
        shift       = self.skew * (base_share - 0.5)
        half        = self._target_half_spread(book_id, account.fees.maker_fee_rate)

        # Outside the band only the side that brings inventory back toward 0.5 is quoted.
        quote_bid = base_share <= 0.5 + self.inventory_band
        quote_ask = base_share >= 0.5 - self.inventory_band

        decimals = self.price_decimals(book_id)
        tick     = 10 ** -decimals
        # Never cross the book: a passive quote must stay on its own side of the spread.
        bid_price = round(min(mid * (1 - half - shift), best_ask - tick), decimals)
        ask_price = round(max(mid * (1 + half - shift), best_bid + tick), decimals)
        if bid_price <= 0 or bid_price >= ask_price:
            return

        # One volume increment above the minimum: the engine rounds the float it receives down onto its
        # grid, so an order of exactly min_order_size (e.g. 2.5364 -> 2.53639999...) arrives as 2.5363.
        volume_decimals = self.volume_decimals(book_id)
        min_quantity    = self.min_order_size(book_id) + 10 ** -volume_decimals
        quantity        = round(max(self.quantity, min_quantity), volume_decimals)

        if not quote_bid:
            if bids:
                response.cancel_orders(book_id, [o.id for o in bids])
        elif self._needs_requote(response, book_id, bids, bid_price, mid):
            if account.quote_balance.free >= quantity * bid_price:
                self._place(response, book_id, OrderDirection.BUY, quantity, bid_price)
        if not quote_ask:
            if asks:
                response.cancel_orders(book_id, [o.id for o in asks])
        elif self._needs_requote(response, book_id, asks, ask_price, mid):
            if account.base_balance.free >= quantity:
                self._place(response, book_id, OrderDirection.SELL, quantity, ask_price)

    def _needs_requote(self, response, book_id, orders, target, mid):
        """True when this side has no quote near `target`; cancels any stale quotes it finds."""
        if len(orders) == 1 and abs(orders[0].price - target) / mid <= self.requote_threshold:
            return False
        if orders:
            response.cancel_orders(book_id, [o.id for o in orders])
        return True

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
    launch(MyAgentVOrigin2)
