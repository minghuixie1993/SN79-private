"""Checks of replay_agent.FillBook's queue models.

    python3 agents/mainnet/test_replay_queue.py
"""
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parent)]
import replay_agent as ra  # noqa: E402

V, B = "v", 1


def order(qty=1.0, price=100.0, side=0, t=0, oid=7):
    return {"i": oid, "s": side, "p": price, "q": qty, "t": t}


def trade(qty, price, side=1, t=10, maker=99, taker=-5):
    return {"q": qty, "p": price, "s": side, "t": t, "Ma": maker, "Ta": taker}


def lv(bids=None, asks=None):
    return (bids or {}, asks or {})


results = []


def check(name, cond):
    results.append(cond)
    print(("PASS " if cond else "FAIL ") + name)


# Q01 the old rule: a trade at the order's price never fills it
fb = ra.FillBook(1)
got = fb.match(V, B, [order()], [trade(50, 100.0)], 100.5)
check("Q01 none: a trade at the price does not fill", got == {} and fb.touches == 1)

# Q02 prob: the queue ahead (the visible level) is used up first, then the order fills
fb = ra.FillBook(1, "prob")
o = order(qty=3)
got1 = fb.match(V, B, [o], [trade(6, 100.0)], 100.5, lv({100.0: 10}), lv({100.0: 4}))
got2 = fb.match(V, B, [o], [trade(6, 100.0, t=20)], 100.5, lv({100.0: 4}), lv({100.0: 1}))
check("Q02 prob: 10 ahead, 6 traded -> no fill; 6 more -> 2 filled", got1 == {} and abs(got2.get(7, 0) - 2.0) < 1e-9)

# Q03 prob: a trade through the price fills whatever the queue
fb = ra.FillBook(1, "prob")
got = fb.match(V, B, [order(qty=2)], [trade(5, 99.5)], 100.5, lv({100.0: 50}), lv({100.0: 50}))
check("Q03 prob: a trade through the price fills in full", abs(got.get(7, 0) - 2.0) < 1e-9)

# Q04 prob: cancels at the level move the order up (all of them when nothing is behind it)
fb = ra.FillBook(1, "prob")
o = order(qty=1)
fb.match(V, B, [o], [], 100.5, lv({100.0: 10}), lv({100.0: 4}))
got = fb.match(V, B, [o], [trade(5, 100.0, t=20)], 100.5, lv({100.0: 4}), lv({100.0: 0}))
check("Q04 prob: 6 cancelled ahead of 10 -> 4 ahead; 5 traded -> 1 filled", abs(got.get(7, 0) - 1.0) < 1e-9)

# Q05 prob, live agent: its own quote is in the recorded level, so it is not counted ahead of itself
fb = ra.FillBook(1, "prob", own_in_book=True)
got = fb.match(V, B, [order(qty=2)], [trade(3, 100.0)], 100.5, lv({100.0: 2}), lv({100.0: 0}))
check("Q05 prob live: level 2 = its own 2 -> front of the queue, fills 2", abs(got.get(7, 0) - 2.0) < 1e-9)

# Q06 prob: a price inside the spread (no visible level) starts at the front
fb = ra.FillBook(1, "prob")
got = fb.match(V, B, [order(qty=1, price=100.2)], [trade(4, 100.2)], 100.5, lv({100.0: 10}), lv({100.0: 10}))
check("Q06 prob: new level -> front, fills", abs(got.get(7, 0) - 1.0) < 1e-9)

# Q07 prob: the live agent's own trades are not other people's flow
fb = ra.FillBook(1, "prob")
got = fb.match(V, B, [order(qty=1, price=100.2)], [trade(4, 100.2, maker=1)], 100.5, lv(), lv())
check("Q07 prob: trades where the live uid was the maker are skipped", got == {})

# Q08 forget drops state of orders no longer resting
fb = ra.FillBook(1, "prob")
fb.match(V, B, [order(oid=1), order(oid=2)], [], 100.5, lv({100.0: 5}), lv({100.0: 5}))
fb.forget(V, {(B, 2)})
check("Q08 forget keeps only resting orders", set(fb.queue) == {(V, B, 2)})

# Q09 candidate replacing the live agent: the live quote's size is not ahead, and trades that hit it reach the candidate
fb = ra.FillBook(1, "prob")
got = fb.match(V, B, [order(qty=2)], [trade(3, 100.0, maker=1)], 100.5, lv({100.0: 5}), lv({100.0: 2}),
               replaced={(0, 100.0): 3.0})
