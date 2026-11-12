# SPDX-License-Identifier: MIT
"""
MyAgent10_26_5Gpt with a per-book loss limit aimed at the skill leg's kappa: a book whose drift-stripped PnL over the
kappa window sits in the left tail of the uid's books quotes smaller until it recovers.
Why (27 October, agents/data/replays/research_log.md run 69): the validator's skill is kappa_of_alpha over the uid's
per-book alphas (debeta.py:745-760) - each alpha divided by the MAD around the median, then mean / LPM3^(1/3). With the
scorer's own function, halving the 3 worst of 60 books' losses raised kappa +51% while the same alpha added to the 3 best
books raised it +7%: the left tail is where skill is won or lost.
How: per book, every `loss_step_s` (30) sim seconds the agent keeps (base held, mid); over the last `loss_window_s`
(10800, the kappa window) its alpha estimate is sum (q_i - mean q) x (mid_i+1 - mid_i) in QUOTE (the per-book form of
debeta.book_alphas_by_book on our own samples). Across the validator's books of the enabled classes, a book whose alpha is
below median - `loss_k` (2) x MAD (and has at least `loss_min_samples` (20) samples) quotes with size_fraction x
`loss_size` (0.5) on both sides until it is back above the line. Both sides keep quoting (one-sided quoting fails the
2 x min(buy, sell) making credit, shield_grid_log #123). A class without `loss_limit_c<k>=1` is exactly 10_26_5Gpt.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_27_5Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 loss_limit_c0=1 loss_limit_c2=1
"""
import collections
import contextvars
import importlib.util
import statistics
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch

_spec = importlib.util.spec_from_file_location("MyAgent10_26_5Gpt", Path(__file__).with_name("MyAgent10_26_5Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)

_TAIL = contextvars.ContextVar("my_agent_10_27_5_tail", default=False)


class MyAgent10_27_5Gpt(_parent.MyAgent10_26_5Gpt):
    """MyAgent10_26_5Gpt with the per-book loss limit; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.loss_classes = {i for i in range(16) if float(getattr(self.config, f"loss_limit_c{i}", 0) or 0) > 0}
        self.loss_step_s = max(1.0, float(getattr(self.config, "loss_step_s", 30.0)))
        self.loss_window_s = max(self.loss_step_s, float(getattr(self.config, "loss_window_s", 10_800.0)))
        self.loss_k = float(getattr(self.config, "loss_k", 2.0))
        self.loss_size = float(getattr(self.config, "loss_size", 0.5))
        self.loss_min_samples = int(float(getattr(self.config, "loss_min_samples", 20)))
        self._loss_alpha = collections.defaultdict(dict)     # validator -> book -> latest alpha estimate
        self._loss_line = {}                                 # validator -> (line, computed at sim s)
        bt.logging.info("10_27_5Gpt: loss limit "
                        + (", ".join(f"c{i}" for i in sorted(self.loss_classes))
                           + f": alpha < median - {self.loss_k:g} x MAD over {self.loss_window_s:g} s -> size x{self.loss_size:g}"
                           if self.loss_classes else "off"))

    def _loss_update(self, validator, book_id, memory, account):
        """Samples (base held, mid) every loss_step_s and returns the book's windowed alpha estimate, or None."""
        t = memory["timestamp"] / 1e9
        mid = memory.get("mid")
        if not mid or account is None:
            return None
        samples = memory.setdefault("loss_samples", collections.deque())
        if not samples or t - samples[-1][0] >= self.loss_step_s:
            samples.append((t, account.base_balance.total, mid))
            while samples and t - samples[0][0] > self.loss_window_s:
                samples.popleft()
        if len(samples) < self.loss_min_samples:
            return None
        qs = [s[1] for s in samples]
        qbar = sum(qs) / len(qs)
        alpha = sum((samples[i][1] - qbar) * (samples[i + 1][2] - samples[i][2]) for i in range(len(samples) - 1))
        self._loss_alpha[validator][book_id] = alpha
        return alpha

    def _loss_threshold(self, validator, t):
        line = self._loss_line.get(validator)
        if line is not None and t - line[1] < self.loss_step_s:
            return line[0]
        values = list(self._loss_alpha[validator].values())
        if len(values) < 8:
            return None
        med = statistics.median(values)
        mad = statistics.median([abs(v - med) for v in values]) or 1e-12
        value = med - self.loss_k * mad
        self._loss_line[validator] = (value, t)
        return value

    def _quote_book_2(self, response, validator, book_id, book, memory, account=None):
        if self._class_index(book_id) not in self.loss_classes:
            return super()._quote_book_2(response, validator, book_id, book, memory, account)
        acct = account if account is not None else self._accounts().get(book_id)
        alpha = self._loss_update(validator, book_id, memory, acct)
        line = self._loss_threshold(validator, memory["timestamp"] / 1e9)
        tail = alpha is not None and line is not None and alpha < line
        if tail:
            self._count(validator, "loss_limited")
        token = _TAIL.set(tail)
        try:
            return super()._quote_book_2(response, validator, book_id, book, memory, account)
        finally:
            _TAIL.reset(token)

    def _params(self, book_id):
        params = super()._params(book_id)
        if _TAIL.get() and params.get("size_fraction", 0) > 0:
            return dict(params, size_fraction=params["size_fraction"] * self.loss_size)
        return params

    def _plan_card(self, validator):
        card = super()._plan_card(validator)
        if not self.loss_classes:
            return card
        stats = self.plan_stats.get(validator, {})
        return card + f" | 5GPT27 loss limit: {stats.get('loss_limited', 0)} book-rounds sized x{self.loss_size:g}"


if __name__ == "__main__":
    launch(MyAgent10_27_5Gpt)
