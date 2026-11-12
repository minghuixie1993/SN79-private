# SPDX-License-Identifier: MIT
"""
MyAgent10_27_2Gpt (the live c2-off agent) with budget-aware spread widening: a book whose traded volume approaches the
validator's rolling cap quotes wider instead of running into the hard stop.
Why (29 October, agents/data/replays/shield_grid_log.md 03:01 check and research run 85): the per-book volume cap
(capital_turnover_cap x miner_wealth = 500k, the agent stops at turnover_margin 0.9) is a ROLLING 24 sim h window that
carries across simulation runs; at the live c0 size the steady-state volume (~570k) exceeds it, so busy c0 books would
alternate between quoting and the hard stop. With volume as the binding resource the credit-maximising answer is a
higher price per unit - fewer fills at more capture and better markout - on exactly the books that will run out
(Sarkissian's spread-turnover multiplier; the shadow price of #148).
How: for a class with `cap_widen_k_c<k>` > 0, each round the book's validator-reported traded_volume v is taken as a share
f = v / (turnover_cap x miner_wealth); above `cap_widen_from` (default 0.6) the half spread is multiplied by
1 + k x (f - from) / (turnover_margin - from), i.e. by 1 + k at the stop. Below `from` the book quotes exactly as
10_27_2Gpt; the hard stop stays underneath. A class without `cap_widen_k_c<k>` is exactly 10_27_2Gpt.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_29_1Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 quote_off_c2=1 cap_widen_k_c0=1.0
"""
import importlib.util
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch

_spec = importlib.util.spec_from_file_location("MyAgent10_27_2Gpt", Path(__file__).with_name("MyAgent10_27_2Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)


class MyAgent10_29_1Gpt(_parent.MyAgent10_27_2Gpt):
    """MyAgent10_27_2Gpt with budget-aware spread widening; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.cap_widen = {i: float(getattr(self.config, f"cap_widen_k_c{i}", 0) or 0) for i in range(16)}
        self.cap_widen = {i: k for i, k in self.cap_widen.items() if k > 0}
        self.cap_widen_from = float(getattr(self.config, "cap_widen_from", 0.6))
        bt.logging.info("10_29_1Gpt: cap widening "
                        + (", ".join(f"c{i} x(1 + {k:g} x ramp from {self.cap_widen_from:g} of cap)"
                                     for i, k in sorted(self.cap_widen.items())) or "off"))

    def _params(self, book_id):
        params = super()._params(book_id)
        k = self.cap_widen.get(self._class_index(book_id))
        if not k:
            return params
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        account = self._accounts().get(book_id) or self._accounts().get(int(book_id))
        if wealth <= 0 or account is None:
            return params
        frac = (account.traded_volume or 0.0) / (self.turnover_cap * wealth)
        top = max(self.cap_widen_from + 1e-6, self.turnover_margin)
        if frac <= self.cap_widen_from:
            return params
        ramp = min(1.0, (frac - self.cap_widen_from) / (top - self.cap_widen_from))
        return dict(params, half_spread=params["half_spread"] * (1.0 + k * ramp))


if __name__ == "__main__":
    launch(MyAgent10_29_1Gpt)