check("Q09 prob candidate: level 5 with live 3 -> 2 ahead; 3 traded with the live quote -> 1 filled",
      abs(got.get(7, 0) - 1.0) < 1e-9)

# Q10 candidate: a trade through its price where the live agent was the maker still does not fill it (old rule)
fb = ra.FillBook(1, "prob")
got = fb.match(V, B, [order(qty=2)], [trade(3, 99.5, maker=1)], 100.5, lv({100.0: 5}), lv({100.0: 5}), replaced={})
check("Q10 prob candidate: through-trades with the live maker stay excluded", got == {})

# Q11 a price nudged a millionth of a tick above its grid point (grid_nudge) still meets its level and its trades
fb = ra.FillBook(1, "prob")
got = fb.match(V, B, [order(qty=1, price=100.0 + 1e-8)], [trade(3, 100.0)], 100.5, lv({100.0: 2}), lv({100.0: 0}))
check("Q11 prob: nudged price -> its level (2 ahead), 3 traded -> 1 filled", abs(got.get(7, 0) - 1.0) < 1e-9)

# Q12 the old rule, kept bit for bit: a price nudged above its grid point counts a trade AT the grid price as through it,
# so a nudged order there fills with no queue at all (why the old candidate estimate ran 1.6-3x the live agent's real fills)
fb = ra.FillBook(1)
got = fb.match(V, B, [order(qty=1, price=100.0 + 1e-8)], [trade(3, 100.0)], 100.5)
check("Q12 none: nudged price, trade at the grid price -> filled as 'through' (old rule unchanged)",
      abs(got.get(7, 0) - 1.0) < 1e-9)

# Q13 validator-basis making runs the validator's own accumulator on a print stream without raising, and credits a
# two-sided maker: the live uid 1 sells at 101 and buys at 99 around prints at 100 from background makers and takers
prints = {(V, B): [{"p": 100.0, "q": 1, "s": 0, "t": i * 10**9, "Ma": -1, "Ta": -2} for i in range(40)]}
prints[(V, B)].insert(10, {"p": 101.0, "q": 1, "s": 0, "t": 10 * 10**9 + 1, "Ma": 1, "Ta": -2})
prints[(V, B)].insert(25, {"p": 99.0, "q": 1, "s": 1, "t": 25 * 10**9 + 1, "Ma": 1, "Ta": -2})
out = ra.validator_making(prints, [], {B: "simulation_0"}, 1, False)
vb = out.get("simulation_0", {})
check("Q13 validator basis: two-sided capture > 0 for a balanced maker", (vb.get("two_sided_capture_vb") or 0) > 0)

# Q14 a candidate fill takes its quantity out of the recorded trade it rode on (the deeper print shrinks)
fb = ra.FillBook(1)
fb.match(V, B, [order(qty=2, price=100.0)], [dict(trade(5, 99.5), i=77)], 100.5)
check("Q14 fill_trades records the trade each fill rode on", fb.fill_trades == [(V, B, 77)] and len(fb.fills) == 1)
base = [{"p": 100.5, "q": 1, "s": 0, "t": i * 10**9, "Ma": -1, "Ta": -2, "i": i} for i in range(40)]
base.insert(20, {"p": 99.5, "q": 5, "s": 1, "t": 10, "Ma": -1, "Ta": -2, "i": 77})
out_full = ra.validator_making({(V, B): list(base)}, fb.fills, {B: "simulation_0"}, 1, True, None)
out_take = ra.validator_making({(V, B): list(base)}, fb.fills, {B: "simulation_0"}, 1, True, fb.fill_trades)
cap = lambda o: (o.get("simulation_0") or {}).get("two_sided_capture_vb")
check("Q14 consuming the ridden trade runs and keeps the inputs unchanged", base[20]["q"] == 5 and out_take is not None)

# Q15 --own-balances: swap_account moves the recorded BASE / QUOTE totals by the candidate's offset, never below 0
raw = {"accounts": {1: {3: {"o": [], "bb": {"t": 10.0, "r": 0.0, "f": 10.0}, "qb": {"t": 500.0, "r": 0.0, "f": 500.0}}}}}
virt = ra.Virtual()
ra.swap_account(raw, 1, V, virt, None, {(V, 3): [2.5, -250.0]})
acc = raw["accounts"][1][3]
check("Q15 own balances: totals 10 -> 12.5 BASE, 500 -> 250 QUOTE, free follows",
      abs(acc["bb"]["t"] - 12.5) < 1e-9 and abs(acc["qb"]["t"] - 250.0) < 1e-9 and abs(acc["bb"]["f"] - 12.5) < 1e-9)
