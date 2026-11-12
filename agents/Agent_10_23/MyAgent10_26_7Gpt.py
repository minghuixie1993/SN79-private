# SPDX-License-Identifier: MIT
"""
MyAgent10_26_6Gpt with a reseed lean: right after the published fundamental changes, the predicted move leans toward it.
Why (26 October, agents/data/replays/shield_grid_log.md 18:55, the user's "snipe at the reseed"): the fundamental jumps
~4.5 bps at each 30 s reseed and the mid has moved ~0 by the first round that shows it, then moves on by ~+1.2 bps on
class 0 over the next ~10 sim s (+1.7 c1, +0.4 c2) in the jump's direction. Taking that move does not pay in the score
(a take books negative capture, de-beta skill ignores fees), but a maker can collect it: lean the fair price toward the
new fundamental so the bid (after an up jump) sits closer and the ask further, i.e. fill on the side the price moves to
and not on the side it runs over. The 20 s predictor already carries coef_fund x gap, but the gap is the LEVEL; the
jump is the news.
How: for a class with `reseed_lean_k_c<k>` > 0, each book remembers its last published fundamental; when it changes, the
jump (bps) and its time are kept, and for `reseed_lean_s_c<k>` sim seconds (default 10) the prediction gets
k x jump x (1 - elapsed / duration) on top, inside max_pred_bps; the lean cap (max_lean) still bounds the quotes.
A class without `reseed_lean_k_c<k>` predicts exactly as 10_26_6Gpt.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_26_7Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 reseed_lean_k_c0=0.3
"""
import importlib.util
import math
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch

_spec = importlib.util.spec_from_file_location("MyAgent10_26_6Gpt", Path(__file__).with_name("MyAgent10_26_6Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)


class MyAgent10_26_7Gpt(_parent.MyAgent10_26_6Gpt):
    """MyAgent10_26_6Gpt with a reseed lean per class; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.reseed_lean = {}
        for index in range(16):
            k = float(getattr(self.config, f"reseed_lean_k_c{index}", 0) or 0)
            if k:
                self.reseed_lean[index] = (k, float(getattr(self.config, f"reseed_lean_s_c{index}", 10.0)))
        bt.logging.info("10_26_7Gpt: reseed lean "
                        + (", ".join(f"c{i} {k:g} x jump for {s:g} s" for i, (k, s) in sorted(self.reseed_lean.items()))
                           or "off"))

    def _remember(self, validator, book_id, book, timestamp):
        memory = super()._remember(validator, book_id, book, timestamp)
        if memory is None or memory.get("class_index") not in self.reseed_lean:
            return memory
        fundamental = getattr(book, "fundamental", None)
        if not fundamental or fundamental <= 0:
            return memory
        last = memory.get("rl_fund")
        if last and fundamental != last:
            memory["rl_jump"], memory["rl_t"] = (fundamental - last) / last * 1e4, timestamp
        memory["rl_fund"] = fundamental
        return memory

    def _predict(self, book, account, memory, params):
        pred = super()._predict(book, account, memory, params)
        lean = self.reseed_lean.get(memory.get("class_index"))
        if lean is None or memory.get("rl_t") is None:
            return pred
        k, duration = lean
        elapsed = (memory["timestamp"] - memory["rl_t"]) / 1e9
        if 0 <= elapsed < duration:
            pred = max(-self.max_pred_bps, min(self.max_pred_bps, pred + k * memory["rl_jump"] * (1 - elapsed / duration)))
            if not math.isfinite(pred):
                pred = 0.0
            memory["pred"] = pred
        return pred


if __name__ == "__main__":
    launch(MyAgent10_26_7Gpt)
