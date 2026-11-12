# SPDX-License-Identifier: MIT
"""
MyAgent10_26_5Gpt with a built-in switchback experiment: one class parameter alternates between two values in sim-time
blocks whose order is a seeded coin flip, so a live A / B comparison needs no restart and no before / after reading.
Why (27 October, agents/data/replays/shield_grid_log.md #128-#130 and research run 67): the c0 3.5 bps step was read as a
single before / after change on one uid, confounded by time; marketplaces with one unit run switchbacks - alternate the
treatment over time, drop a washout after each switch, analyse per block (period-level bootstrap), here also against the
field's markout in the same block (difference-in-differences).
How: `sb_key_c<k>` names a class parameter (e.g. half_spread), `sb_a_c<k>` / `sb_b_c<k>` its two values, `sb_block_s`
the block length in sim seconds (default 300) and `sb_seed` the seed. Block i = floor(sim seconds / sb_block_s) uses arm B
when sha256(f"{seed}:{i}")[0] is odd, else A - the same function in analysis rebuilds every block's arm from the timestamp,
so nothing extra has to be recorded. A class without `sb_key_c<k>` is exactly 10_26_5Gpt.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_27_4Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 sb_key_c0=half_spread sb_a_c0=0.0004 sb_b_c0=0.00035
"""
import contextvars
import importlib.util
import hashlib
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch

_spec = importlib.util.spec_from_file_location("MyAgent10_26_5Gpt", Path(__file__).with_name("MyAgent10_26_5Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)

_NOW = contextvars.ContextVar("my_agent_10_27_4_now", default=None)


def switchback_arm(seed, block):
    """'B' when the first byte of sha256(f"{seed}:{block}") is odd, else 'A' - shared by the agent and the analysis.

    Not crc32 (28 Oct, shield_grid_log #170): crc32 is linear over GF(2), so its low bit is a fixed XOR of the input's bits -
    seed 28's crc32 arms came in patterned runs (mean run 3.45 vs ~2 for a coin, autocorrelation -0.79 at lag 10 and -0.88
    at lag 100) and every other seed gave the same split or its complement, confounding the arms with periodic market
    patterns and leaving no valid randomization test. sha256's parity: mean run 2.06, |autocorrelation| <= 0.08."""
    return "B" if hashlib.sha256(f"{seed}:{block}".encode()).digest()[0] & 1 else "A"


class MyAgent10_27_4Gpt(_parent.MyAgent10_26_5Gpt):
    """MyAgent10_26_5Gpt with a per-class switchback; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.sb_block_s = max(1.0, float(getattr(self.config, "sb_block_s", 300.0)))
        self.sb_seed = int(float(getattr(self.config, "sb_seed", 1)))
        self.sb = {}
        for i in range(16):
            key = getattr(self.config, f"sb_key_c{i}", None)
            if key:
                self.sb[i] = (str(key), float(getattr(self.config, f"sb_a_c{i}")), float(getattr(self.config, f"sb_b_c{i}")))
        bt.logging.info("10_27_4Gpt: switchback "
                        + (", ".join(f"c{i} {k} A {a:g} / B {b:g}" for i, (k, a, b) in sorted(self.sb.items()))
                           + f", blocks {self.sb_block_s:g} s, seed {self.sb_seed}" if self.sb else "off"))

    def _arm(self, timestamp):
        return switchback_arm(self.sb_seed, int(timestamp / 1e9 // self.sb_block_s))

    def _quote_book_2(self, response, validator, book_id, book, memory, account=None):
        if not self.sb:
            return super()._quote_book_2(response, validator, book_id, book, memory, account)
        token = _NOW.set(memory["timestamp"])
        try:
            if self._class_index(book_id) in self.sb:
                self._count(validator, f"sb_rounds_{self._arm(memory['timestamp'])}")
            return super()._quote_book_2(response, validator, book_id, book, memory, account)
        finally:
            _NOW.reset(token)

    def _params(self, book_id):
        params = super()._params(book_id)
        spec = self.sb.get(self._class_index(book_id))
        now = _NOW.get()
        if spec is None or now is None:
            return params
        key, a, b = spec
        return dict(params, **{key: b if self._arm(now) == "B" else a})

    def _plan_card(self, validator):
        card = super()._plan_card(validator)
        if not self.sb:
            return card
        stats = self.plan_stats.get(validator, {})
        return (card + f" | 4GPT27 switchback {self.sb_block_s:g} s blocks, seed {self.sb_seed}: book-rounds A "
                f"{stats.get('sb_rounds_A', 0)}, B {stats.get('sb_rounds_B', 0)}")


if __name__ == "__main__":
    launch(MyAgent10_27_4Gpt)