raw = {"accounts": {1: {3: {"o": [], "bb": {"t": 1.0}, "qb": {"t": 5.0}}}}}
ra.swap_account(raw, 1, V, virt, None, {(V, 3): [-4.0, 0.0]})
check("Q16 own balances: an offset below the recorded total clamps at 0", raw["accounts"][1][3]["bb"]["t"] == 0.0)
raw = {"accounts": {1: {3: {"o": [], "bb": {"t": 10.0}, "qb": {"t": 500.0}}}}}
ra.swap_account(raw, 1, V, virt, None, None)
check("Q17 without --own-balances the totals stay the live agent's", raw["accounts"][1][3]["bb"]["t"] == 10.0)

# Q18 block_stats: fills land in blocks counted from t0; capture and clamped realized add up; two-sided per book
S_ = 10**9
hist = {(V, B): [(i * S_, 100.0 if i < 50 else 100.02) for i in range(700)]}
fills = [(V, B, 0, 99.99, 1.0, 10 * S_, 100.0, True, "simulation_0"),      # bid, block 0, captures 0.01, marks up
         (V, B, 1, 100.01, 1.0, 20 * S_, 100.0, True, "simulation_0"),     # ask, block 0
         (V, B, 0, 99.99, 2.0, 400 * S_, 100.02, True, "simulation_0")]    # bid, block 1 (t0 = 0, 300 s blocks)
bs = ra.block_stats(fills, hist, t0=0)
b0, b1 = bs["blocks"][0]["simulation_0"], bs["blocks"][1]["simulation_0"]
check("Q18 block stats: 2 fills in block 0, 1 in block 1; capture 0.02 and two-sided 0.02 in block 0; "
      "realized clamped to +/- capture",
      b0["n"] == 2 and b1["n"] == 1 and abs(b0["C"] - 0.02) < 1e-9 and abs(b0["two_sided"] - 0.02) < 1e-9
      and abs(b1["C"] - 0.06) < 1e-9 and abs(b0["R"]) <= 0.02 + 1e-9 and bs["blocks"][1]["k"] == 1)

# Q19 fill_notices: a candidate fill comes back as the uid's own ET notice - uid as maker, the filled quantity, the
# candidate's order id, the maker fee scaled to that quantity - and only for the validator asked for
fb = ra.FillBook(1)
tr = dict(trade(5, 99.5), i=77, Ti=9, Tf=-0.1, Mi=55, Mf=2.0, cr=0, Toi=0)
fb.match(V, B, [order(qty=2, price=100.0, oid=4000000001)], [tr], 100.5)
ns = ra.fill_notices(fb, 0, V, 1)
n = ns[0] if ns else {}
check("Q19 fill_notices: ET, Ma = uid, Mi = candidate order, q = 2 of 5, Mf = 2.0 x 2/5, book and trade id kept",
      len(ns) == 1 and n.get("y") == "ET" and n.get("Ma") == 1 and n.get("Mi") == 4000000001 and abs(n.get("q") - 2.0) < 1e-12
      and abs(n.get("Mf") - 0.8) < 1e-12 and n.get("b") == B and n.get("i") == 77 and n.get("Ta") == -5
      and ra.fill_notices(fb, 0, "other validator", 1) == [] and ra.fill_notices(fb, 1, V, 1) == [])

# Q20 load_rounds stops at the first silence longer than max_gap_s (26 Oct: a --since window ran across UID 8's pruning
# into UID 229's recording); 0 keeps everything, and the round limit applies after the cut
_rec, _read = ra.recordings, ra.read_frames
ra.recordings = lambda d: {"V1": ["f"]}
ra.read_frames = lambda path: [{"received": t, "validator": "V1"} for t in (100, 110, 120, 5000, 5010)]
cut, kept, lim = ra.load_rounds("d", [], None, 0, 600), ra.load_rounds("d", [], None, 0, 0), ra.load_rounds("d", [], None, 2, 600)
ra.recordings, ra.read_frames = _rec, _read
check("Q20 load_rounds: cut at a 4880 s gap (3 rounds), max_gap 0 keeps 5, the limit applies after the cut",
      [f["received"] for f in cut] == [100, 110, 120] and len(kept) == 5 and [f["received"] for f in lim] == [100, 110])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
