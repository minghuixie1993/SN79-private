# SPDX-License-Identifier: MIT
"""
MyAgent10_26_5Gpt with an off switch per asset class: `quote_off_c<k>=1` stops quoting every book of class k.
Why (27 October, agents/data/replays/shield_grid_log.md free-time #116): the validator's making credit is multiplied by
ONE quality ratio per uid, clamp(R / C, 0, 1) summed over every book of every class (debeta.markout_quality), so a class
whose realized spread is negative lowers the credit of all the others. Live since the 06:16 switch class 2 realized
-0.57 bps on 150k of notional against captured +1.82: the uid ratio was 0.403 with it and would be ~0.475 without it,
worth more than class 2's own making (~9% of the captured base). size_fraction 0 is no off switch (9Gpt sizes with the default quantity when it is 0), hence this.
How: for a class with `quote_off_c<k>` > 0, each round cancels that book's resting orders and quotes nothing (counted
as "class_off" on the plan card); its inventory is left as it is. Every other class quotes exactly as 10_26_5Gpt, and
with no `quote_off_c<k>` the agent is 10_26_5Gpt.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_27_2Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 quote_off_c2=1
"""
import importlib.util
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch

_spec = importlib.util.spec_from_file_location("MyAgent10_26_5Gpt", Path(__file__).with_name("MyAgent10_26_5Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)


class MyAgent10_27_2Gpt(_parent.MyAgent10_26_5Gpt):
    """MyAgent10_26_5Gpt with `quote_off_c<k>`; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.classes_off = {i for i in range(16) if float(getattr(self.config, f"quote_off_c{i}", 0) or 0) > 0}
        bt.logging.info("10_27_2Gpt: classes not quoted: "
                        + (", ".join(f"c{i}" for i in sorted(self.classes_off)) or "none"))

    def _quote_book_2(self, response, validator, book_id, book, memory, account=None):
        if self._class_index(book_id) not in self.classes_off:
            return super()._quote_book_2(response, validator, book_id, book, memory, account)
        account = account if account is not None else self._accounts()[book_id]
        self._cancel(response, book_id, list(account.orders))
        self._count(validator, "class_off")


if __name__ == "__main__":
    launch(MyAgent10_27_2Gpt)
