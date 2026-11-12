# SPDX-License-Identifier: MIT
"""
MyAgent10_26_13Gpt with "don't chase" on ordinary two-sided books: no direction lock, only the add side capped at the
position's average cost (user, 26 October: "even if the price goes up, don't buy it, but sell it for the next chance").

Why (replay of the 25 October mainnet recording, 4715 rounds, against 9Gpt's live set): 12Gpt's lock with no-chase
on classes 1 and 2 lifted their net alpha by +213, but the lock made class 2 one-sided and its making (2 x min(buy,
sell capture) per book) fell from 6.5 to 0, so the whole was about even. Here both sides keep quoting as 9Gpt
quotes them; with `nc_mode=1` (per class `nc_mode_c<k>`):
  - while long (BASE above the initial), the bid is at most cost x (1 - `nc_bps`): no buying above what the
    position cost;
  - while short, the ask is at least cost x (1 + `nc_bps`): no selling below what the short was sold at;
  - the side that reduces the position is quoted as 9Gpt quotes it, floor-free, so the book stays two-sided.
The cost is 11Gpt's: the position's average entry price from the round's BASE / QUOTE change, fees included.

With nc_mode=0 it places exactly 13Gpt's orders.

Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_26_14Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 nc_mode=1
"""
import importlib.util
import math
from pathlib import Path

import bittensor as bt
from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

_spec = importlib.util.spec_from_file_location("MyAgent10_26_13Gpt", Path(__file__).with_name("MyAgent10_26_13Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)

CLASS14_PARAMS = {
    "nc_mode": 0.0,   # 0 = 13Gpt
    "nc_bps":  0.0,   # adds at least this much better than the cost
}


class MyAgent10_26_14Gpt(_parent.MyAgent10_26_13Gpt):
    """MyAgent10_26_13Gpt with the add side capped at the position's cost on two-sided books."""

    def initialize(self):
        super().initialize()
        self.class14_params = {}
        p = self._params14_index(0)
        bt.logging.info(f"14Gpt: two-sided no-chase {int(p['nc_mode'])}, margin {p['nc_bps']:g} bps "
                        f"(class 0; nc_mode_c<k> per class)")

    def _params14_index(self, index):
        params = self.class14_params.get(index)
        if params is None:
            params = {}
            for name, default in CLASS14_PARAMS.items():
                base = float(getattr(self.config, name, default))
                params[name] = float(getattr(self.config, f"{name}_c{index}", base))
            self.class14_params[index] = params
        return params

    def _sync_config(self, state):
        before = self._layout
        super()._sync_config(state)
        if self._layout != before:
            self.class14_params.clear()

    def _quote_book(self, response, validator, book_id, book, memory, account=None):
        memory["n14"] = None
        mid = memory["mid"]
        if self._params14_index(self._class_index(book_id))["nc_mode"] > 0 and math.isfinite(mid) and mid > 0:
            account = account if account is not None else self._accounts()[book_id]
            # the lock books the same round again when it is on too; the second call sees no change
            position, cost = self._book_cost(memory, account, mid)
            minimum = max(self.min_order_size(book_id), 10 ** -self.volume_decimals(book_id))
            if cost is not None and abs(position) >= minimum:
                memory["n14"] = (position, cost)
        return super()._quote_book(response, validator, book_id, book, memory, account)

    def _plan_side(self, direction, orders, target, base_quantity, ctx):
        memory = self.memory.get((ctx["validator"], ctx.get("book_id")))
        held = memory.get("n14") if memory is not None else None
        if held is not None and math.isfinite(target) and target > 0:
            position, cost = held
            index = self._class_index(ctx.get("book_id"))
            margin = self._params14_index(index)["nc_bps"] / 1e4
            if position > 0 and direction == OrderDirection.BUY:
                cap = self._on_grid(cost * (1 - margin), ctx["decimals"], True)
                if cap < target:
                    self._count(ctx["validator"], f"n14_capped_c{index}")
                    target = cap
            elif position < 0 and direction == OrderDirection.SELL:
                cap = self._on_grid(cost * (1 + margin), ctx["decimals"], False)
                if cap > target:
                    self._count(ctx["validator"], f"n14_capped_c{index}")
                    target = cap
        return super()._plan_side(direction, orders, target, base_quantity, ctx)

    def _plan_card(self, validator):
        card = super()._plan_card(validator)
        stats = self.plan_stats.get(validator, {})
        capped = [f"{name} {stats.get(f'n14_capped_c{i}', 0)}" for i, name in enumerate(self._class_names() or ["class 0"])
                  if stats.get(f"n14_capped_c{i}")]
        return card + (" | 14GPT adds capped at cost: " + ", ".join(capped) if capped else "")


if __name__ == "__main__":
    launch(MyAgent10_26_14Gpt)
