# SPDX-License-Identifier: MIT
"""
MyAgent10_26_5Gpt with a maker-side "fundamental entry": after a reseed moves the published fundamental, for a few
seconds the book quotes only the side that builds a position in the jump's direction.
Why (27 October, the user's idea "make skill with a long / short position when the fundamental moves"): after a reseed
jump the class 0 mid keeps moving the jump's way (~+1.2 bps over 10 s, shield_grid_log 26 Oct 18:55), which a TAKE cannot
collect inside the score (the 2.3 bps per-notional skill hurdle, scoring.debeta.skill_hurdle_bps, and a take's negative
capture). A maker can: an up-jump leaves only the bid quoted - filled while the price is still near the old level, held
while it moves up - and the other side, the one the move runs over, is cancelled. The earlier "quote the safe side DURING
the shield" (5Gpt shield_side, Z1) lost 18-30%: in the first seconds the safe-side quote fills mostly when the price turns
against the jump. So the entry window starts when the shield ends, and the entry quote may be moved closer to the touch.
How: per book, the fundamental at the start of each `reseed_s` cycle is kept; a new cycle whose fundamental moved by at
least `fund_entry_min_bps_c<k>` (default 2) opens the entry window from `fund_entry_from_s_c<k>` (default 5) to
`fund_entry_to_s_c<k>` (default 15) sim seconds into the cycle. Inside it the side against the jump is cancelled / not
placed, and the entry side's price moves `fund_entry_ticks_c<k>` ticks (default 0) toward the touch, never crossing it
(the cross guard), with its size x `fund_entry_size_c<k>` (default 1). A class without `fund_entry_c<k>=1` quotes
exactly as 10_26_5Gpt.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_27_3Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 fund_entry_c0=1 fund_entry_ticks_c0=2
"""
import contextvars
import importlib.util
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

_spec = importlib.util.spec_from_file_location("MyAgent10_26_5Gpt", Path(__file__).with_name("MyAgent10_26_5Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)

_ENTRY = contextvars.ContextVar("my_agent_10_27_3_entry", default=None)


class MyAgent10_27_3Gpt(_parent.MyAgent10_26_5Gpt):
    """MyAgent10_26_5Gpt with the maker-side fundamental entry; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.fund_entry = {}
        for i in range(16):
            if float(getattr(self.config, f"fund_entry_c{i}", 0) or 0) > 0:
                self.fund_entry[i] = {
                    "min": float(getattr(self.config, f"fund_entry_min_bps_c{i}", 2.0)),
                    "from": float(getattr(self.config, f"fund_entry_from_s_c{i}", 5.0)),
                    "to": float(getattr(self.config, f"fund_entry_to_s_c{i}", 15.0)),
                    "ticks": int(float(getattr(self.config, f"fund_entry_ticks_c{i}", 0))),
                    "size": float(getattr(self.config, f"fund_entry_size_c{i}", 1.0)),
                }
        bt.logging.info("10_27_3Gpt: fundamental entry "
                        + (", ".join(f"c{i} |jump| >= {p['min']:g} bps, {p['from']:g}-{p['to']:g} s, {p['ticks']} ticks, "
                                     f"size x{p['size']:g}" for i, p in sorted(self.fund_entry.items())) or "off"))

    def _entry_side(self, book_id, book, memory):
        """The side to quote alone this round, or None."""
        p = self.fund_entry.get(self._class_index(book_id))
        fundamental = getattr(book, "fundamental", None)
        if p is None or not fundamental or fundamental <= 0:
            return None, None
        seconds = memory["timestamp"] / 1e9
        cycle = int(seconds // self.reseed_s)
        if memory.get("fe_cycle") != cycle:
            last = memory.get("fe_fund")
            memory["fe_jump"] = (fundamental - last) / last * 1e4 if last else 0.0
            memory["fe_cycle"], memory["fe_fund"] = cycle, fundamental
        jump, phase = memory.get("fe_jump") or 0.0, seconds % self.reseed_s
        if abs(jump) < p["min"] or not (p["from"] <= phase < p["to"]):
            return None, p
        return (OrderDirection.BUY if jump > 0 else OrderDirection.SELL), p

    def _quote_book_2(self, response, validator, book_id, book, memory, account=None):
        if not self.fund_entry:
            return super()._quote_book_2(response, validator, book_id, book, memory, account)
        side, p = self._entry_side(book_id, book, memory)
        if side is not None:
            self._count(validator, "fund_entry_rounds")
        token = _ENTRY.set((side, p) if side is not None else None)
        try:
            return super()._quote_book_2(response, validator, book_id, book, memory, account)
        finally:
            _ENTRY.reset(token)

    def _plan_side(self, direction, orders, target, base_quantity, ctx):
        entry = _ENTRY.get()
        if entry is None:
            return super()._plan_side(direction, orders, target, base_quantity, ctx)
        side, p = entry
        if direction != side:
            self._count(ctx["validator"], "fund_entry_cancelled")
            return (_CANCEL_ONLY, orders, None) if orders else (_SKIP, [], None)
        if p["ticks"]:
            decimals = ctx["decimals"]
            tick = 10 ** -decimals
            if direction == OrderDirection.BUY:
                target = min(round(target + p["ticks"] * tick, decimals), round(ctx["best_ask"] - ctx["guard"], decimals))
            else:
                target = max(round(target - p["ticks"] * tick, decimals), round(ctx["best_bid"] + ctx["guard"], decimals))
        return super()._plan_side(direction, orders, target, base_quantity * p["size"], ctx)

    def _plan_card(self, validator):
        card = super()._plan_card(validator)
        if not self.fund_entry:
            return card
        stats = self.plan_stats.get(validator, {})
        return (card + f" | 3GPT27 fundamental entry: {stats.get('fund_entry_rounds', 0)} book-rounds one-sided, "
                f"other side cancelled {stats.get('fund_entry_cancelled', 0)}")


_plan_globals = _parent.MyAgent10_26_5Gpt._plan_side.__globals__
_CANCEL_ONLY, _SKIP = _plan_globals["CANCEL_ONLY"], _plan_globals["SKIP"]


if __name__ == "__main__":
    launch(MyAgent10_27_3Gpt)
