# SPDX-License-Identifier: MIT
"""
MyAgent10_23_9Gpt with direction-locked books: on a locked book the agent holds only one kind of position, long or
short, keeps the position's average entry price, and closes it only at a price better than that cost.

On a locked book (user idea, 26 October):
  - while the book is flat (its BASE within one minimum order of the initial BASE), its entry side is chosen: long
    (bids only) or short (asks only), by `dir_mode` below; with no clear choice both entries stay open and the
    first fill decides;
  - once a position is open, the side that adds to it is quoted as 9Gpt quotes it (up to 9Gpt's inventory band),
    and the side that closes it is an exit: never larger than the position, and priced at or beyond
    cost x (1 + maker fee + `dir_margin_bps`) for a long (cost x (1 - ...) for a short), so every exit fill closes
    lots at a profit after fees;
  - the lock holds until the position is closed again.

`dir_mode` (per class with `dir_mode_c<k>`; `dir_long` / `dir_short` list book ids that are always long / short):
    0  off: 9Gpt's two-sided quoting
    1  enter on the side the published fundamental pulls the price, when |fundamental gap| >= `dir_enter_bps`
    2  enter with the slow momentum (mom300, decayed bps over ~300 s), when |mom300| >= `dir_enter_bps`
    3  long only
    4  short only
    5  no signal: both entries while flat, the first fill decides
With mode 1 or 2 and a weak signal the book behaves as mode 5.

The cost is the position's average entry price, fees included: each round the change in the account's BASE and
QUOTE totals since the last round is booked as one fill at -dQUOTE / dBASE (a round with both sides filled can give
a meaningless price; outside half to twice the mid it is booked at the mid). A position found without history (a
restart, or the first round of a simulation) starts at the mid.

The risk the floor takes: a position that runs against its cost is never closed at a loss, so it can sit at the
inventory band while its exit rests far from the touch. `dir_stop_bps` > 0 lifts the floor once the mid is that
many bps beyond the cost (0 = never).

With dir_mode=0 and no dir_long / dir_short it places exactly 9Gpt's orders.

Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_26_11Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 dir_mode=5
"""
import importlib.util
import math
import re
from pathlib import Path

import bittensor as bt
from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

