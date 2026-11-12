# SPDX-License-Identifier: MIT
"""
MyAgent10_26_5Gpt with an optional reseed snipe: when a reseed moves a book's published fundamental by at least
`snipe_min_bps`, the book sends one immediate-or-cancel take at the touch in the move's direction, and the ordinary
quoting (inventory skew, bands) unwinds the position afterwards.
Why (28 October, agents/data/replays/shield_grid_log.md #155-#156): the top of the SN79 incentive table is held by pure
takers (coldkey 5CfQTT, 9 uids in the top 50) who take only in the first 5 s of a cycle whose fundamental moved >= 10 bps,
always in its direction; on the recording, a take at the touch on those cycles marks out (fee-free, 20 s) +6.8 / +8.8 /
+7.0 bps on c0 / c1 / c2 in the first round and +4.2 / +3.2 / +5.1 one round later - above the 2.3 bps skill hurdle -
while cycles under 5 bps give ~0. The score ignores fees (de-beta alpha is the inventory MTM path), so the takes feed
the skill half (net alpha) and never the making credit.
How: per book, the fundamental of the previous round is kept; in the first round of a new `reseed_s` cycle whose
fundamental moved by >= `snipe_min_bps_c<k>` (default 10) the book sends a limit order at the opposite touch (best ask
for an up-move, best bid for a down-move), time in force IOC (never rests), for min(`snipe_notional_c<k>` (default 3000
QUOTE) / price, the touch quantity, free funds), skipped near the validator's volume cap (`turnover_margin`). Only
the touch is taken by default (deeper levels are what the faster snipers leave); `snipe_cushion_bps_c<k>` > 0 prices the
take that many bps through the touch instead (still IOC, the quantity then capped by the notional only), for the rounds
in which the touch has moved before the order lands. A class without `snipe_c<k>=1` quotes exactly as 10_26_5Gpt.
Local test (from the agents directory, with the proxy and simulator running):
    python Agent_10_23/MyAgent10_28_1Gpt.py --port 8888 --agent_id 0 --params lazy_load=0 snipe_c0=1 snipe_c2=1
"""
import importlib.util
import math
from pathlib import Path

import bittensor as bt

from taos.common.agents import launch
from taos.im.protocol.models import OrderDirection, STP, TimeInForce

_spec = importlib.util.spec_from_file_location("MyAgent10_26_5Gpt", Path(__file__).with_name("MyAgent10_26_5Gpt.py"))
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)


class MyAgent10_28_1Gpt(_parent.MyAgent10_26_5Gpt):
    """MyAgent10_26_5Gpt with the reseed snipe; see the module docstring."""

    def initialize(self):
        super().initialize()
        self.snipe = {}
        for i in range(16):
            if float(getattr(self.config, f"snipe_c{i}", 0) or 0) > 0:
                self.snipe[i] = (float(getattr(self.config, f"snipe_min_bps_c{i}", 10.0)),
                                 float(getattr(self.config, f"snipe_notional_c{i}", 3000.0)),
                                 float(getattr(self.config, f"snipe_cushion_bps_c{i}", 0.0)))
        bt.logging.info("10_28_1Gpt: reseed snipe "
                        + (", ".join(f"c{i} |dF| >= {m:g} bps, up to {n:g} QUOTE at the touch + {c:g} bps, IOC"
                                     for i, (m, n, c) in sorted(self.snipe.items())) or "off"))

    def _quote_book_2(self, response, validator, book_id, book, memory, account=None):
        result = super()._quote_book_2(response, validator, book_id, book, memory, account)
        spec = self.snipe.get(self._class_index(book_id))
        if spec is not None and memory is not None:
            self._snipe(response, validator, book_id, book, memory, account, spec)
        return result

    def _snipe(self, response, validator, book_id, book, memory, account, spec):
        fundamental = getattr(book, "fundamental", None)
        if not fundamental or fundamental <= 0 or not book.bids or not book.asks:
            return
        cycle = int(memory["timestamp"] / 1e9 // self.reseed_s)
        last_cycle, last_fund = memory.get("sn_cycle"), memory.get("sn_fund")
        memory["sn_cycle"], memory["sn_fund"] = cycle, fundamental
        if last_cycle is None or last_cycle == cycle or not last_fund:
            return                                           # not the first round of a new cycle
        move = (fundamental - last_fund) / last_fund * 1e4
        min_bps, notional, cushion = spec
        if abs(move) < min_bps:
            return
        account = account if account is not None else self._accounts().get(book_id)
        if account is None:
            return
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        if wealth > 0 and (account.traded_volume or 0.0) >= self.turnover_margin * self.turnover_cap * wealth:
            self._count(validator, "snipe_capped")
            return
        buy = move > 0
        level = book.asks[0] if buy else book.bids[0]
        price = level.price
        if not math.isfinite(price) or price <= 0:
            return
        if cushion > 0:
            # a limit through the touch by `cushion` bps: still IOC, fills whatever the book holds up to that price
            tick = 10 ** -self.price_decimals(book_id)
            far = price * (1 + cushion / 1e4) if buy else price * (1 - cushion / 1e4)
            price = (math.ceil(far / tick - 1e-9) if buy else math.floor(far / tick + 1e-9)) * tick
            price = round(price, self.price_decimals(book_id))
        quantity = min(notional / price, level.quantity if cushion <= 0 else notional / price)
        quantity = min(quantity, account.quote_balance.free / price if buy else account.base_balance.free)
        decimals = self.volume_decimals(book_id)
        quantity = math.floor(quantity * 10 ** decimals) / 10 ** decimals
        if quantity < self.min_order_size(book_id):
            self._count(validator, "snipe_too_small")
            return
        response.limit_order(
            book_id=book_id,
            direction=OrderDirection.BUY if buy else OrderDirection.SELL,
            quantity=quantity,
            price=price,
            stp=STP.CANCEL_OLDEST,
            postOnly=False,
            timeInForce=TimeInForce.IOC,
        )
        self._count(validator, "snipe_takes")
        self._count(validator, f"snipe_takes_c{self._class_index(book_id)}")

    def _plan_card(self, validator):
        card = super()._plan_card(validator)
        if not self.snipe:
            return card
        stats = self.plan_stats.get(validator, {})
        return (card + f" | 1GPT28 snipe: {stats.get('snipe_takes', 0)} takes ("
                + ", ".join(f"c{i} {stats.get(f'snipe_takes_c{i}', 0)}" for i in sorted(self.snipe))
                + f"), capped {stats.get('snipe_capped', 0)}, too small {stats.get('snipe_too_small', 0)}")


if __name__ == "__main__":
    launch(MyAgent10_28_1Gpt)
