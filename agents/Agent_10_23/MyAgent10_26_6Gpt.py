# SPDX-License-Identifier: MIT
"""
MyAgent10_26_5Gpt with per-book budget pacing of the quote size (the validator's daily volume cap).
Why (26 October, agents/data/replays/shield_grid_log.md free-time #75 / research run 41): the validator drops a book's
orders once it has traded 10 x wealth in a rolling 24 sim h; the agent stops at 0.9 of it (450k per book, a sustainable
18.75k per sim h). At class 0 size 0.024 the median c0 book trades ~29k per sim h and the busiest ~87k, so a few books
run out early and sit dark while the 0.012 step-down planned for after immunity halves every book, the quiet ones too.
How (Balseiro & Gur 2019 adaptive pacing, as restated by Gaitonde et al., arXiv 2205.08674 Algorithm 1): each book of a
class with `budget_pace_c<k>=1` keeps a multiplier mu >= 0; its quote size is size_fraction / (1 + mu). At every pacing
period boundary (`pace_period_s`, 300 sim s) mu <- clip(mu - eta x (rho - z) / rho, 0, `pace_mu_max`), z being the
book's own traded notional in the period just ended (9Gpt's per-period volume) and rho the period's even share of
`turnover_margin` x the cap. A book trading under its share keeps the full size (mu -> 0); a busy one shrinks until it
trades at about rho. The 0.9 hard stop stays underneath. A class without `budget_pace_c<k>` sizes exactly as 10_26_5Gpt.
`pace_mode=1` (free-time #97, 26 Oct): rho = the book's REMAINING budget (turnover_margin x cap - the validator's own
traded_volume) over the periods left in the simulation run (simulation_config.duration) - a run is 24 sim h, the same
length as the budget window, so a book that will not run out before the run ends keeps mu = 0 and only the ~15 busiest
c0 books are slowed (the even-share mode slowed every book that traded above 1/288 of the day and cost making).
`pace_mode=2` (free-time #165, 28 Oct; the cumulative throttle proposed in #102 after mode 1 over-reacted to lumpy fills):
no recursion - at each pacing period boundary the size factor is min(1, room / (rate x time left)), room = turnover_margin x
cap - the validator's own traded_volume, rate = traded_volume / sim time elapsed in the run (the run starts at sim t 0),
after a warm-up of `pace_warmup_periods` (12 = 1 sim h); the factor never goes below `pace_min_factor` (0.25). A book on
course to finish under its budget keeps the full size; a busy one shrinks just enough to last to the run's end.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_26_6Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 budget_pace_c0=1
"""
import importlib.util
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch

_spec = importlib.util.spec_from_file_location("MyAgent10_26_5Gpt", Path(__file__).with_name("MyAgent10_26_5Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)


class MyAgent10_26_6Gpt(_parent.MyAgent10_26_5Gpt):
    """MyAgent10_26_5Gpt with per-book budget pacing; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.pace_classes = {i for i in range(16) if float(getattr(self.config, f"budget_pace_c{i}", 0) or 0) > 0}
        self.pace_eta     = float(getattr(self.config, "pace_eta", 1.0))
        self.pace_mu_max  = float(getattr(self.config, "pace_mu_max", 3.0))
        self.pace_mu      = {}          # book id -> mu, the book's size divisor is 1 + mu
        self.pace_mode    = int(float(getattr(self.config, "pace_mode", 0)))
        self.pace_warmup  = int(float(getattr(self.config, "pace_warmup_periods", 12)))
        self.pace_min_factor = max(0.01, float(getattr(self.config, "pace_min_factor", 0.25)))
        bt.logging.info("10_26_6Gpt: budget pacing on "
                        + (", ".join(f"c{i}" for i in sorted(self.pace_classes)) or "no class")
                        + f" (eta {self.pace_eta:g}, mu max {self.pace_mu_max:g}, mode "
                        + {1: "run end", 2: "cumulative throttle"}.get(self.pace_mode, "even day") + ")")

    def _remember(self, validator, book_id, book, timestamp):
        memory = super()._remember(validator, book_id, book, timestamp)
        if memory is None or memory.get("class_index") not in self.pace_classes:
            return memory
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        if wealth <= 0:
            return memory
        period = int(self.pace_period_s * 1e9)
        index = memory["timestamp"] // period
        last = memory.get("pace_idx")
        if last is None or index < last:
            memory["pace_idx"] = index
            return memory
        if index > last and self.pace_mode == 2:
            account = self._accounts().get(book_id) or self._accounts().get(int(book_id))
            duration = getattr(getattr(self, "simulation_config", None), "duration", None)
            factor = 1.0
            if account is not None and duration and index >= self.pace_warmup:
                elapsed = max(1.0, memory["timestamp"] / 1e9)
                left = max(1.0, (duration - memory["timestamp"]) / 1e9)
                used = account.traded_volume or 0.0
                room = self.turnover_margin * self.turnover_cap * wealth - used
                rate = used / elapsed
                if rate > 0:
                    factor = max(self.pace_min_factor, min(1.0, max(room, 0.0) / (rate * left)))
            self.pace_mu[book_id] = 1.0 / factor - 1.0
            memory["pace_idx"] = index
            return memory
        if index > last:
            rho = self.turnover_margin * self.turnover_cap * wealth / (86_400 / self.pace_period_s)
            if self.pace_mode == 1:
                account = self._accounts().get(book_id) or self._accounts().get(int(book_id))
                duration = getattr(getattr(self, "simulation_config", None), "duration", None)
                if account is not None and duration:
                    left = max(1.0, (duration - memory["timestamp"]) / period)
                    room = self.turnover_margin * self.turnover_cap * wealth - (account.traded_volume or 0.0)
                    rho = max(room, 1e-9) / left
            mu = self.pace_mu.get(book_id, 0.0)
            volume = memory.get("pvolume") or {}
            # every period that ended since the last update, the quiet ones (z = 0) included, one step each
            for i in range(last, index):
                z = volume.get(i, 0.0)
                mu = max(0.0, min(self.pace_mu_max, mu - self.pace_eta * (rho - z) / rho))
            self.pace_mu[book_id] = mu
            memory["pace_idx"] = index
        return memory

    def _params(self, book_id):
        params = super()._params(book_id)
        mu = self.pace_mu.get(book_id)
        if mu and params.get("size_fraction", 0) > 0:
            params = dict(params, size_fraction=params["size_fraction"] / (1.0 + mu))
        return params

    def _plan_card(self, validator):
        card = super()._plan_card(validator)
        if not self.pace_classes:
            return card
        mus = sorted(self.pace_mu.values())
        if not mus:
            return card + " | 6GPT26 budget pacing: no book paced yet"
        return (card + f" | 6GPT26 budget pacing: {len(mus)} books, size / (1 + mu): mu median {mus[len(mus) // 2]:.2f}, "
                f"max {mus[-1]:.2f}, at 0: {sum(1 for m in mus if m == 0)}")


if __name__ == "__main__":
    launch(MyAgent10_26_6Gpt)
