# SPDX-License-Identifier: MIT
"""
MyAgent10_26_3Gpt with an optional de-meaned fundamental term in the predictor, per class.
Why (26 October, agents/data/replays/shield_grid_log.md free-time #72): the validator's skill alpha strips each book's
own drift (debeta.book_alphas_from_drift: MTM - mean inventory x book drift), so a predictor earns there only through
the WITHIN-book variation of its signal. On UID 229's recording (scratchpad d660_fundic_fe.py, within-book IC against
the mid's log return) the published fundamental gap (fundamental - mid, bps) predicts better once its own slow level
is removed: gap - EWMA(gap, 600 s) has IC +0.23 / +0.26 on class 0 (20 s / 300 s) against +0.15 / +0.11 raw, and
+0.34 / +0.13 on class 1 against +0.15 / -0.04 raw (class 1's gap sits ~+60 bps above the mid on average). Class 2 is
the other way round (raw +0.10 / +0.21, de-meaned +0.02 / +0.03): its mid closes a ~+275 bps gap slowly, so it keeps
the raw term. Within-book 20 s slopes: class 0 raw 0.032 (V3's coef_fund 0.04) vs de-meaned 0.074; class 1 raw 0.074
(class default 0.0035) vs de-meaned 0.36.
How: for a class k with `fund_demean_s_c<k>` > 0, the predictor's `coef_fund` x gap term is replaced by
`coef_fund_dm_c<k>` x (gap - its EWMA over that many sim seconds); every other input is unchanged. The EWMA is kept per
book in its memory and starts at the first gap seen. A class without `fund_demean_s_c<k>` predicts exactly as 10_26_3Gpt.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_26_4Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 fund_demean_s_c0=600 coef_fund_dm_c0=0.07
"""
import importlib.util
import math
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch

_spec = importlib.util.spec_from_file_location("MyAgent10_26_3Gpt", Path(__file__).with_name("MyAgent10_26_3Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)


class MyAgent10_26_4Gpt(_parent.MyAgent10_26_3Gpt):
    """MyAgent10_26_3Gpt with a de-meaned fundamental term per class; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.fund_demean_s, self.coef_fund_dm = {}, {}
        for index in range(16):
            tau = float(getattr(self.config, f"fund_demean_s_c{index}", 0) or 0)
            if tau > 0:
                self.fund_demean_s[index] = tau
                self.coef_fund_dm[index] = float(getattr(self.config, f"coef_fund_dm_c{index}", 0.0))
        bt.logging.info("10_26_4Gpt: de-meaned fundamental "
                        + (", ".join(f"c{k} tau {self.fund_demean_s[k]:g} s coef {self.coef_fund_dm[k]:g}"
                                     for k in sorted(self.fund_demean_s)) or "off"))

    def _remember(self, validator, book_id, book, timestamp):
        memory = super()._remember(validator, book_id, book, timestamp)
        if memory is not None:
            # the predictor sees the book and a copy of its class parameters, not the book id, so the class rides along
            memory["class_index"] = self._class_index(book_id)
        return memory

    def _predict(self, book, account, memory, params):
        index = memory.get("class_index")
        tau = self.fund_demean_s.get(index) if index is not None else None
        fundamental = getattr(book, "fundamental", None)
        mid = memory["mid"]
        if not tau or not fundamental or fundamental <= 0 or mid <= 0:
            return super()._predict(book, account, memory, params)
        gap = (fundamental - mid) / mid * 1e4
        timestamp, ewma, last = memory["timestamp"], memory.get("gap_ewma"), memory.get("gap_ts")
        if ewma is None or last is None or timestamp < last:
            ewma = gap
        else:
            ewma += (1 - math.exp(-(timestamp - last) / 1e9 / tau)) * (gap - ewma)
        memory["gap_ewma"], memory["gap_ts"] = ewma, timestamp
        pred = super()._predict(book, account, memory, dict(params, coef_fund=0.0))
        pred = max(-self.max_pred_bps, min(self.max_pred_bps, pred + self.coef_fund_dm[index] * (gap - ewma)))
        if not math.isfinite(pred):
            pred = 0.0
        memory["pred"] = pred
        return pred


if __name__ == "__main__":
    launch(MyAgent10_26_4Gpt)
