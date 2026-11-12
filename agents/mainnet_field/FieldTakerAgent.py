# SPDX-License-Identifier: MIT
"""
FieldTakerAgent: a stand-in for the other miners that TAKE liquidity on mainnet, so a local run can put our makers
in front of the flow that picks them off there.

Calibrated from the real mainnet trades of 23 October (agents/data/real_market/mainnet_20261019_1025, 182k trades
over the last 3 sim hours, 20 s markouts against the published mid):

    class 0 (books 0-95)    miner takers are informed: their takes mark out +3.2 bps against background makers
                            and +1.1 bps against miner makers 20 s later; they are 13.6% of taker notional.
    class 1 (books 96-127)  miner takers are 63.7% of taker notional but uninformed: -1.1 bps against miner makers,
                            -3.6 bps against background makers (they take for the rebate and for inventory).
    sizes                   median take 1.0-1.8k QUOTE; 130+ distinct takers, the top 20 holding 43-48% of the flow.
    fees                    makers pay ~19 bps (class 0) / ~5 bps (class 1), takers are paid ~17 / ~3 bps.

So on each book, each round, the agent takes with probability `rate_c<k>`:
  - in the direction of its predicted 20 s move when |prediction| clears `edge_bps_c<k>` (the informed part), the
    prediction being `coef_fund` x the gap to the book's fundamental plus `coef_mom` x the mid's move over ~5 s and
    `coef_imb` x the top-of-book imbalance, the inputs the subnet's predictors use,
  - otherwise, with probability `noise_c<k>`, in a random direction, or the one that brings its inventory back,
and sizes each take as a lognormal around `notional` QUOTE. Several instances with different seeds and settings make
a field of takers rather than one (the counterparty measure P11 punishes a maker fed by one dedicated taker).

It keeps within the validator's daily volume cap (10 x miner wealth per book), paced per hour, and stops adding to
an inventory gap beyond `inventory_band` of wealth, as a real taker would have to.

Local test (from the agents directory, with the proxy and simulator running):
    python mainnet_field/FieldTakerAgent.py --port 8888 --agent_id 1 --params seed=1 rate_c0=0.02
"""
import gc
import math
import random

import bittensor as bt
from taos.common.agents import launch
from taos.im.agents import FinanceAgent
from taos.im.protocol.models import OrderDirection, STP

HOUR = 3_600_000_000_000

# Per-class settings; `<name>_c<k>` in --params overrides one for class k. Classes past the last listed use its row.
CLASS_PARAMS = {
    0: {"rate": 0.03, "edge_bps": 1.5, "noise": 0.10, "coef_fund": 0.02, "coef_mom": 0.3, "coef_imb": 1.0},
    1: {"rate": 0.06, "edge_bps": 99.0, "noise": 0.60, "coef_fund": 0.004, "coef_mom": -0.2, "coef_imb": 0.3},
}


