# SPDX-License-Identifier: MIT
"""
MyAgent10_23_9Gpt with a markout target per asset class.
Why (26 October, agents/data/replays/shield_grid_log.md code reviews #57 and #58): 9Gpt's class and book spread
controllers both steer toward one global `markout_target_bps` (3 bps, set on the local market), while the mainnet
classes mark out very differently - c0 ~+1.2, c1 ~+1.8-2.2, c2 ~+0.7 bps. With one target the book controller widens
almost every c0 / c2 book toward its x1.25 cap (live card: 111 books widened), and any single lower target mis-steers at
least one class. Here `markout_target_bps_c<k>` overrides the target for class k; a class without one keeps the global
`markout_target_bps`.
How: each round's pending fills are split by class and 9Gpt's whole markout settlement runs once per class with that
class's target in place, so the class multiplier, the book multipliers and every statistic the chain keeps see the same
fills as before, only with their own class's target. Fills not yet due are kept as 9Gpt keeps them.
Without any `markout_target_bps_c<k>` it places exactly 9Gpt's orders.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_26_1Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 markout_target_bps_c2=0.8
"""
import importlib.util
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch

_spec = importlib.util.spec_from_file_location("MyAgent10_23_9Gpt", Path(__file__).with_name("MyAgent10_23_9Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)


class MyAgent10_26_1Gpt(_parent.MyAgent10_23_9Gpt):
    """MyAgent10_23_9Gpt with `markout_target_bps_c<k>` per class; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.class_targets = {}
        for index in range(16):
            given = getattr(self.config, f"markout_target_bps_c{index}", None)
            if given is not None:
                self.class_targets[index] = float(given)
        bt.logging.info(f"10_26_1Gpt: markout target {self.markout_target_bps:g} bps, per class "
                        + (", ".join(f"c{k} {v:g}" for k, v in sorted(self.class_targets.items())) or "none"))

    def _settle_markouts(self, validator, timestamp):
        """9Gpt's settlement, run once per class with that class's markout target."""
        pending = self.pending_markouts.get(validator)
        if not self.class_targets or not pending:
            return super()._settle_markouts(validator, timestamp)
        groups = {}
        for fill in pending:
            groups.setdefault(self._class_index(fill[1]), []).append(fill)
        base = self.markout_target_bps
        kept, todo = [], list(groups.items())
        try:
            while todo:
                index, fills = todo[0]
                self.pending_markouts[validator] = fills
                self.markout_target_bps = self.class_targets.get(index, base)
                super()._settle_markouts(validator, timestamp)
                kept.extend(self.pending_markouts.get(validator) or [])
                todo.pop(0)
        finally:
            # if a class's settlement raised, its fills and the classes not reached yet stay pending
            self.markout_target_bps = base
            self.pending_markouts[validator] = kept + [f for _, fills in todo for f in fills]


if __name__ == "__main__":
    launch(MyAgent10_26_1Gpt)
