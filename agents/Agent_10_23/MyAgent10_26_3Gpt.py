# SPDX-License-Identifier: MIT
"""
MyAgent10_26_2Gpt with the per-book fill balance measured in captured spread instead of notional.
Why (26 October, agents/data/replays/shield_grid_log.md free-time #71): the validator pays each book 2 x min(buy capture,
sell capture), capture taken against a centred mid of the prints around each fill. 9Gpt's `fill_skew` steers the book's
NOTIONAL balance. On UID 229's recording (06:02-11:30 CET, the validator's own accumulate_book_capture) c0's notional is
94% two-sided but its capture only 69%, and the notional-heavy side is the capture-heavy side in just 38 of 64 books.
Of the quantities the agent sees, the mean of the edge at the fill (against the quote mid before it) and the 20 s
markout, times notional, tracks the validator's per-book capture imbalance best: correlation across books c0 0.55,
c1 0.90, c2 0.60, against 0.26 / 0.78 / 0.20 for notional (scratchpad d660_balproxy.py).
How: with `balance_basis=1` each maker fill, when its markout settles, adds 0.5 x (edge + markout) x notional (bps x
QUOTE) to its side of a per-book capture balance, decayed over `fill_window` like the notional one, and `fill_skew`
steers on that balance. The evidence prior is `fill_confidence` quotes' notional x `balance_capture_bps` (3 bps, about
c0's mean capture per fill), so one fill still cannot swing the quotes. `balance_basis=0` is 10_26_2Gpt exactly.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_26_3Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 balance_basis=1
"""
import importlib.util
import math
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

_spec = importlib.util.spec_from_file_location("MyAgent10_26_2Gpt", Path(__file__).with_name("MyAgent10_26_2Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)


class MyAgent10_26_3Gpt(_parent.MyAgent10_26_2Gpt):
    """MyAgent10_26_2Gpt with a capture-based fill balance; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.balance_basis       = int(float(getattr(self.config, "balance_basis", 1)))
        self.balance_capture_bps = float(getattr(self.config, "balance_capture_bps", 3.0))
        bt.logging.info(f"10_26_3Gpt: fill balance on {'capture' if self.balance_basis else 'notional'}"
                        f" (prior {self.balance_capture_bps:g} bps per quote)")

    def _remember(self, validator, book_id, book, timestamp):
        memory = super()._remember(validator, book_id, book, timestamp)
        if memory is None:
            return memory
        cap = memory.get("cap")
        if cap is None or timestamp < cap["ts"]:
            memory["cap"] = {"ts": timestamp, OrderDirection.BUY: 0.0, OrderDirection.SELL: 0.0}
        else:
            elapsed = (timestamp - cap["ts"]) / 1e9
            if elapsed > 0:
                decay = math.exp(-elapsed / self.fill_window)
                cap[OrderDirection.BUY] *= decay
                cap[OrderDirection.SELL] *= decay
                cap["ts"] = timestamp
        return memory

    def _settle_markouts(self, validator, timestamp):
        """The parent's settlement; each fill it settles first adds its captured spread to its book's balance."""
        if self.balance_basis:
            due = timestamp - self.markout_horizon
            for fill_time, book_id, sign, price, notional, fill_mid in self.pending_markouts.get(validator) or []:
                memory = self.memory.get((validator, book_id))
                # the same test the parents settle by (9Gpt's phase markouts), so each fill is counted exactly once
                if memory is None or fill_time > memory["timestamp"] or fill_time > due or price <= 0:
                    continue
                cap = memory.get("cap")
                if cap is None:
                    continue
                edge = sign * (fill_mid - price) / price * 1e4
                mark = sign * (memory["mid"] - price) / price * 1e4
                side = OrderDirection.BUY if sign > 0 else OrderDirection.SELL
                cap[side] += 0.5 * (edge + mark) * notional
        super()._settle_markouts(validator, timestamp)

    def _fill_balance(self, memory, quote_notional, confidence_quotes):
        """(buy - sell) / (|buy| + |sell| + prior) of the book's recent captured spread; positive when buys ran
        ahead. The notional balance when `balance_basis` is 0 or the book has no capture balance yet."""
        cap = memory.get("cap")
        if not self.balance_basis or cap is None:
            return super()._fill_balance(memory, quote_notional, confidence_quotes)
        buys, sells = cap[OrderDirection.BUY], cap[OrderDirection.SELL]
        prior = max(confidence_quotes, 0.0) * quote_notional * self.balance_capture_bps
        total = abs(buys) + abs(sells) + prior
        return (buys - sells) / total if total > 0 else 0.0


if __name__ == "__main__":
    launch(MyAgent10_26_3Gpt)
