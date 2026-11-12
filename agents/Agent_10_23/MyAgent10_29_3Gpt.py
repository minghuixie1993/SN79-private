# SPDX-License-Identifier: MIT
"""
MyAgent10_27_2Gpt with a per-BOOK off switch: `books_off=a-b,c,...` stops quoting the listed books (their resting orders
are cancelled each round), so two uids of one owner can split the books instead of queueing for the same fills.
Why (29 October, agents/data/replays/shield_grid_log.md 15:4x): UID 229 and UID 55 quoted the same c0 / c1 prices on the
same books and split each book's fills. A book's making credit is 2 x min(buy, sell) of its own fills, convex in the
fill count while the counts are small (#182: E[min] per fill 0.33 at 0.5 fills / side -> 0.80 at 8), so the same flow
earns more credit when each book has ONE of our uids on it; and it lets the young uid's share be set on purpose.
How: `books_off` is a comma list of book ids and inclusive ranges; each listed book cancels its resting orders and
quotes nothing (counted as "book_off" on the plan card); with `books_off_unwind` > 0 (a share of the book's wealth,
e.g. 0.01) a listed book that still holds more inventory than that quotes only the side that reduces it until it is
flat ("book_off_unwind"), so a handed-over book does not keep a stranded position. `phase_off_c<k>=a-b` cancels and
skips class k's quotes while the reseed phase (sim seconds into the `phase_period_s` = 30 s cycle) is in [a, b). Every other book, and the
class switch `quote_off_c<k>`, works exactly as 10_27_2Gpt; with no `books_off` the agent is 10_27_2Gpt.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_29_3Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 books_off=48-63,80-95
"""
import importlib.util
import json
import os
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

