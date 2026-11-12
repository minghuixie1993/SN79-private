# SPDX-License-Identifier: MIT
"""
MyAgent10_27_2Gpt with a per-book SIDE guard: a book side whose recent fills mark out badly stops quoting until its
record decays, so it stops pulling the book's making credit below zero.
Why (29 October, agents/data/replays/shield_grid_log.md free-time #184): the validator pays each book 2 x min(buy capture,
sell capture), so a book's credit is set by its WEAKER side and a side with a negative sum makes the whole book
negative. UID 229 had 12 of 31 class-2 books negative (-7.9 against +15.6), in 8 of them through the BUY side on books
whose price kept falling. The per-book spread adaptation that could widen such a book is off (book_mult_max=1.0).
How: for a class with `side_guard_c<k>` > 0, each maker fill, once it is `markout_horizon` old, adds its notional-weighted
markout sign x (mid then - fill price) to a decayed sum for its (book, side), half-life `side_guard_hl_s` sim seconds
(default 1800). While a side's decayed average is below -`side_guard_bps` (default 1.0) over at least
`side_guard_min_notional` (default 2000) of decayed notional, that side is not quoted on that book (its resting orders are
cancelled, new ones are not placed); the other side quotes as before. With no new fills the record decays, so the side
comes back. A class without `side_guard_c<k>` is exactly 10_27_2Gpt.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_29_2Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 side_guard_c2=1
"""
import importlib.util
import math
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

_spec = importlib.util.spec_from_file_location("MyAgent10_27_2Gpt", Path(__file__).with_name("MyAgent10_27_2Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)


class MyAgent10_29_2Gpt(_parent.MyAgent10_27_2Gpt):
    """MyAgent10_27_2Gpt with the per-book side guard; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.side_guard = {i for i in range(16) if float(getattr(self.config, f"side_guard_c{i}", 0) or 0) > 0}
        self.side_guard_hl = float(getattr(self.config, "side_guard_hl_s", 1800)) * 1e9
        self.side_guard_bps = float(getattr(self.config, "side_guard_bps", 1.0))
        self.side_guard_min = float(getattr(self.config, "side_guard_min_notional", 2000))
        self.side_record = {}            # (validator, book, sign) -> [decayed markout x notional, decayed notional, t]
        self._blocked = set()            # (book, OrderDirection) not to be placed in the reply being built
        bt.logging.info("10_29_2Gpt: side guard on "
                        + (", ".join(f"c{i}" for i in sorted(self.side_guard)) or "none")
                        + f" (half-life {self.side_guard_hl / 1e9:g} s, below -{self.side_guard_bps:g} bps"
                          f" over >= {self.side_guard_min:g} notional)")

    def _decay(self, record, t):
        if t > record[2]:
            f = math.exp(-math.log(2) * (t - record[2]) / self.side_guard_hl)
            record[0] *= f
            record[1] *= f
            record[2] = t

    def _settle_markouts(self, validator, timestamp):
        if self.side_guard:
            due = timestamp - self.markout_horizon
            for fill_time, book_id, sign, price, notional, _ in self.pending_markouts.get(validator) or []:
                memory = self.memory.get((validator, book_id))
                if memory is None or fill_time > memory["timestamp"] or fill_time > due:
                    continue                     # the same entries the parent settles (and drops) below
                if self._class_index(book_id) not in self.side_guard:
                    continue
                record = self.side_record.setdefault((validator, int(book_id), sign), [0.0, 0.0, timestamp])
                self._decay(record, timestamp)
                record[0] += notional * sign * (memory["mid"] - price) / price * 1e4
                record[1] += notional
        super()._settle_markouts(validator, timestamp)

    def _side_blocked(self, validator, book_id, sign, timestamp):
        record = self.side_record.get((validator, int(book_id), sign))
        if record is None:
            return False
        self._decay(record, timestamp)
        return record[1] >= self.side_guard_min and record[0] / record[1] < -self.side_guard_bps

    def _quote_book_2(self, response, validator, book_id, book, memory, account=None):
        if self._class_index(book_id) not in self.side_guard:
            return super()._quote_book_2(response, validator, book_id, book, memory, account)
        now = memory["timestamp"]
        blocked = [d for d, sign in ((OrderDirection.BUY, 1), (OrderDirection.SELL, -1))
                   if self._side_blocked(validator, book_id, sign, now)]
        if not blocked:
            return super()._quote_book_2(response, validator, book_id, book, memory, account)
        self._blocked = {(int(book_id), d) for d in blocked}
        try:
            super()._quote_book_2(response, validator, book_id, book, memory, account)
        finally:
            self._blocked = set()
        account = account if account is not None else self._accounts()[book_id]
        sides = {0 if d == OrderDirection.BUY else 1 for d in blocked}
        self._cancel(response, book_id, [o for o in account.orders if o.side in sides])
        for d in blocked:
            self._count(validator, "side_guard_buy" if d == OrderDirection.BUY else "side_guard_sell")

    def _place(self, response, book_id, direction, quantity, price):
        if (int(book_id), direction) in self._blocked:
            return
        super()._place(response, book_id, direction, quantity, price)


if __name__ == "__main__":
    launch(MyAgent10_29_2Gpt)
