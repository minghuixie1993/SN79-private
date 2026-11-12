# SPDX-License-Identifier: MIT
"""
Inventory-skewed two-sided market maker, written as a starting point for local proxy testing.

On every book it keeps one passive bid and one passive ask around the mid price:
  - quotes sit `half_spread` (fraction of mid) either side of the mid,
  - both quotes shift away from the side the agent is already heavy on (`skew`), so inventory
    drifts back toward a balanced split instead of accumulating,
  - a resting quote is kept while it stays within `requote_threshold` of where it should be, and
    is cancelled and replaced once the market moves away from it,
  - quoting stops on a book while its maker fee rate exceeds `max_fee_rate`.

Two-sided passive fills are what the "making" half of the score rewards (FAQ question 2), and the
inventory skew limits the drift exposure that the "skill" half strips out.

Local test (from the agents directory, with the proxy and simulator running):
    python MyAgent.py --port 8888 --agent_id 0 --params quantity=1.0 half_spread=0.0005 skew=0.002

Or the full stack from the repository root:
    ./agents/proxy/run --config agents/proxy/my_config.json
"""
import bittensor as bt
from taos.common.agents import launch
from taos.im.agents import FinanceAgent
from taos.im.protocol.models import OrderDirection, STP, TimeInForce


class MyAgent(FinanceAgent):
    """Inventory-skewed passive market maker quoting every book."""

    def initialize(self):
        """Reads strategy parameters; every value passed via --params arrives as a float."""
        self.quantity          = float(getattr(self.config, "quantity", 1.0))
        self.half_spread       = float(getattr(self.config, "half_spread", 0.0005))
        self.skew              = float(getattr(self.config, "skew", 0.002))
        self.requote_threshold = float(getattr(self.config, "requote_threshold", 0.0005))
        self.max_fee_rate      = float(getattr(self.config, "max_fee_rate", 0.005))
        self.expiry_period     = int(getattr(self.config, "expiry_period", 200_000_000_000))
        # Local score card: printed every `report_interval` simulation nanoseconds (default one minute).
        self.report_interval   = int(getattr(self.config, "report_interval", 60_000_000_000))
        self.start_wealth      = {}
        self.maker_fills       = {OrderDirection.BUY: 0, OrderDirection.SELL: 0}
        self.next_report       = None

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
                self._quote_book(response, book_id, book)
                returns[book_id] = self._book_return(book_id, book)
            except Exception as ex:
                # A problem on one book must not cost the instructions for every other book.
                bt.logging.error(f"BOOK {book_id} | quoting failed: {ex}")
        self._report(state.timestamp, returns)
        return response

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

    def _quote_book(self, response, book_id, book):
        """Keeps one bid and one ask resting near their target prices on a single book."""
        account  = self.accounts[book_id]
        best_bid = book.bids[0].price
        best_ask = book.asks[0].price
        mid      = (best_bid + best_ask) / 2
        if mid <= 0:
            return

        bids = [o for o in account.orders if o.side == OrderDirection.BUY]
        asks = [o for o in account.orders if o.side == OrderDirection.SELL]

        # Stop making while the maker fee is above tolerance (negative rates are rebates).
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

        decimals = self.price_decimals(book_id)
        tick     = 10 ** -decimals
        # Never cross the book: a passive quote must stay on its own side of the spread.
        bid_price = round(min(mid * (1 - self.half_spread - shift), best_ask - tick), decimals)
        ask_price = round(max(mid * (1 + self.half_spread - shift), best_bid + tick), decimals)
        if bid_price <= 0 or bid_price >= ask_price:
            return

        # One volume increment above the minimum: the engine rounds the float it receives down onto its
        # grid, so an order of exactly min_order_size (e.g. 2.5364 -> 2.53639999...) arrives as 2.5363.
        volume_decimals = self.volume_decimals(book_id)
        min_quantity    = self.min_order_size(book_id) + 10 ** -volume_decimals
        quantity        = round(max(self.quantity, min_quantity), volume_decimals)

        if self._needs_requote(response, book_id, bids, bid_price, mid):
            if account.quote_balance.free >= quantity * bid_price:
                self._place(response, book_id, OrderDirection.BUY, quantity, bid_price)
        if self._needs_requote(response, book_id, asks, ask_price, mid):
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
    launch(MyAgent)
