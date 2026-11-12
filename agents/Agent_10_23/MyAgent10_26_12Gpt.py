# SPDX-License-Identifier: MIT
"""
MyAgent10_26_11Gpt that never chases the price: "even if the price goes up, don't buy it; sell it, for the next
chance" (user, 26 October).

On a direction-locked book (11Gpt's `dir_mode` / `dir_long` / `dir_short`), with `dir_no_chase=1`:
  - no averaging up: an open long adds only at or below its average cost x (1 - `dir_chase_bps`), an open short
    only at or above cost x (1 + `dir_chase_bps`); a long whose price has risen sells (its exit, floored at cost by
    11Gpt) and does not buy more;
  - no buying back higher: once a long has been sold down to flat at price P, a new long entry bids at most
    P x (1 - `dir_chase_bps`); once a short has been bought back at P, a new short entry asks at least
    P x (1 + `dir_chase_bps`). The other entry stays open (mode 5), so after selling into a rise the book can sell
    more there and buy back lower. The exit price is forgotten after `dir_exit_memory_s` sim seconds (0 = kept until
    the next position opens);
  - `dir_mode=6` (fade): while flat, enter short after the price has risen (mom300 >= `dir_enter_bps`), long after
    it has fallen, both entries in between;
  - `dir_patience_s` > 0: the exit floor slides from the cost to the mid over that many sim seconds after the
    position opened, so a position the market ran away from is not held forever (the validator pays each book
    2 x min(buy, sell capture): a book stuck on one side earns nothing).

With dir_no_chase=0 and no dir_mode 6 it places exactly 11Gpt's orders.

Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_26_12Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 dir_mode=6
"""
import importlib.util
import math
from pathlib import Path

import bittensor as bt
from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

