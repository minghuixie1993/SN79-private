# SPDX-License-Identifier: MIT
"""
MyAgent10_29_3Gpt with MyAgent10_28_1Gpt's optional reseed snipe (`snipe_c<k>=1`, `snipe_min_bps_c<k>`,
`snipe_notional_c<k>`, `snipe_cushion_bps_c<k>`; see MyAgent10_28_1Gpt.py for the rule).
Why (30 October 03:3x, agents/data/replays/shield_grid_log.md): under 0.6.6's mid mark (debeta_skill_mark_alt, already
published by the validators) UID 229's skill is -0.010 and its net alpha -48 (field median +433), so once 0.6.6 lands the
skill half pays it nothing; the snipe's IOC takes on reseed moves >= 10 bps marked out +6.8 / +8.8 / +7.0 bps (c0 / c1 / c2)
on the recording, and 0.6.6 no longer books takes as taker legs in the making credit. With no `snipe_c<k>` the agent is
exactly MyAgent10_29_3Gpt (live set, gap-EWMA persistence included).
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_30_1Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 snipe_c0=1 snipe_c1=1 snipe_c2=1
"""
import importlib.util
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_parent = _load("MyAgent10_29_3Gpt")
_sniper = _load("MyAgent10_28_1Gpt").MyAgent10_28_1Gpt


class MyAgent10_30_1Gpt(_parent.MyAgent10_29_3Gpt):
    """MyAgent10_29_3Gpt with the reseed snipe; see the module docstring."""

    _snipe = _sniper._snipe          # no super() inside: safe to share

    def initialize(self):
        super().initialize()
        self.snipe = {}
        for i in range(16):
            if float(getattr(self.config, f"snipe_c{i}", 0) or 0) > 0:
                self.snipe[i] = (float(getattr(self.config, f"snipe_min_bps_c{i}", 10.0)),
                                 float(getattr(self.config, f"snipe_notional_c{i}", 3000.0)),
                                 float(getattr(self.config, f"snipe_cushion_bps_c{i}", 0.0)))
        bt.logging.info("10_30_1Gpt: reseed snipe "
                        + (", ".join(f"c{i} |dF| >= {m:g} bps, up to {n:g} QUOTE at the touch + {c:g} bps, IOC"
                                     for i, (m, n, c) in sorted(self.snipe.items())) or "off"))

    def _quote_book_2(self, response, validator, book_id, book, memory, account=None):
        result = super()._quote_book_2(response, validator, book_id, book, memory, account)
        spec = self.snipe.get(self._class_index(book_id)) if self.snipe else None
        if spec is not None and memory is not None and int(book_id) not in self.books_off:
            self._snipe(response, validator, book_id, book, memory, account, spec)
        return result

    def _plan_card(self, validator):
        card = super()._plan_card(validator)
        if not self.snipe:
            return card
        stats = self.plan_stats.get(validator, {})
        return (card + f" | 1GPT30 snipe: {stats.get('snipe_takes', 0)} takes ("
                + ", ".join(f"c{i} {stats.get(f'snipe_takes_c{i}', 0)}" for i in sorted(self.snipe))
                + f"), capped {stats.get('snipe_capped', 0)}, too small {stats.get('snipe_too_small', 0)}")


if __name__ == "__main__":
    launch(MyAgent10_30_1Gpt)
