# SPDX-License-Identifier: MIT
"""
MyAgent10_23IovC with its volume controls turned toward what the validator pays for.

The making pool pays each uid's share of realized spread (captured spread times one whole-uid markout quality,
clamped to 0..1) and the skill pool its share of positive net alpha (taos/im/validator/reward.py
making_pool_inputs / skill_pool_share, debeta.markout_quality): both grow with volume. On 24 October UID 105 was
pruned at trading rank 57 of 257 with a making share of 0.0013 against ~0.0039 for the average miner, so IovC's
parts that hold volume back cost more than the markout they protect:
  - token-bucket pacing held 60 s of the daily allowance, ~0.0063 x wealth, about one order: a single class 1 fill
    (0.012 x wealth) emptied it and the book went dark for ~1 minute, both sides cancelled, so the fill that
    opened the book's capture could not be matched on the other side (making credits the smaller side per book).
    The live miner used ~6% of the daily cap. `pace_bucket` is now an hour, and a pacing pause cancels only the
    side that adds to the inventory gap; the side that reduces it keeps quoting. The daily cap still stops both.
  - budget sizing made quotes smaller on the books that fill most often: off (`budget_sizing=0`).
  - spread multipliers could widen to 2 x 2 = 4x toward a 3 bps markout target: capped at x1.25 class and x1.25
    book as in MyAgent10_23_5Gpt, with a 2 bps target.
  - one order may be up to 3% of the book's wealth instead of 2% (`max_order_fraction`); on the 24 October replay
    of the live miner 2% capped every class 1 and class 2 quote once the size grew.
  - lazy_load is always off: IovC slices book levels, which lazily parsed books cannot do, so with it on every
    book failed and nothing was quoted (the same failure 4Gpt hit on the real miner).
Every value can still be set through --params; with pace_bucket=60 budget_sizing=1 adapt_max=2 book_mult_max=2
markout_target_bps=3 max_order_fraction=0.02 pace_one_sided=0 it places exactly MyAgent10_23IovC's orders.

Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_24_1IovC.py --port 8888 --agent_id 0 --params lazy_load=1
"""
import importlib.util
from pathlib import Path

from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection

# The parent is loaded from its file next to this one, so the agent works however its directory is put on the path
# (the miner's agent loader, the replay harness or a direct launch).
_spec = importlib.util.spec_from_file_location("MyAgent10_23IovC", Path(__file__).with_name("MyAgent10_23IovC.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)

# Defaults that differ from MyAgent10_23IovC; any of them passed in --params wins.
DEFAULTS = {
    "pace_bucket":        3600.0,
    "budget_sizing":      0.0,
    "adapt_max":          1.25,
    "book_mult_max":      1.25,
    "markout_target_bps": 2.0,
    "max_order_fraction": 0.03,
    "pace_one_sided":     1.0,
}


class MyAgent10_24_1IovC(_parent.MyAgent10_23IovC):
    """MyAgent10_23IovC with a looser pace, no budget sizing, capped spread multipliers and one-sided pacing pauses."""

    def initialize(self):
        """Fills in this agent's defaults for every parameter not passed, then reads them as the parent does."""
        for name, value in DEFAULTS.items():
            if not hasattr(self.config, name):
                setattr(self.config, name, value)
        super().initialize()
        # The quoting slices book levels (book.bids[:5]), which taos.im.protocol.lazy's levels do not support: with
        # lazy_load on every book failed with "unhashable type: 'slice'" and nothing was quoted.
        self.config.lazy_load = False
        self.pace_one_sided = float(getattr(self.config, "pace_one_sided", 1.0)) > 0
        # The book being quoted; set by _over_volume_pace, which _quote_book calls before either side is quoted.
        self._pacing_memory = None

    def _over_volume_pace(self, book_id, account, memory):
        """True only at the daily cap. A pacing pause instead marks the book, and `_quote_side` then quotes only the
        side that reduces the inventory gap, so a book that has just filled can still be filled back."""
        self._pacing_memory = memory
        memory["pace_paused"] = False
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        paced = super()._over_volume_pace(book_id, account, memory)
        if not paced or not self.pace_one_sided or wealth <= 0:
            return paced
        cap = self.turnover_cap * wealth
        day = sum(memory["volume"].values())
        if (account.traded_volume or 0.0) >= self.turnover_margin * cap or day >= self.turnover_margin * cap:
            return True
        memory["pace_paused"] = True
        return False

    def _quote_side(self, response, book_id, orders, target, direction, quantity, min_quantity, volume_decimals,
                    away, toward, free, expiry):
        """The parent's quote, except during a pacing pause, when the side that would add to the inventory gap (the
        bid when long, the ask when short, both when flat) is cancelled instead."""
        memory = self._pacing_memory
        if memory is not None and memory.get("pace_paused"):
            gap = memory["alpha"].get("inventory") or 0.0
            reduces = (direction == OrderDirection.SELL and gap > 0) or (direction == OrderDirection.BUY and gap < 0)
            if not reduces:
                self._cancel(response, book_id, orders)
                return False
        return super()._quote_side(response, book_id, orders, target, direction, quantity, min_quantity,
                                   volume_decimals, away, toward, free, expiry)


if __name__ == "__main__":
    launch(MyAgent10_24_1IovC)
