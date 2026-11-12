# SPDX-License-Identifier: MIT
"""
MyAgent10_26_12Gpt with buy and sell windows set per book by the validator's two-sided test (user, 26 October:
"if the validator pays each book 2 x min(buy capture, sell capture), decide buy window and sell window properly").

What the validator pays (taos/im/validator/trade.py, debeta.balanced_reward_per_book): per book, the uid's maker
capture on buys and on sells, (mid - price) x qty and (price - mid) x qty, each summed over a rolling window of
`scoring.kappa.lookback` (3 sim hours), and then 2 x min(buy sum, sell sum). Capture on the side that is ahead
earns nothing until the other side catches up; how the fills are ordered inside the window does not matter. On the
mainnet recording (25 October, 9Gpt live set) class 0 had buy capture 242.7 and sell capture 150.3 summed over its
books but a two-sided sum of only 102.8: 26% of the capture was paid.

So each book's own capture is kept the same way (every own maker fill, against the book's quote mid when it was
filled, for `bal_window_s`), and its imbalance d = (buy - sell) / (buy + sell + `bal_prior`) sets the window:
    d >  0: a sell window - the bid's size is cut by d / `bal_off`, to nothing at d >= `bal_off`;
    d <  0: a buy window - the ask's size is cut the same way;
    the lagging side keeps its size, times 1 + `bal_lag_gain` x |d| (at most `bal_lag_max`).
Nothing acts before the book has `bal_min_fills` fills in the window. The lock, no-chase and patience of 11Gpt / 12Gpt
still apply on top (all off by default).

With bal_mode=0 it places exactly 12Gpt's orders.

Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_26_13Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 bal_mode=1
"""
import importlib.util
from collections import deque
from pathlib import Path

import bittensor as bt
from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

_spec = importlib.util.spec_from_file_location("MyAgent10_26_12Gpt", Path(__file__).with_name("MyAgent10_26_12Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)
CANCEL_ONLY, SKIP = _parent._parent.CANCEL_ONLY, _parent._parent.SKIP

CLASS13_PARAMS = {
    "bal_mode":      0.0,       # 0 = 12Gpt
    "bal_window_s":  10800.0,   # the validator's kappa lookback, sim seconds
    "bal_prior":     1.0,       # QUOTE of capture added to the denominator, so a book's first fills move it little
    "bal_off":       0.3,       # imbalance at which the leading side is off
    "bal_lag_gain":  0.0,       # lagging side x (1 + this x |d|)
    "bal_lag_max":   1.5,
    "bal_min_fills": 4.0,       # fills in the window before the balance acts
}


class MyAgent10_26_13Gpt(_parent.MyAgent10_26_12Gpt):
    """MyAgent10_26_12Gpt with per-book buy / sell windows from the 3 h two-sided capture balance."""

    def initialize(self):
        super().initialize()
        self.class13_params = {}
        p = self._params13_index(0)
        bt.logging.info(f"13Gpt: capture balance {int(p['bal_mode'])} over {p['bal_window_s']:g} s, leading side off at "
                        f"{p['bal_off']:g}, lagging gain {p['bal_lag_gain']:g} (class 0; _c<k> overrides per class)")

    def _params13_index(self, index):
        params = self.class13_params.get(index)
        if params is None:
            params = {}
            for name, default in CLASS13_PARAMS.items():
                base = float(getattr(self.config, name, default))
                params[name] = float(getattr(self.config, f"{name}_c{index}", base))
            self.class13_params[index] = params
        return params

    def _sync_config(self, state):
        before = self._layout
        super()._sync_config(state)
        if self._layout != before:
            self.class13_params.clear()

    # ── The book's own capture over the window ─────────────────────────────────────────────────────────────────

    def onTrade(self, event, validator=None):
        super().onTrade(event, validator)
        if event.makerAgentId != self.uid:
            return
        memory = self.memory.get((validator, event.bookId))
        if memory is None or not memory.get("mid"):
            return
        mid = memory["mid"]
        buy = event.side != OrderDirection.BUY          # a taker sell fills our bid
        capture = ((mid - event.price) if buy else (event.price - mid)) * event.quantity
        memory.setdefault("b13_fills", deque()).append((event.timestamp, buy, capture))

    def _balance(self, memory, index):
        """(imbalance d, fills in the window) of the book's buy and sell capture."""
        fills = memory.get("b13_fills")
        if not fills:
            return 0.0, 0
        p = self._params13_index(index)
        edge = memory["timestamp"] - p["bal_window_s"] * 1e9
        while fills and fills[0][0] < edge:
            fills.popleft()
        buys = sum(c for _, b, c in fills if b)
        sells = sum(c for _, b, c in fills if not b)
        # a side whose capture is negative pays less than nothing: count it as 0 for the comparison
        buys, sells = max(buys, 0.0), max(sells, 0.0)
        return (buys - sells) / (buys + sells + p["bal_prior"]), len(fills)

    def _bal_mult(self, direction, memory, index):
        """This side's size multiplier from the book's balance, or None when the balance does not act."""
        p = self._params13_index(index)
        if p["bal_mode"] <= 0:
            return None
        d, n = self._balance(memory, index)
        if n < p["bal_min_fills"] or d == 0:
            return None
        leading = (d > 0) == (direction == OrderDirection.BUY)
        if leading:
            return max(0.0, 1.0 - abs(d) / max(p["bal_off"], 1e-9))
        return min(p["bal_lag_max"], 1.0 + p["bal_lag_gain"] * abs(d))

    def _plan_side(self, direction, orders, target, base_quantity, ctx):
        validator, book_id = ctx["validator"], ctx.get("book_id")
        memory = self.memory.get((validator, book_id))
        if memory is None:
            return super()._plan_side(direction, orders, target, base_quantity, ctx)
        index = self._class_index(book_id)
        mult = self._bal_mult(direction, memory, index)
        if mult is None:
            return super()._plan_side(direction, orders, target, base_quantity, ctx)
        if mult <= 0:
            self._count(validator, f"b13_off_c{index}")
            return (CANCEL_ONLY, orders, None) if orders else (SKIP, [], None)
        if mult != 1.0:
            self._count(validator, f"b13_{'cut' if mult < 1 else 'up'}_c{index}")
        return super()._plan_side(direction, orders, target, base_quantity * mult, ctx)

    def _plan_card(self, validator):
        card = super()._plan_card(validator)
        stats = self.plan_stats.get(validator, {})
        parts = []
        for index, name in enumerate(self._class_names() or ["class 0"]):
            if self._params13_index(index)["bal_mode"] <= 0:
                continue
            buy_w = sell_w = 0
            ratio = []
            for (v, book_id), memory in self.memory.items():
                if v != validator or self._class_index(book_id) != index or not memory.get("b13_fills"):
                    continue
                d, n = self._balance(memory, index)
                buy_w += d < 0
                sell_w += d > 0
                fills = memory["b13_fills"]
                b = max(0.0, sum(c for _, x, c in fills if x))
                s = max(0.0, sum(c for _, x, c in fills if not x))
                ratio.append((2 * min(b, s), b + s))
            paid = sum(x for x, _ in ratio) / max(sum(y for _, y in ratio), 1e-9)
            parts.append(f"{name}: buy windows {buy_w}, sell windows {sell_w}, paid share {paid:.2f}, off "
                         f"{stats.get(f'b13_off_c{index}', 0)}, cut {stats.get(f'b13_cut_c{index}', 0)}")
        return card + (" | 13GPT " + "; ".join(parts) if parts else "")


if __name__ == "__main__":
    launch(MyAgent10_26_13Gpt)
