# SPDX-License-Identifier: MIT
"""
MyAgentGpt: MyAgentV2 with a few risk and sizing additions on top.

Everything MyAgentV2 does is kept (predicted fair price, volatility spread, markout-adapted width, inventory
target at the initial holding); this class only adds:
  - confidence-weighted fill skew: V2's fill balance is scaled by total / (total + `fill_confidence_fraction`
    x the book's wealth), so a single fill cannot push the quotes the full `fill_skew`,
  - asymmetric inventory sizing: the quote that would add to the inventory gap shrinks linearly with it, by
    `inventory_sizing` at the edge of V2's `inventory_band`, never below `min_inventory_size_factor` of the
    normal size nor below the book's minimum order,
  - a hard cap of `max_order_fraction` of the book's wealth per order,
  - cancel-everything when the mid or the book's wealth is not a finite positive number.

The original draft of this agent was written against an older MyAgentV2 (with `_imbalance`,
`_fundamental_lean` and an inventory target of half of wealth); it hooked V2 where those methods used to be.
Its own top-of-book OFI and volatility estimate are dropped: V2 already computes both, and the OFI written into
`memory["ofi"]` on a different scale would have corrupted V2's predictor input.

Local test (from the agents directory, with the proxy and simulator running):
    python MyAgentGpt.py --port 8888 --agent_id 0 --params inventory_sizing=0.8
"""
import math

from MyAgentV2 import MyAgentV2
from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection


class MyAgentGpt(MyAgentV2):
    """MyAgentV2 with confidence-weighted fill skew, inventory-asymmetric sizing and hard size caps."""

    def initialize(self):
        super().initialize()
        self.fill_confidence_fraction  = max(1e-6, float(getattr(self.config, "fill_confidence_fraction", 0.02)))
        self.max_order_fraction        = max(0.0, float(getattr(self.config, "max_order_fraction", 0.02)))
        self.inventory_sizing          = max(0.0, min(1.0, float(getattr(self.config, "inventory_sizing", 0.8))))
        self.min_inventory_size_factor = max(0.0, min(1.0, float(getattr(self.config, "min_inventory_size_factor", 0.2))))
        # Per book, for the quote being built: (wealth, inventory gap); set in _quote_book, read by _place.
        self._quote_context = {}

    def _quote_book(self, response, validator, book_id, book, memory):
        """V2's quoting, after recording the book's wealth and inventory gap for the sizing hooks, and cancelling
        everything when either is unusable."""
        account = self.accounts[book_id]
        mid     = memory["mid"]
        wealth  = account.base_balance.total * mid + account.quote_balance.total if math.isfinite(mid) else math.nan
        if not math.isfinite(mid) or mid <= 0 or not math.isfinite(wealth) or wealth <= 0:
            self._cancel(response, book_id, list(account.orders))
            return
        memory["current_wealth"] = wealth
        self._quote_context[book_id] = (wealth, self._inventory_gap(account, memory, mid, wealth))
        try:
            super()._quote_book(response, validator, book_id, book, memory)
        finally:
            self._quote_context.pop(book_id, None)

    def _fill_balance(self, memory):
        """V2's fill balance, scaled by how much fill notional backs it relative to the book's wealth."""
        raw   = super()._fill_balance(memory)
        total = memory["fills"][OrderDirection.BUY] + memory["fills"][OrderDirection.SELL]
        threshold = max(1e-9, memory.get("current_wealth", 0.0)) * self.fill_confidence_fraction
        return raw * total / (total + threshold) if total > 0 else 0.0

    def _place(self, response, book_id, direction, quantity, price):
        """Shrinks the order that adds to the inventory gap and caps it at `max_order_fraction` of wealth.

        Only ever shrinks V2's quantity, so V2's free-balance check still holds; never below the book's
        minimum order, since a refused order would leave the side unquoted after V2 cancelled its old quote."""
        context = self._quote_context.get(book_id)
        if context is not None and price > 0:
            wealth, inventory = context
            pressure = max(0.0, inventory if direction == OrderDirection.BUY else -inventory)
            factor   = max(self.min_inventory_size_factor,
                           1.0 - self.inventory_sizing * pressure / max(self.inventory_band, 1e-9))
            size = quantity * factor
            if self.max_order_fraction > 0:
                size = min(size, self.max_order_fraction * wealth / price)
            decimals = self.volume_decimals(book_id)
            step     = 10 ** -decimals
            minimum  = self.min_order_size(book_id) + step
            quantity = round(max(minimum, math.floor(size / step + 1e-9) * step), decimals)
        super()._place(response, book_id, direction, quantity, price)


if __name__ == "__main__":
    launch(MyAgentGpt)
