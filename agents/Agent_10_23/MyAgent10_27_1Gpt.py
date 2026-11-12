# SPDX-License-Identifier: MIT
"""
MyAgent10_26_5Gpt with an exit band at the volume stop: before a book pauses for the validator's volume cap, its
inventory is brought back to its own recent mean, so it does not sit frozen while the skill window still counts it.
Why (27 October, agents/data/replays/shield_grid_log.md free-time #104): the validator refuses everything but cancels
once a book's volume reaches 10 x miner wealth (500k; taos/im/validator/query.py:398) and 9Gpt pauses at
turnover_margin 0.9 of it (450k), cancelling both sides and keeping its inventory. The skill leg's per-book alpha in the
3 sim h kappa window is sum (q_t - mean q) dp_t (debeta.book_alphas_by_book), and the book stays in the pool while it
has fills inside the window, so a frozen q adds (q_stop - mean q) x the later drift: on UID 229's c0 books that term
has sd 14.8 against 16.0 for a whole window's alpha, and costs ~3.6% of kappa with 11 books stopped; exiting to the
window's mean inventory first leaves sd 1.8 (GLFT finite-horizon logic: a known stop with a frozen position is a
terminal penalty on (q - mean q)^2, research run 54).
How: each book keeps the mean of its raw inventory (BASE beyond the initial BASE at mid, share of wealth, as 9Gpt's
_inventory_gap before 5Gpt's target) over the last `exit_mean_s` sim seconds (default 10800, the kappa window) in
60 s buckets. When the volume pause would start for the cap or the 24 h limit ('cap' / 'day') and the book has used
less than `exit_band_hi` x the cap, and |inventory - mean| is above `exit_tol` (default 0.005 of wealth; the class's
size_fraction was too loose - with the quiet multiplier it reached 0.06, above most deviations, free-time #106), the book keeps quoting ONLY the side that moves the inventory toward the mean; the other side is
cancelled. Past `exit_band_hi` x the cap, or once within the tolerance, it pauses as before. `exit_band_hi` 0 (the
default) is exactly 10_26_5Gpt. It must stay below 1.0 (the validator refuses all but cancels at the cap).
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_27_1Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 exit_band_hi=0.97
"""
import contextvars
import importlib.util
import math
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

_spec = importlib.util.spec_from_file_location("MyAgent10_26_5Gpt", Path(__file__).with_name("MyAgent10_26_5Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)

_BUCKET_NS = 60_000_000_000
_MEMORY = contextvars.ContextVar("my_agent_10_27_1_memory", default=None)


class MyAgent10_27_1Gpt(_parent.MyAgent10_26_5Gpt):
    """MyAgent10_26_5Gpt with an exit band before the volume pause; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.exit_band_hi = min(0.99, float(getattr(self.config, "exit_band_hi", 0) or 0))
        self.exit_mean_s  = float(getattr(self.config, "exit_mean_s", 10_800.0))
        self.exit_tol     = float(getattr(self.config, "exit_tol", 0.005))
        if 0 < self.exit_band_hi <= self.turnover_margin:
            bt.logging.warning(f"10_27_1Gpt: exit_band_hi {self.exit_band_hi:g} <= turnover_margin "
                               f"{self.turnover_margin:g}: the band is empty")
        bt.logging.info("10_27_1Gpt: exit band "
                        + (f"{self.turnover_margin:g}-{self.exit_band_hi:g} of the cap, toward the {self.exit_mean_s:g} s "
                           f"mean inventory" if self.exit_band_hi > 0 else "off"))

    def _raw_inventory(self, account, memory, mid):
        """9Gpt's _inventory_gap (BASE beyond the initial BASE at mid, share of wealth), without 5Gpt's target."""
        wealth = account.base_balance.total * mid + account.quote_balance.total
        initial = getattr(account.base_balance, "initial", None)
        if initial is None:
            initial = memory.get("start_base")
        if initial is None or not (wealth > 0):
            return None
        return (account.base_balance.total - initial) * mid / wealth

    def _quote_book_2(self, response, validator, book_id, book, memory, account=None):
        if self.exit_band_hi <= 0:
            return super()._quote_book_2(response, validator, book_id, book, memory, account)
        acct = account if account is not None else self._accounts().get(book_id)
        mid = memory.get("mid")
        q = self._raw_inventory(acct, memory, mid) if acct is not None and mid and mid > 0 else None
        memory["exit_q"] = q if q is not None and math.isfinite(q) else None
        if memory["exit_q"] is not None:
            buckets = memory.setdefault("exit_buckets", {})
            index = memory["timestamp"] // _BUCKET_NS
            total, n = buckets.get(index, (0.0, 0))
            buckets[index] = (total + memory["exit_q"], n + 1)
            oldest = index - int(self.exit_mean_s * 1e9 // _BUCKET_NS)
            for old in [i for i in buckets if i <= oldest or i > index]:
                del buckets[old]
        token = _MEMORY.set(memory)
        try:
            return super()._quote_book_2(response, validator, book_id, book, memory, account)
        finally:
            _MEMORY.reset(token)

    def _over_volume_pace(self, book_id, account, memory):
        paused = super()._over_volume_pace(book_id, account, memory)
        memory["exit_dir"] = None
        if not paused or self.exit_band_hi <= 0 or memory.get("pause_reason") not in ("cap", "day"):
            return paused
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        q, buckets = memory.get("exit_q"), memory.get("exit_buckets") or {}
        if wealth <= 0 or q is None or not buckets:
            return paused
        cap = self.turnover_cap * wealth
        used = max(account.traded_volume or 0.0, sum((memory.get("pvolume") or {}).values()))
        if used >= self.exit_band_hi * cap:
            return paused
        n = sum(c for _, c in buckets.values())
        mean = sum(s for s, _ in buckets.values()) / n if n else q
        if abs(q - mean) <= self.exit_tol:
            return paused
        memory["exit_dir"] = OrderDirection.SELL if q > mean else OrderDirection.BUY
        memory["pause_reason"] = None
        return False

    def _plan_side(self, direction, orders, target, base_quantity, ctx):
        memory = _MEMORY.get()
        side = memory.get("exit_dir") if memory is not None else None
        if side is not None and direction != side:
            self._count(ctx["validator"], "exit_band_blocked")
            return (_KEEP_CANCEL, orders, None) if orders else (_SKIP, [], None)
        if side is not None:
            self._count(ctx["validator"], "exit_band_side")
        return super()._plan_side(direction, orders, target, base_quantity, ctx)

    def _plan_card(self, validator):
        card = super()._plan_card(validator)
        if self.exit_band_hi <= 0:
            return card
        stats = self.plan_stats.get(validator, {})
        return (card + f" | 1GPT27 exit band to {self.exit_band_hi:g} x cap: exit side planned "
                f"{stats.get('exit_band_side', 0)}, other side cancelled {stats.get('exit_band_blocked', 0)}")


_plan_globals = _parent.MyAgent10_26_5Gpt._plan_side.__globals__
_KEEP_CANCEL, _SKIP = _plan_globals["CANCEL_ONLY"], _plan_globals["SKIP"]


if __name__ == "__main__":
    launch(MyAgent10_27_1Gpt)
