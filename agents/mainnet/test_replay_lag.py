# SPDX-License-Identifier: MIT
"""
Offline tests for replay_agent.py's one-round-lag model (--lag-rounds), on synthetic frames (no recording needed).

    python agents/mainnet/test_replay_lag.py

L01 next_timestamps skips repeated timestamps; L02 lag_by_frame detects a lagged regime from the live agent's own
cancels and not before it; L03 Virtual.apply lands placements and cancels at the given time; L04 swap_account in a
lagged round hides placements that have not landed and still shows orders whose cancel has not landed; L05
resting_after keeps the previous reply's cancelled orders fillable until this round + LAGGED_ARRIVAL_NS; L06 with
land=None everything is exactly the old +ARRIVAL_DELAY_NS timing.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay_agent as ra          # noqa: E402

SEC = 1_000_000_000
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def frame(ts, resting, cancels, validator="v"):
    """A recorded round: the live account's resting order ids on book 1, and its reply cancelling `cancels`."""
    return {"validator": validator, "timestamp": ts, "received": ts / SEC,
            "_raw": {"accounts": {7: {"1": {"o": [{"i": i} for i in resting]}}}},
            "response": {"instructions": [{"type": "CANCEL_ORDERS", "bookId": 1,
                                           "cancellations": [{"orderId": i} for i in cancels]}] if cancels else []}}


def l01_next_timestamps():
    frames = [frame(1 * SEC, [], []), frame(1 * SEC, [], []), frame(2 * SEC, [], []), frame(3 * SEC, [], [])]
    got = ra.next_timestamps(frames)
    check("L01 next_timestamps: the next LARGER timestamp from the same validator", got == [2 * SEC, 2 * SEC, 3 * SEC, None],
          str(got))


def l02_lag_detection():
    # rounds 0-9: every cancel lands in time (the id is gone next round); rounds 10-29: cancelled ids are still resting
    frames, next_id = [], 100
    for k in range(30):
        lagged = k >= 10
        resting = [next_id, next_id + 1] + ([next_id - 2] if lagged and k > 10 else [])
        frames.append(frame((k + 1) * SEC, resting, [next_id]))
        next_id += 2 if not lagged else 0
    with mock.patch.object(ra, "decode_raw", lambda f: f["_raw"]):
        got = ra.lag_by_frame(frames, 7)
    check("L02 lag_by_frame: 0 while cancels land in time, 1 once they stay resting (after the smoothing window)",
          sum(got[:10]) == 0 and all(got[20:]), str(got))


def l03_apply_land():
    v = ra.Virtual()
    v.orders[("v", 1)][5] = {"i": 5, "t": 0, "q": 1.0, "s": 0, "p": 10.0, "_expires": None}
    ins = [{"type": "CANCEL_ORDERS", "bookId": 1, "cancellations": [{"orderId": 5}]},
           {"type": "PLACE_ORDER_LIMIT", "bookId": 1, "direction": 1, "price": 11.0, "quantity": 1.0}]
    v.apply("v", 1 * SEC, ins, {}, land=2 * SEC + ra.LAGGED_ARRIVAL_NS)
    placed = next(iter(v.orders[("v", 1)].values()))
    check("L03 Virtual.apply(land=...): the cancel lands (and the placement arrives) at the next round + 35 ms",
          v.dying[("v", 1)][5]["_until"] == 2 * SEC + ra.LAGGED_ARRIVAL_NS and placed["t"] == 2 * SEC + ra.LAGGED_ARRIVAL_NS)


def l04_swap_account_visibility():
    v = ra.Virtual()
    v.orders[("v", 1)][6] = {"i": 6, "t": 2 * SEC + ra.LAGGED_ARRIVAL_NS, "q": 1.0, "s": 1, "p": 11.0, "_expires": None}
    v.dying[("v", 1)][5] = {"i": 5, "t": 0, "q": 1.0, "s": 0, "p": 10.0, "_until": 2 * SEC + ra.LAGGED_ARRIVAL_NS}
    raw = {"accounts": {7: {"1": {"bb": {"t": 5.0}, "qb": {"t": 100.0}}}}}
    lagged = ra.swap_account(raw, 7, "v", v, timestamp=2 * SEC)["accounts"][7]["1"]["o"]
    raw2 = {"accounts": {7: {"1": {"bb": {"t": 5.0}, "qb": {"t": 100.0}}}}}
    plain = ra.swap_account(raw2, 7, "v", v)["accounts"][7]["1"]["o"]
    check("L04 swap_account at a lagged round: shows the order whose cancel has not landed, hides the unlanded placement",
          [o["i"] for o in lagged] == [5] and [o["i"] for o in plain] == [6], f"lagged {lagged}, plain {plain}")


def l05_resting_after_pending():
    orders = {1: [{"i": 5, "t": 0, "q": 1.0, "s": 0, "p": 10.0}, {"i": 8, "t": 0, "q": 1.0, "s": 1, "p": 11.0}]}
    out = ra.resting_after(orders, [], 2 * SEC, {}, land=3 * SEC + ra.LAGGED_ARRIVAL_NS, pending_cancels={1: {5}})
    by_id = {o["i"]: o for o in out[1]}
    check("L05 resting_after: an order the previous reply cancelled stays fillable until this round + 35 ms",
          by_id[5]["_until"] == 2 * SEC + ra.LAGGED_ARRIVAL_NS and "_until" not in by_id[8])


def l06_default_timing():
    v = ra.Virtual()
    v.orders[("v", 1)][5] = {"i": 5, "t": 0, "q": 1.0, "s": 0, "p": 10.0, "_expires": None}
    ins = [{"type": "CANCEL_ORDERS", "bookId": 1, "cancellations": [{"orderId": 5}]},
           {"type": "PLACE_ORDER_LIMIT", "bookId": 1, "direction": 1, "price": 11.0, "quantity": 1.0}]
    v.apply("v", 1 * SEC, ins, {})
    placed = next(iter(v.orders[("v", 1)].values()))
    out = ra.resting_after({1: [{"i": 9, "t": 0, "q": 1.0, "s": 0, "p": 10.0}]},
                           [{"type": "CANCEL_ORDERS", "bookId": 1, "cancellations": [{"orderId": 9}]}], 1 * SEC, {})
    check("L06 without land: the round + ARRIVAL_DELAY_NS, as before --lag-rounds",
          v.dying[("v", 1)][5]["_until"] == SEC + ra.ARRIVAL_DELAY_NS and placed["t"] == SEC + ra.ARRIVAL_DELAY_NS
          and out[1][0]["_until"] == SEC + ra.ARRIVAL_DELAY_NS)


if __name__ == "__main__":
    for test in (l01_next_timestamps, l02_lag_detection, l03_apply_land, l04_swap_account_visibility,
                 l05_resting_after_pending, l06_default_timing):
        try:
            test()
        except Exception as ex:
            import traceback
            traceback.print_exc()
            check(test.__name__, False, f"raised {ex!r}")
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
