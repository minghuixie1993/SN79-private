# SPDX-License-Identifier: MIT
"""
MyAgent10_23_1Gpt: MyAgent10_23_Gpt with size-aware keeping, non-finite guards and per-validator statistics.

Everything MyAgent10_23_Gpt (and through it MyAgentV3) decides is kept; this class changes four things:
  - a resting quote is kept only while its size still fits the plan: once it is more than
    `keep_size_tolerance` (a fraction, default 0.3) larger than the size the plan would place now, it is
    replaced, so a quote placed before the inventory moved cannot keep adding to the gap at its old size.
    A resting quote smaller than the plan (typically what is left after a partial fill) is kept, since
    replacing it would give up its place in the queue for a little more size. 0 switches the check off,
  - a non-finite input (a fair price, half spread or prediction that is NaN or infinite) cancels the book's
    quotes instead of raising mid-way and leaving them unmanaged; a non-finite decayed input in the book's
    memory (volatility, order-flow, taker-flow and momentum sums) is reset, so one bad state cannot poison
    every later round,
  - rejections are attributed to the validator whose response placed the order (matched on book, side,
    price and size), so the PLAN CARD's placed / rejected / reject rate are per validator,
  - an order that is placed and filled in full between two states (so it is never seen resting) still
    counts for the fill probability and the time to fill, timed from the round it was sent.

Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_23_1Gpt.py --port 8888 --agent_id 1 --params keep_size_tolerance=0.3
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bittensor as bt
from MyAgent10_23_Gpt import MyAgent10_23_Gpt, KEEP, REPLACE, CANCEL_ONLY, SKIP, DECISIONS
from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

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


if __name__ == "__main__":
    launch(MyAgent10_23_1Gpt)