_spec = importlib.util.spec_from_file_location("MyAgent10_23_9Gpt", Path(__file__).with_name("MyAgent10_23_9Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)
CANCEL_ONLY, SKIP = _parent.CANCEL_ONLY, _parent.SKIP

CLASS11_PARAMS = {
    "dir_mode":       0.0,   # see the module docstring
    "dir_enter_bps":  20.0,  # signal size (fundamental gap or mom300, bps) that picks the entry side while flat
    "dir_margin_bps": 1.0,   # an exit is at least this far beyond the cost, on top of the maker fee
    "dir_stop_bps":   0.0,   # > 0: the floor is lifted once the mid is this far beyond the cost against the position
}
LONG, SHORT = 1, -1


def _book_ids(value):
    """Book ids from a param: '3,7', '3:7', or a single number (the miner's parser turns '3' into 3.0)."""
    if value is None or value == "":
        return set()
    if isinstance(value, (int, float)):
        return {int(value)}
    return {int(float(x)) for x in re.split(r"[,:;/ ]+", str(value)) if x.strip()}


class MyAgent10_26_11Gpt(_parent.MyAgent10_23_9Gpt):
    """MyAgent10_23_9Gpt with direction-locked books and cost-floored exits; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.class11_params = {}
        self.dir_long = _book_ids(getattr(self.config, "dir_long", ""))
        self.dir_short = _book_ids(getattr(self.config, "dir_short", ""))
        p = self._params11_index(0)
        bt.logging.info(f"11Gpt: direction lock mode {p['dir_mode']:g} (class 0; dir_mode_c<k> per class), enter "
                        f"{p['dir_enter_bps']:g} bps, exit margin {p['dir_margin_bps']:g} bps + fee, stop "
                        f"{p['dir_stop_bps']:g} bps; long books {sorted(self.dir_long)}, short books {sorted(self.dir_short)}")

    def _params11_index(self, index):
        params = self.class11_params.get(index)
        if params is None:
            params = {}
            for name, default in CLASS11_PARAMS.items():
                base = float(getattr(self.config, name, default))
                params[name] = float(getattr(self.config, f"{name}_c{index}", base))
            self.class11_params[index] = params
        return params

    def _sync_config(self, state):
        before = self._layout
        super()._sync_config(state)
        if self._layout != before:
            self.class11_params.clear()

    def _mode(self, book_id):
        if book_id in self.dir_long:
            return 3
        if book_id in self.dir_short:
            return 4
        return int(self._params11_index(self._class_index(book_id))["dir_mode"])

    # ── The position and its cost, once per round ───────────────────────────────────────────────────────────

    def _book_cost(self, memory, account, mid):
        """Books the BASE / QUOTE change since the last round as one fill; returns (position, average cost)."""
        base, quote = account.base_balance.total, account.quote_balance.total
        initial = getattr(account.base_balance, "initial", None)
        if initial is None:
            if memory["start_base"] is None:
                memory["start_base"] = base
            initial = memory["start_base"]
        position = base - initial
        last = memory.get("d11_last")
        cost = memory.get("d11_cost")
        if last is None:
            cost = mid if abs(position) > 0 else None
        else:
            held = memory.get("d11_pos", 0.0)
            change = base - last[0]
            if abs(change) > 1e-12:
                price = -(quote - last[1]) / change
                if not (0.5 * mid < price < 2.0 * mid):
                    price = mid
                if held == 0 or cost is None:
                    cost = price
                elif (held > 0) == (change > 0):
                    # adding: the average of the old lots and the new one
                    cost = (cost * abs(held) + price * abs(change)) / max(abs(held + change), 1e-12)
                elif abs(change) > abs(held):
                    # through flat: the remainder is a new position at this fill's price
                    cost = price
        memory["d11_last"] = (base, quote)
        memory["d11_pos"] = position
        memory["d11_cost"] = cost if abs(position) > 0 else None
        return position, memory["d11_cost"]

    def _quote_book(self, response, validator, book_id, book, memory, account=None):
        account = account if account is not None else self._accounts()[book_id]
        mode = self._mode(book_id)
        mid = memory["mid"]
        if mode == 0 or not (math.isfinite(mid) and mid > 0):
            memory["d11_lock"] = None
            return super()._quote_book(response, validator, book_id, book, memory, account)
        position, cost = self._book_cost(memory, account, mid)
        minimum = max(self.min_order_size(book_id), 10 ** -self.volume_decimals(book_id))
        p = self._params11_index(self._class_index(book_id))
        lock = {"mode": mode, "position": position, "cost": cost, "flat": abs(position) < minimum}
        if lock["flat"]:
            if mode == 3:
                entries = {LONG}
            elif mode == 4:
                entries = {SHORT}
            else:
                signal = 0.0
                if mode == 1:
                    fundamental = getattr(book, "fundamental", None)
                    signal = (fundamental - mid) / mid * 1e4 if fundamental and fundamental > 0 else 0.0
                elif mode == 2:
                    signal = memory.get("mom300") or 0.0
                strong = mode in (1, 2) and abs(signal) >= p["dir_enter_bps"]
                entries = ({LONG} if signal > 0 else {SHORT}) if strong else {LONG, SHORT}
            lock["entries"] = entries
        memory["d11_lock"] = lock
        return super()._quote_book(response, validator, book_id, book, memory, account)

    # ── Sides ───────────────────────────────────────────────────────────────────────────────────────────────────

    def _plan_side(self, direction, orders, target, base_quantity, ctx):
        validator, book_id = ctx["validator"], ctx.get("book_id")
        memory = self.memory.get((validator, book_id))
        lock = memory.get("d11_lock") if memory is not None else None
        if lock is None or not math.isfinite(target) or target <= 0:
            return super()._plan_side(direction, orders, target, base_quantity, ctx)
        side = LONG if direction == OrderDirection.BUY else SHORT
        index = self._class_index(book_id)
        if lock["flat"]:
            if side not in lock["entries"]:
                self._count(validator, f"d11_blocked_c{index}")
                return (CANCEL_ONLY, orders, None) if orders else (SKIP, [], None)
            return super()._plan_side(direction, orders, target, base_quantity, ctx)
        held = LONG if lock["position"] > 0 else SHORT
        if side == held:
            # adding: a fixed-direction book does not add to a position on its other side
            if (lock["mode"] == 3 and side == SHORT) or (lock["mode"] == 4 and side == LONG):
                self._count(validator, f"d11_blocked_c{index}")
                return (CANCEL_ONLY, orders, None) if orders else (SKIP, [], None)
            return super()._plan_side(direction, orders, target, base_quantity, ctx)

        # the exit: no larger than the position, no better for the taker than the cost plus fee and margin
        cost = lock["cost"]
        quantity = min(base_quantity, abs(lock["position"]))
        if cost is None:
            return super()._plan_side(direction, orders, target, quantity, ctx)
        p = self._params11_index(index)
        account = ctx["account"]
        fee = max(0.0, account.fees.maker_fee_rate) if account.fees is not None else 0.0
        edge = fee + p["dir_margin_bps"] / 1e4
        mid = memory["mid"]
        against = (cost - mid) / cost * 1e4 if held == LONG else (mid - cost) / cost * 1e4
        if p["dir_stop_bps"] > 0 and against >= p["dir_stop_bps"]:
            self._count(validator, f"d11_stop_c{index}")
            return super()._plan_side(direction, orders, target, quantity, ctx)
        decimals = ctx["decimals"]
        if held == LONG:
            floor = self._on_grid(cost * (1 + edge), decimals, False)
            if floor > target:
                self._count(validator, f"d11_floored_c{index}")
                target = floor
        else:
            ceiling = self._on_grid(cost * (1 - edge), decimals, True)
            if ceiling < target:
                self._count(validator, f"d11_floored_c{index}")
                target = ceiling
        return super()._plan_side(direction, orders, target, quantity, ctx)

    # ── Score card ──────────────────────────────────────────────────────────────────────────────────────────────

    def _plan_card(self, validator):
        card = super()._plan_card(validator)
        stats = self.plan_stats.get(validator, {})
        parts = []
        for index, name in enumerate(self._class_names() or ["class 0"]):
            longs = shorts = 0
            gaps = []
            for (v, book_id), memory in self.memory.items():
                lock = memory.get("d11_lock") if v == validator else None
                if lock is None or self._class_index(book_id) != index or lock["flat"]:
                    continue
                longs += lock["position"] > 0
                shorts += lock["position"] < 0
                if lock["cost"]:
                    sign = 1 if lock["position"] > 0 else -1
                    gaps.append(sign * (memory["mid"] - lock["cost"]) / lock["cost"] * 1e4)
            if not (longs or shorts or stats.get(f"d11_floored_c{index}")):
                continue
            mean = sum(gaps) / len(gaps) if gaps else 0.0
            parts.append(f"{name}: long {longs} short {shorts}, open vs cost {mean:+.1f} bps, floored "
                         f"{stats.get(f'd11_floored_c{index}', 0)}, blocked {stats.get(f'd11_blocked_c{index}', 0)}, "
                         f"stops {stats.get(f'd11_stop_c{index}', 0)}")
        return card + (" | 11GPT " + "; ".join(parts) if parts else "")


if __name__ == "__main__":
    launch(MyAgent10_26_11Gpt)
