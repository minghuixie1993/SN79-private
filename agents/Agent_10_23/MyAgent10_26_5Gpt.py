# SPDX-License-Identifier: MIT
"""
MyAgent10_26_4Gpt with an optional inventory target per class that follows the published fundamental gap.
Why (26 October, agents/data/replays/shield_grid_log.md free-time #77): on UID 229's recording the class 2 inventory runs
AGAINST the gap that predicts its price - within each book, corr(position, gap) -0.12 while corr(gap, next 300 s
return) +0.18, and corr(position, next 300 s return) -0.11; the drift-stripped inventory alpha of class 2 was about
-190 QUOTE in 7 wall h (the validator pays inventory timing within a book: debeta.book_alphas_from_drift). The reseed
pulls the class 2 mid toward a fundamental that sits ~+275 bps away and closes slowly, so the side the move runs into
keeps getting filled and the book ends up short just before the price rises. The predictor leans the quotes by
coef_fund x gap for 20 s (about 0.1 bps per 30 bps of gap on class 2), far too little to hold inventory the right way
over the minutes the move takes.
How: Avellaneda-Stoikov with a drift - the inventory skew pulls toward a target q* instead of zero. For a class k with
`inv_target_k_c<k>` > 0 the inventory the skew, sizing and bands see is (inventory - q*), q* = clip(k x (gap - its
EWMA over `inv_target_tau_c<k>` sim s, default 7200), +/- `inv_target_max_c<k>`) as a share of the book's wealth, the gap
being (fundamental - mid) / mid in bps. The level is taken out because a steady position earns nothing once each book's
drift is stripped (and class 2's ~+275 bps level would pin q* at its cap); within-book IC at 300 s of the gap minus a
7200 s EWMA is +0.11 (1800 s: 0.00, the useful variation is slow). With `inv_target_hold_s_c<k>` > 0 the target is held
fixed for blocks of that many sim seconds and re-set only at each block's first round (a set position held for a block
of time, the user's idea of 26 Oct) - the validator pays inventory timing against the window's drift, so a held block
earns when its level was set the right way. With `inv_target_vol_c<k>=1` the target is also divided by the book's own
volatility over the hold (sigma in bps over `inv_target_hold_s_c<k>`, or 300 s; from the agent's EWMA variance) and k is
read as "share of wealth per sigma of deviation": Avellaneda-Stoikov's q* = mu / (gamma sigma^2) with mu ~ IC x sigma x
z, i.e. a noisier book holds less for the same signal (Kelly sizing, free-time #101). A class without
`inv_target_k_c<k>` behaves exactly as 10_26_4Gpt.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_26_5Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 inv_target_k_c2=0.001 inv_target_max_c2=0.06
"""
import importlib.util
import math
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch

_spec = importlib.util.spec_from_file_location("MyAgent10_26_4Gpt", Path(__file__).with_name("MyAgent10_26_4Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)


class MyAgent10_26_5Gpt(_parent.MyAgent10_26_4Gpt):
    """MyAgent10_26_4Gpt with a fundamental-gap inventory target per class; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.inv_target = {}
        for index in range(16):
            k = float(getattr(self.config, f"inv_target_k_c{index}", 0) or 0)
            if k > 0:
                self.inv_target[index] = (k, float(getattr(self.config, f"inv_target_max_c{index}", 0.06)),
                                          float(getattr(self.config, f"inv_target_tau_c{index}", 7200.0)),
                                          float(getattr(self.config, f"inv_target_hold_s_c{index}", 0.0)),
                                          int(float(getattr(self.config, f"inv_target_vol_c{index}", 0))))
        bt.logging.info("10_26_5Gpt: inventory target "
                        + (", ".join(f"c{i} {k:g} x (gap - EWMA {tau:g} s){' / sigma' if v else ''}, max {m:g}, hold {h:g} s" for i, (k, m, tau, h, v) in sorted(self.inv_target.items()))
                           or "off"))

    def _remember(self, validator, book_id, book, timestamp):
        memory = super()._remember(validator, book_id, book, timestamp)
        target = self.inv_target.get(memory.get("class_index")) if memory is not None else None
        if target is None:
            return memory
        fundamental, mid = getattr(book, "fundamental", None), memory.get("mid")
        if not (fundamental and fundamental > 0 and mid):
            memory["fund_dev"] = None
            return memory
        gap = (fundamental - mid) / mid * 1e4
        ewma, last = memory.get("inv_gap_ewma"), memory.get("inv_gap_ts")
        if ewma is None or last is None or timestamp < last:
            ewma = gap
        else:
            ewma += (1 - math.exp(-(timestamp - last) / 1e9 / target[2])) * (gap - ewma)
        memory["inv_gap_ewma"], memory["inv_gap_ts"] = ewma, timestamp
        hold = target[3]
        dev = gap - ewma
        if target[4]:
            # z-score of the deviation in units of the book's own move over the hold (Kelly: q* ~ mu / sigma^2)
            sigma = math.sqrt(memory["variance"] * max(hold, 300.0)) * 1e4 if memory.get("variance") else 0.0
            dev = dev / sigma if sigma > 0 else 0.0
        if hold > 0:
            block = int(timestamp // (hold * 1e9))
            if memory.get("inv_block") != block or memory.get("fund_dev") is None:
                memory["inv_block"], memory["fund_dev"] = block, dev
        else:
            memory["fund_dev"] = dev
        return memory

    def _inventory_gap(self, account, memory, mid, wealth):
        gap = super()._inventory_gap(account, memory, mid, wealth)
        target = self.inv_target.get(memory.get("class_index"))
        if target is None or memory.get("fund_dev") is None:
            return gap
        k, most = target[0], target[1]
        return gap - max(-most, min(most, k * memory["fund_dev"]))


if __name__ == "__main__":
    launch(MyAgent10_26_5Gpt)