class FieldTakerAgent(FinanceAgent):
    """Takes liquidity like mainnet's other miners: informed on class 0, rebate and inventory driven on class 1."""

    def initialize(self):
        self.notional       = float(getattr(self.config, "notional", 1500.0))
        self.size_sigma     = float(getattr(self.config, "size_sigma", 0.6))
        self.inventory_band = float(getattr(self.config, "inventory_band", 0.2))
        self.turnover_cap   = float(getattr(self.config, "turnover_cap", 10.0))
        self.cap_margin     = float(getattr(self.config, "cap_margin", 0.9))
        self.rng            = random.Random(int(float(getattr(self.config, "seed", 0))))
        self.memory         = {}
        self.class_params   = {}
        self.class_of_book  = {}
        self.takes          = {}
        gc.set_threshold(50_000, 20, 100)

    def _class_index(self, book_id):
        index = self.class_of_book.get(int(book_id))
        if index is None:
            index = next((k for k, cls in enumerate(self.asset_classes()) if int(book_id) in cls.books), 0)
            self.class_of_book[int(book_id)] = index
        return index

    def _params(self, book_id):
        index  = self._class_index(book_id)
        params = self.class_params.get(index)
        if params is None:
            base   = CLASS_PARAMS[min(index, max(CLASS_PARAMS))]
            params = {k: float(getattr(self.config, f"{k}_c{index}", getattr(self.config, k, v))) for k, v in base.items()}
            self.class_params[index] = params
        return params

    def respond(self, state):
        response  = self.make_response()
        validator = getattr(getattr(state, "dendrite", None), "hotkey", None)
        for book_id, book in (state.books or {}).items():
            if not book.bids or not book.asks or book_id not in self.accounts:
                continue
            try:
                self._take(response, validator, book_id, book, state.timestamp)
            except Exception as ex:
                bt.logging.error(f"BOOK {book_id} | take failed: {ex}")
        return response

    def _take(self, response, validator, book_id, book, timestamp):
        best_bid, best_ask = book.bids[0], book.asks[0]
        mid = (best_bid.price + best_ask.price) / 2
        key = (validator, book_id)
        memory = self.memory.get(key)
        if memory is None or timestamp < memory["timestamp"]:
            memory = {"timestamp": timestamp, "mid": mid, "mom": 0.0, "start_base": None, "volume": {}}
            self.memory[key] = memory
        elapsed = (timestamp - memory["timestamp"]) / 1e9
        if elapsed > 0 and mid > 0 and memory["mid"] > 0:
            memory["mom"] = memory["mom"] * math.exp(-elapsed / 5) + math.log(mid / memory["mid"]) * 1e4
        memory["timestamp"], memory["mid"] = timestamp, mid

        params = self._params(book_id)
        if self.rng.random() >= params["rate"]:
            return
        account = self.accounts[book_id]
        wealth  = account.base_balance.total * mid + account.quote_balance.total
        if wealth <= 0 or self._over_cap(book_id, account, memory, timestamp):
            return

        fundamental = getattr(book, "fundamental", None)
        gap = (fundamental - mid) / mid * 1e4 if fundamental and fundamental > 0 else 0.0
        top = best_bid.quantity + best_ask.quantity
        imb = (best_bid.quantity - best_ask.quantity) / top if top > 0 else 0.0
        pred = params["coef_fund"] * gap + params["coef_mom"] * memory["mom"] + params["coef_imb"] * imb

        initial = getattr(account.base_balance, "initial", None)
        if initial is None:
            if memory["start_base"] is None:
                memory["start_base"] = account.base_balance.total
            initial = memory["start_base"]
        inventory = (account.base_balance.total - initial) * mid / wealth

        if abs(pred) >= params["edge_bps"]:
            direction, kind = (OrderDirection.BUY if pred > 0 else OrderDirection.SELL), "informed"
        elif self.rng.random() < params["noise"]:
            if abs(inventory) > self.inventory_band / 2:
                direction = OrderDirection.SELL if inventory > 0 else OrderDirection.BUY
            else:
                direction = OrderDirection.BUY if self.rng.random() < 0.5 else OrderDirection.SELL
            kind = "noise"
        else:
            return
        # Beyond the band only the side that brings the inventory back.
        if (inventory >= self.inventory_band and direction == OrderDirection.BUY) or \
           (inventory <= -self.inventory_band and direction == OrderDirection.SELL):
            return

        notional = self.notional * math.exp(self.rng.gauss(0.0, self.size_sigma) - self.size_sigma ** 2 / 2)
        decimals = self.volume_decimals(book_id)
        quantity = round(max(notional / mid, self.min_order_size(book_id) + 10 ** -decimals), decimals)
        if direction == OrderDirection.BUY and account.quote_balance.free < quantity * best_ask.price * 1.01:
            return
        if direction == OrderDirection.SELL and account.base_balance.free < quantity:
            return
        response.market_order(book_id=book_id, direction=direction, quantity=quantity, stp=STP.CANCEL_OLDEST)
        hour = timestamp // HOUR
        memory["volume"][hour] = memory["volume"].get(hour, 0.0) + quantity * mid
        counts = self.takes.setdefault((validator, self._class_index(book_id), kind), [0, 0.0])
        counts[0] += 1
        counts[1] += quantity * mid

    def _over_cap(self, book_id, account, memory, timestamp):
        """True once this book's volume reaches the validator's daily cap, or its even hourly share of it."""
        wealth = getattr(self.config_for(book_id), "miner_wealth", 0.0) or 0.0
        if wealth <= 0:
            return False
        cap  = self.turnover_cap * wealth
        hour = timestamp // HOUR
        for old in [h for h in memory["volume"] if h <= hour - 24]:
            del memory["volume"][old]
        return ((account.traded_volume or 0.0) >= self.cap_margin * cap
                or memory["volume"].get(hour, 0.0) >= 2 * cap / 24)


if __name__ == "__main__":
    launch(FieldTakerAgent)