_spec = importlib.util.spec_from_file_location("MyAgent10_26_11Gpt", Path(__file__).with_name("MyAgent10_26_11Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)
LONG, SHORT = _parent.LONG, _parent.SHORT

CLASS12_PARAMS = {
    "dir_no_chase":      1.0,   # 0 = 11Gpt
    "dir_chase_bps":     0.0,   # adds and re-entries at least this much better than the cost / the last exit
    "dir_exit_memory_s": 0.0,   # sim seconds the last exit price binds a re-entry (0 = until a new position opens)
    "dir_patience_s":    0.0,   # > 0: the exit floor slides from the cost to the mid over this many sim seconds
}


class MyAgent10_26_12Gpt(_parent.MyAgent10_26_11Gpt):
    """MyAgent10_26_11Gpt that adds and re-enters only at better prices, with a fade entry mode; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.class12_params = {}
        p = self._params12_index(0)
        bt.logging.info(f"12Gpt: no chase {int(p['dir_no_chase'])}, margin {p['dir_chase_bps']:g} bps, exit memory "
                        f"{p['dir_exit_memory_s']:g} s (class 0; _c<k> overrides per class)")

    def _params12_index(self, index):
        params = self.class12_params.get(index)
        if params is None:
            params = {}
            for name, default in CLASS12_PARAMS.items():
                base = float(getattr(self.config, name, default))
                params[name] = float(getattr(self.config, f"{name}_c{index}", base))
            self.class12_params[index] = params
        return params

    def _sync_config(self, state):
        before = self._layout
        super()._sync_config(state)
        if self._layout != before:
            self.class12_params.clear()

    def _book_cost(self, memory, account, mid):
        """11Gpt's cost, and the price of the last fill that closed (part of) a position."""
        last, held = memory.get("d11_last"), memory.get("d11_pos", 0.0)
        position, cost = super()._book_cost(memory, account, mid)
        if last is not None:
            change = account.base_balance.total - last[0]
            if held and abs(change) > 1e-12 and (held > 0) != (change > 0):
                price = -(account.quote_balance.total - last[1]) / change
                if not (0.5 * mid < price < 2.0 * mid):
                    price = mid
                memory["d12_exit"] = (LONG if held > 0 else SHORT, price, memory["timestamp"])
            elif held == 0 and abs(change) > 1e-12:
                memory.pop("d12_exit", None)          # a new position: the old exit no longer binds
        if abs(position) <= 0:
            memory.pop("d12_opened", None)
        elif not held or (held > 0) != (position > 0):
            memory["d12_opened"] = memory["timestamp"]
        return position, cost

    def _plan_side(self, direction, orders, target, base_quantity, ctx):
        validator, book_id = ctx["validator"], ctx.get("book_id")
        memory = self.memory.get((validator, book_id))
        lock = memory.get("d11_lock") if memory is not None else None
        if lock is None or not math.isfinite(target) or target <= 0:
            return super()._plan_side(direction, orders, target, base_quantity, ctx)
        index = self._class_index(book_id)
        if lock["mode"] == 6 and lock["flat"] and not lock.get("d12_faded"):
            p11 = self._params11_index(index)
            momentum = memory.get("mom300") or 0.0
            if abs(momentum) >= p11["dir_enter_bps"]:
                lock["entries"] = {SHORT} if momentum > 0 else {LONG}
            lock["d12_faded"] = True
        p = self._params12_index(index)
        side = LONG if direction == OrderDirection.BUY else SHORT
        if p["dir_patience_s"] > 0 and not lock["flat"] and lock["cost"] is not None \
                and side != (LONG if lock["position"] > 0 else SHORT):
            return self._patient_exit(direction, orders, target, base_quantity, ctx, lock, memory, p, index)
        if p["dir_no_chase"] <= 0:
            return super()._plan_side(direction, orders, target, base_quantity, ctx)
        margin = p["dir_chase_bps"] / 1e4
        decimals = ctx["decimals"]
        reference = None
        if not lock["flat"]:
            if side == (LONG if lock["position"] > 0 else SHORT) and lock["cost"] is not None:
                reference = lock["cost"]                  # adding to the position
        else:
            exit_ = memory.get("d12_exit")
            if exit_ is not None and exit_[0] == side:
                age = (memory["timestamp"] - exit_[2]) / 1e9
                if p["dir_exit_memory_s"] <= 0 or age <= p["dir_exit_memory_s"]:
                    reference = exit_[1]                  # re-entering the side just closed
        if reference is not None:
            if side == LONG:
                cap = self._on_grid(reference * (1 - margin), decimals, True)
                if cap < target:
                    self._count(validator, f"d12_nochase_c{index}")
                    target = cap
            else:
                cap = self._on_grid(reference * (1 + margin), decimals, False)
                if cap > target:
                    self._count(validator, f"d12_nochase_c{index}")
                    target = cap
        return super()._plan_side(direction, orders, target, base_quantity, ctx)

    def _patient_exit(self, direction, orders, target, base_quantity, ctx, lock, memory, p, index):
        """11Gpt's exit with the cost it floors at moved toward the mid by the share of `dir_patience_s` the position
        has been open, when the mid is on the losing side of the cost (a winning position keeps its floor)."""
        cost, mid = lock["cost"], memory["mid"]
        opened = memory.get("d12_opened")
        losing = mid < cost if lock["position"] > 0 else mid > cost
        if opened is None or not losing:
            return super()._plan_side(direction, orders, target, base_quantity, ctx)
        share = min(1.0, max(0.0, (memory["timestamp"] - opened) / 1e9 / p["dir_patience_s"]))
        if share > 0:
            self._count(ctx["validator"], f"d12_patience_c{index}")
        lock["cost"] = cost + share * (mid - cost)
        try:
            return super()._plan_side(direction, orders, target, base_quantity, ctx)
        finally:
            lock["cost"] = cost

    def _plan_card(self, validator):
        card = super()._plan_card(validator)
        stats = self.plan_stats.get(validator, {})
        held = [f"{name} {stats.get(f'd12_nochase_c{i}', 0)}" for i, name in enumerate(self._class_names() or ["class 0"])
                if stats.get(f"d12_nochase_c{i}")]
        return card + (" | 12GPT held back (no chase): " + ", ".join(held) if held else "")


if __name__ == "__main__":
    launch(MyAgent10_26_12Gpt)
