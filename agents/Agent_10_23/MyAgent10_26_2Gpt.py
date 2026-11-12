# SPDX-License-Identifier: MIT
"""
MyAgent10_23_9Gpt with each book's cancels sent as ONE instruction ahead of its new orders.
Why (26 October, agents/data/replays/shield_grid_log.md code review #70): the validator's set_delays (reward.py
2268-2311) gives the first instruction per book no extra delay and every later one an independent random 5-25 ms. 9Gpt
sends a book's plan as [cancel buy, buy, cancel sell, sell], so the new sell and the cancel of the sell it replaces
arrive in random order: about half the time the new sell is processed first. Where the free BASE is below the new
sell (the old sell still reserves it) the engine refuses it with INSUFFICIENT_BASE, the old sell is cancelled a few ms
later, and the book has no ask until the next round. Live UID 229, 26 Oct 06:02-10:35 CET: 171 such refusals, all on
three c0 books with BASE sold down (book 4: 85, free 2.58 of 11.29 BASE, 8.70 reserved by the resting sell).
How: after 9Gpt has built a book's instructions, its cancels are merged into one cancel instruction placed first, so it
takes the book's zero-delay slot and every new order lands after all of that book's cancels.
With `cancels_first=0` it places exactly 9Gpt's orders.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_26_2Gpt.py --port 8888 --agent_id 0 --params lazy_load=0
"""
import importlib.util
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch

_spec = importlib.util.spec_from_file_location("MyAgent10_23_9Gpt", Path(__file__).with_name("MyAgent10_23_9Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)


class MyAgent10_26_2Gpt(_parent.MyAgent10_23_9Gpt):
    """MyAgent10_23_9Gpt with a book's cancels merged and sent first; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.cancels_first = bool(float(getattr(self.config, "cancels_first", 1)))
        bt.logging.info(f"10_26_2Gpt: cancels first {self.cancels_first}")

    def _execute(self, response, book_id, plans, validator):
        start = len(response.instructions)
        super()._execute(response, book_id, plans, validator)
        if not self.cancels_first:
            return
        added = response.instructions[start:]
        cancels = [i for i in added if hasattr(i, "cancellations")]
        if not cancels or (len(cancels) == 1 and added[0] is cancels[0]):
            return
        first = cancels[0]
        merged = type(first)(agentId=first.agentId, delay=first.delay, bookId=first.bookId,
                             cancellations=[c for i in cancels for c in i.cancellations])
        response.instructions[start:] = [merged] + [i for i in added if not hasattr(i, "cancellations")]


if __name__ == "__main__":
    launch(MyAgent10_26_2Gpt)