_spec = importlib.util.spec_from_file_location("MyAgent10_27_2Gpt", Path(__file__).with_name("MyAgent10_27_2Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)


def parse_books(spec) -> set:
    """'48-63,80-95,100' -> {48, ..., 63, 80, ..., 95, 100}; '' or None -> set()."""
    books = set()
    for part in str(spec or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(float(x)) for x in part.split("-", 1))
            books.update(range(min(lo, hi), max(lo, hi) + 1))
        else:
            books.add(int(float(part)))
    return books


class MyAgent10_29_3Gpt(_parent.MyAgent10_27_2Gpt):
    """MyAgent10_27_2Gpt with `books_off`; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.books_off = parse_books(getattr(self.config, "books_off", ""))
        self.books_off_unwind = float(getattr(self.config, "books_off_unwind", 0) or 0)
        # phase gate (29 Oct 16:2x): `phase_off_c<k>=a-b` stops quoting class k while the reseed phase (sim seconds into
        # the 30 s fundamental cycle) is in [a, b); UID 229's last 8 h of c2 maker fills realized -0.53 / -1.09 bps in
        # 5-10 / 10-20 s and +0.48 in 20-30 s
        self.phase_off = {}
        for i in range(16):
            spec = str(getattr(self.config, f"phase_off_c{i}", "") or "")
            if "-" in spec:
                lo, hi = (float(x) for x in spec.split("-", 1))
                self.phase_off[i] = (lo, hi)
        self.reseed_period_s = float(getattr(self.config, "phase_period_s", 30.0))
        self._blocked = set()
        # inventory stop (29 Oct 22:3x: UID 229 sat +24.5% long on book 36 against a -82 bps gap with no fills for 15 min):
        # `inv_stop_c<k>` = share of the book's wealth; beyond it the growing side is not quoted and the reducing side
        # joins the best price on its own side (post-only) until the position is back inside
        self.inv_stop = {i: float(getattr(self.config, f"inv_stop_c{i}", 0) or 0) for i in range(16)}
        self.inv_stop = {i: v for i, v in self.inv_stop.items() if v > 0}
        self._touch = {}
        bt.logging.info(f"10_29_3Gpt: books not quoted: {len(self.books_off)}"
                        + (f" ({min(self.books_off)}..{max(self.books_off)})" if self.books_off else ""))

    def _quote_book_2(self, response, validator, book_id, book, memory, account=None):
        gate = self.phase_off.get(self._class_index(book_id)) if self.phase_off else None
        if gate is not None and int(book_id) not in self.books_off:
            phase = (memory["timestamp"] / 1e9) % self.reseed_period_s
            if gate[0] <= phase < gate[1]:
                account = account if account is not None else self._accounts()[book_id]
                self._cancel(response, book_id, list(account.orders))
                self._count(validator, "phase_off")
                return
        if int(book_id) not in self.books_off:
            stop = self.inv_stop.get(self._class_index(book_id)) if self.inv_stop else None
            if stop:
                acct = account if account is not None else self._accounts()[book_id]
                gap = self._raw_gap(acct, memory)
                if abs(gap) > stop and book.bids and book.asks:
                    grow = OrderDirection.BUY if gap > 0 else OrderDirection.SELL
                    reduce = OrderDirection.SELL if gap > 0 else OrderDirection.BUY
                    self._blocked = {(int(book_id), grow)}
                    self._touch = {(int(book_id), reduce): book.asks[0].price if gap > 0 else book.bids[0].price}
                    try:
                        super()._quote_book_2(response, validator, book_id, book, memory, acct)
                    finally:
                        self._blocked, self._touch = set(), {}
                    self._cancel(response, book_id, [o for o in acct.orders if o.side == (0 if gap > 0 else 1)])
                    self._count(validator, "inv_stop")
                    return
            return super()._quote_book_2(response, validator, book_id, book, memory, account)
        account = account if account is not None else self._accounts()[book_id]
        # unwind (29 Oct 16:0x: UID 229 kept |78k| of inventory on the books it handed to UID 55): while the book still
        # holds more than `books_off_unwind` of wealth in inventory, quote only the side that reduces it
        mid = memory.get("mid") or 0.0
        wealth = account.base_balance.total * mid + account.quote_balance.total if mid > 0 else 0.0
        # RAW inventory vs the initial BASE: _inventory_gap is target-adjusted under 10_26_5Gpt's inventory targets, so
        # "unwind" kept chasing the moving target on handed-over books and never ended (29 Oct 19:1x: UID 55 filled
        # 134k on UID 229's books in an hour)
        gap = self._raw_gap(account, memory)
        if self.books_off_unwind > 0 and abs(gap) > self.books_off_unwind:
            grow = 0 if gap > 0 else 1                 # long: bids (side 0) would grow it; short: asks (side 1)
            self._blocked = {(int(book_id), OrderDirection.BUY if grow == 0 else OrderDirection.SELL)}
            try:
                super()._quote_book_2(response, validator, book_id, book, memory, account)
            finally:
                self._blocked = set()
            self._cancel(response, book_id, [o for o in account.orders if o.side == grow])
            self._count(validator, "book_off_unwind")
            return
        self._cancel(response, book_id, list(account.orders))
        self._count(validator, "book_off")

    def _raw_gap(self, account, memory):
        """RAW inventory vs the initial BASE as a share of wealth (not the target-adjusted _inventory_gap)."""
        mid = memory.get("mid") or 0.0
        wealth = account.base_balance.total * mid + account.quote_balance.total if mid > 0 else 0.0
        initial = getattr(account.base_balance, "initial", None)
        if initial is None:
            initial = memory.get("start_base") if memory.get("start_base") is not None else account.base_balance.total
        return (account.base_balance.total - initial) * mid / wealth if wealth > 0 else 0.0

    def _save_state(self, validator, sim_id, timestamp):
        """4Gpt's learned state plus the fundamental-gap EWMAs (30 Oct 02:3x: a restart re-started them at the current
        gap, so the demeaned predictor and the inventory targets read ~0 for hours of sim time after every restart -
        UID 229 restarted 4 times in an hour and its c2 skill fell 1.9 -> 0.6). 4Gpt's restore applies every saved key."""
        super()._save_state(validator, sim_id, timestamp)
        try:
            data = json.loads(self._state_file.read_text())
            books = data[str(validator)].setdefault("books", {})
            for (v, b), mem in self.memory.items():
                if v != validator:
                    continue
                extra = {k: mem[k] for k in ("gap_ewma", "gap_ts", "inv_gap_ewma", "inv_gap_ts") if mem.get(k) is not None}
                if extra:
                    books.setdefault(str(b), {}).update(extra)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self._state_file)
        except Exception as ex:
            bt.logging.warning(f"10_29_3Gpt: could not save the gap EWMAs: {ex}")

    def _place(self, response, book_id, direction, quantity, price):
        if (int(book_id), direction) in self._blocked:
            return
        touch = self._touch.get((int(book_id), direction)) if self._touch else None
        if touch is not None:
            price = touch                      # join the best price on our own side: post-only, never crosses
        super()._place(response, book_id, direction, quantity, price)


if __name__ == "__main__":
    launch(MyAgent10_29_3Gpt)
