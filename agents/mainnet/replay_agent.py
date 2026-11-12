# SPDX-License-Identifier: MIT
"""
Replays the validator rounds a live miner recorded (agents/mainnet/state_tap.py) through a candidate agent, and
compares what it would have done with what the live agent did, on the real market, without risking a UID.

Each recorded round reaches the candidate as the miner would deliver it (the protocol's own decompress, lazy or not
per the agent's lazy_load, the validator's hotkey on the dendrite), except for the account: the live agent's resting
orders and reservations are swapped for the candidate's own simulated ones. Its replies are applied to that
simulated book of orders: placements (post-only ones that would cross the touch are refused), cancels and GTT
expiries. The live agent's own notices (its fills and order events) are withheld, so a candidate's fill-driven
adaptation stays neutral.

Fills are estimated, since the market cannot react to orders that were never sent: a resting buy is filled when a
trade printed after it reached the book (its placement time; for the candidate, the round plus ~20 ms of validator
delay) prints below its price (a resting sell, above it), up to the volume that traded through it; trades at
exactly its price count as touches only (its queue place is unknown). Trades whose maker was the live agent are left
out, since they existed only because of its orders. Each estimated fill is marked out against the book's mid 20 sim
seconds later, as the validator's making credit does: captured = edge against the mid at the fill, realized = edge
against the mid 20 s later, quality = realized / captured.

The live agent gets the same estimate on its ACTUAL resting orders (each round's recorded account, less the orders its
recorded reply cancelled, plus the ones it placed), so the two compare like for like, and its estimated fills are checked against its real fills (the trade notices it received): the
ratio says how far the estimate can be trusted on this market.

Usage (from the repository root):
    python agents/mainnet/replay_agent.py agents/Agent_10_23/MyAgent10_23_4Gpt.py
    python agents/mainnet/replay_agent.py agents/Agent_10_23/MyAgent10_23IOV.py --params lazy_load=0 max_lean=0.5 \
        --dir agents/data/mainnet_records --validators 5EWwdZB7 --since "2026-10-24 00:00" --rounds 2000

Writes agents/data/replays/<agent>_<time>/summary.json and summary.md, and prints the table.
"""
import argparse
import bisect
import calendar
import importlib.util
import json
import math
import statistics
import sys
import time
import types
import zlib
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(Path(__file__).resolve().parent)]

from state_tap import DEFAULT_DIR, read_frames, recordings  # noqa: E402

MARKOUT_NS = 20_000_000_000
S3_K = 4.0                       # debeta.py s3_k: miner-sourced making credit counts up to this x the background's
# A reply's instructions reach the book after the validator's delay (reward.py set_delays: 10 ms up to 1 s, exponential
# in the call time's share of the 3 s timeout; the mainnet call time ~0.72 s gives ~26 ms). Cancels land after it too:
# an order the reply cancels can still be filled until then, which is how quotes resting into a fundamental reseed
# are picked off before their cancel arrives.
ARRIVAL_DELAY_NS = 25_000_000
OUT_DIR = REPO / "agents" / "data" / "replays"
# Phases of the fundamental's 30 s reseed cycle (sim seconds since the boundary; the published fundamental changes only
# in the round at second 0 mod 30), for the fills-by-phase table.
RESEED_NS = 30_000_000_000
PHASES = ((0, 1, "0-1s"), (1, 5, "1-5s"), (5, 28, "5-28s"), (28, 30, "28-30s"))


# A reply normally takes effect in the same simulated second (+ARRIVAL_DELAY_NS). From ~21:45 CEST on 23 Oct the mainnet
# validator applied every reply one round late (the next state still showed the orders the reply had cancelled, and
# its placements reached the book at the next round's timestamp + ~35 ms). --lag-rounds auto detects that regime round
# by round from the live agent's own recorded cancels (lag_by_frame); 1 forces it; 0 keeps the old timing exactly.
LAGGED_ARRIVAL_NS = 35_000_000
LAG_WINDOW = 10            # rounds smoothed by the detector
LAG_THRESHOLD = 0.2        # share of the previous reply's cancels still resting above which the round is lagged


def next_timestamps(frames):
    """frame index -> the timestamp of the next frame from the same validator with a LARGER timestamp (the recording
    repeats a timestamp now and then), or None for the last one."""
    out, pending = [None] * len(frames), defaultdict(list)
    for i, frame in enumerate(frames):
        waiting = pending[frame["validator"]]
        keep = []
        for j in waiting:
            if frame["timestamp"] > frames[j]["timestamp"]:
                out[j] = frame["timestamp"]
            else:
                keep.append(j)
        pending[frame["validator"]] = keep + [i]
    return out


def measure_lag(frames, uid, classes_of=None, limit=None):
    """Share of the live agent's cancels (of orders resting at round k) that take effect only after round k+1's state:
    the order is still resting in the next round's account. Only consecutive rounds with distinct timestamps count.
    Returns {"all": share, <class>: share, "phase:<name>": share, "n": cancels counted}."""
    last, counts = {}, defaultdict(lambda: [0, 0])
    classes_of = classes_of or {}
    for n, frame in enumerate(frames):
        if limit and n >= limit:
            break
        raw = decode_raw(frame)
        accounts = (raw.get("accounts") or {}).get(uid) or (raw.get("accounts") or {}).get(str(uid)) or {}
        resting = {int(b): {o["i"] for o in (a.get("o") or [])} for b, a in accounts.items()}
        if not classes_of:
            classes_of = book_classes(raw)
        prev = last.get(frame["validator"])
        if prev is not None and frame["timestamp"] > prev[0]:
            for book, ids in prev[1].items():
                for oid in ids:
                    late = oid in resting.get(book, set())
                    for key in ("all", classes_of.get(book, "market"), "phase:" + phase_name(prev[0])):
                        counts[key][0] += late
                        counts[key][1] += 1
        cancelled = defaultdict(set)
        for ins in ((frame.get("response") or {}).get("instructions") or []):
            if ins.get("type") == "CANCEL_ORDERS":
                book = int(ins.get("bookId", -1))
                for c in ins.get("cancellations") or []:
                    if c.get("orderId") in resting.get(book, set()):
                        cancelled[book].add(c.get("orderId"))
        if prev is None or frame["timestamp"] > prev[0]:
            last[frame["validator"]] = (frame["timestamp"], cancelled)
    out = {k: (late / total if total else 0.0) for k, (late, total) in counts.items()}
    out["n"] = counts["all"][1]
    return out


def lag_by_frame(frames, uid):
    """frame index -> 1 when the validator applied replies one round late around that round, else 0: the share of
    the live agent's previous-reply cancels (of orders resting then) still resting in this round's account, smoothed
    over the last LAG_WINDOW distinct rounds of the same validator."""
    out, last, recent = [0] * len(frames), {}, defaultdict(list)
    for n, frame in enumerate(frames):
        raw = decode_raw(frame)
        accounts = (raw.get("accounts") or {}).get(uid) or (raw.get("accounts") or {}).get(str(uid)) or {}
        resting = {int(b): {o["i"] for o in (a.get("o") or [])} for b, a in accounts.items()}
        validator = frame["validator"]
        prev = last.get(validator)
        if prev is not None and frame["timestamp"] > prev[0]:
            total = sum(len(ids) for ids in prev[1].values())
            if total:
                late = sum(1 for book, ids in prev[1].items() for oid in ids if oid in resting.get(book, set()))
                recent[validator] = (recent[validator] + [late / total])[-LAG_WINDOW:]
        shares = sorted(recent[validator])
        out[n] = int(bool(shares) and shares[len(shares) // 2] > LAG_THRESHOLD)
        cancelled = defaultdict(set)
        for ins in ((frame.get("response") or {}).get("instructions") or []):
            if ins.get("type") == "CANCEL_ORDERS":
                book = int(ins.get("bookId", -1))
                for c in ins.get("cancellations") or []:
                    if c.get("orderId") in resting.get(book, set()):
                        cancelled[book].add(c.get("orderId"))
        if prev is None or frame["timestamp"] > prev[0]:
            last[validator] = (frame["timestamp"], cancelled)
    return out


def phase_name(ts):
    second = (ts % RESEED_NS) / 1e9
    return next((name for lo, hi, name in PHASES if lo <= second < hi), PHASES[-1][2])


# ── recorded rounds ──────────────────────────────────────────────────────────────────────────────────────────────

def load_rounds(directory, validators, since, limit, max_gap_s=0.0):
    """Recorded frames from every kept validator, in the order the miner received them.

    With max_gap_s, the frames stop at the first silence longer than that: across one the miner was down or pruned,
    and the next frames may belong to another uid (26 Oct: a --since window ran on from UID 8 into UID 229's
    recording across an 18.8 h gap and mixed the two, shield_grid_log.md review #68)."""
    frames = []
    for validator, files in recordings(directory).items():
        if validators and not any(validator.startswith(p) for p in validators):
            continue
        # each validator's files are read in order; no validator can put more than `limit` frames into the first
        # `limit` overall, and none past its own first long silence, so its reading stops there - the whole recording
        # (now several times the window) never sits in memory (26 Oct: 8.4 GB two minutes into a 4715-round replay)
        kept, last = 0, None
        for path in files:
            for frame in read_frames(path):
                if since and frame["received"] < since:
                    continue
                if limit and kept >= limit:
                    break
                if max_gap_s > 0 and last is not None and frame["received"] - last > max_gap_s:
                    at = time.strftime("%Y-%m-%d %H:%M", time.gmtime(last))
                    print(f"{validator[:8]}: recording silent for {(frame['received'] - last) / 3600:.1f} h after {at} UTC "
                          f"(after {kept} rounds): the rounds past it are left out (--since past the gap to replay them, "
                          f"--max-gap-s 0 to keep both)", file=sys.stderr, flush=True)
                    break
                frames.append(frame)
                kept, last = kept + 1, frame["received"]
            else:
                continue
            break
    frames.sort(key=lambda f: f["received"])
    if max_gap_s > 0:
        for i in range(1, len(frames)):
            gap = frames[i]["received"] - frames[i - 1]["received"]
            if gap > max_gap_s:
                at = time.strftime("%Y-%m-%d %H:%M", time.gmtime(frames[i - 1]["received"]))
                print(f"Recording silent for {gap / 3600:.1f} h after {at} UTC (round {i}): replaying the {i} rounds "
                      f"before it, {len(frames) - i} after it left out (--since past the gap to replay them, "
                      f"--max-gap-s 0 to keep both)", file=sys.stderr, flush=True)
                del frames[i:]
                break
    return frames[:limit] if limit else frames


def decode_raw(frame):
    from taos.im.utils.compress import decompress
    return decompress(frame["compressed"], frame.get("engine") or "lz4", frame.get("version"))


def book_classes(raw):
    """book id -> class name, from the configuration the validator sent ('market' when it lists no classes)."""
    config = raw.get("config") or {}
    out = {}
    for cls in config.get("classes") or []:
        for book in cls.get("books") or []:
            out[int(book)] = cls.get("name") or "class"
    return out


def mids(raw_books):
    out = {}
    for book_id, book in raw_books.items():
        bids, asks = book.get("b") or [], book.get("a") or []
        if bids and asks:
            out[int(book_id)] = ((bids[0]["p"] + asks[0]["p"]) / 2, bids[0]["p"], asks[0]["p"])
    return out


def trades(raw_books):
    """{book: [trade event]} printed since the previous round."""
    return {int(b): [e for e in (book.get("e") or []) if e.get("y") == "t"] for b, book in raw_books.items()}


# ── making on the validator's own basis (debeta.accumulate_book_capture over the recorded prints) ───────────────

CANDIDATE_UID = 1_000_000      # the candidate's id in the merged print stream (no real uid is this large)
CENTERED_WINDOW = 15           # scoring.debeta.centered_window, the default


def validator_making(prints, fills, classes, live_uid, candidate, fill_trades=None, takes=None):
    """Per class: making on the validator's basis, from the recorded trade prints of each book.

    markouts() measures capture against the book's quote mid at the round before a fill and marks it at the quote mid
    20 s later. The validator (debeta.accumulate_book_capture) measures capture against a centred, size-floored mid of
    the trade prints around the fill (W = 15 prints each side) and marks it at the size-weighted background-sided
    prints near the 20 s horizon, a fill with no mark in reach booking nothing. For a fill from a deep sweep the two
    differ most. Here the recorded prints of each book run through the validator's own function as one offline batch:
    for the live agent as recorded (its own maker prints included), for the candidate with the live agent's maker prints
    taken out and the candidate's estimated fills put in at their times (maker CANDIDATE_UID, the taker id of the trade
    each fill rode on). Then making = per-book 2 x min(buy, sell capture) x the uid's markout quality
    (debeta.balanced_reward_per_book, debeta.markout_quality); S3 and P11 are not applied here.

    `takes` (--takes 1, #160): the candidate's takes enter the stream as prints with the candidate as TAKER. The validator's
    capture attribution books both sides of every fill (debeta._attribute_capture: buyer (mid - price) x q, seller the
    negation; only the REALIZED sums are maker-only), so a take's negative capture lowers that side of the candidate's
    two-sided making credit exactly as it would live."""
    from taos.im.validator.debeta import accumulate_book_capture, balanced_reward_per_book, markout_quality
    uid = CANDIDATE_UID if candidate else live_uid
    by_book = defaultdict(list)
    if candidate:
        for validator, book, side, price, qty, ts, _mid, background, *_ in fills:
            # the maker's side 0 (a resting bid) was hit by a taker selling: trade side 1
            by_book[(validator, book)].append({"p": price, "q": qty, "s": 1 if int(side) == 0 else 0, "t": ts,
                                               "Ma": CANDIDATE_UID, "Ta": -1 if background else 2_000_000})
    if candidate and takes:
        for validator, book, side, price, qty, ts, *_ in takes:
            # a take that buys is trade side 0 (taker buys); the maker is someone else (background here)
            # 0.6.6 (--making-rule 066): "taking never earns making credit" - the take still prints (it moves the
            # centred mid) but is not booked to the candidate as a taker leg
            by_book[(validator, book)].append({"p": price, "q": qty, "s": 0 if int(side) == 0 else 1, "t": ts,
                                               "Ma": -1, "Ta": -3 if MAKING_066 else CANDIDATE_UID})
    # the validator's sums are {uid: {book: value}} created on first use (debeta._attribute_capture indexes them)
    # a candidate fill took its quantity from the taker of the trade it rode on, so that recorded print shrinks by it
    # (a sweep that the candidate's quote absorbed prints less beyond it); without this the deeper prints stay and
    # pull the centred mid against the candidate
    consumed = defaultdict(float)
    if candidate and fill_trades:
        for (validator, book, trade_id), fill in zip(fill_trades, fills):
            if trade_id is not None:
                consumed[(validator, book, trade_id)] += fill[4]
    cb, cs, rb, rs = defaultdict(dict), defaultdict(dict), defaultdict(dict), defaultdict(dict)
    for key, recorded in prints.items():
        kept = []
        for t in recorded:
            if candidate and t.get("Ma") == live_uid:
                continue
            take = consumed.get((key[0], key[1], t.get("i"))) if candidate else None
            if take:
                left = (t.get("q") or 0.0) - take
                if left <= 1e-12:
                    continue
                t = dict(t, q=left)
            kept.append(t)
        stream = kept + by_book.get(key, [])
        if not any(t.get("Ma") == uid or (candidate and t.get("Ta") == uid) for t in stream):
            continue
        stream.sort(key=lambda t: t.get("t") or 0)
        book = key[1]
        accumulate_book_capture(cb, cs, book, stream, CENTERED_WINDOW, real_buy_sums=rb, real_sell_sums=rs,
                                horizon_ns=MARKOUT_NS)
    quality = markout_quality(cb, cs, rb, rs, [uid]).get(uid, 0.0)
    per_class = defaultdict(lambda: [{}, {}])
    for book, v in (cb.get(uid) or {}).items():
        per_class[classes.get(book, "market")][0][book] = v
    for book, v in (cs.get(uid) or {}).items():
        per_class[classes.get(book, "market")][1][book] = v
    out = {}
    for cls, (b, s) in per_class.items():
        two_sided = balanced_reward_per_book({uid: b}, {uid: s}, [uid]).get(uid, 0.0)
        out[cls] = {"making_vb": two_sided * quality, "two_sided_capture_vb": two_sided, "uid_quality_vb": quality,
                    "buy_capture_vb": sum(b.values()), "sell_capture_vb": sum(s.values()),
                    "books_vb": len(set(b) | set(s))}
    return out


# ── skill estimate: the validator's per-book alpha on the replayed fills ─────────────────────────────────────────

SKILL_HURDLE_BPS = 2.3      # a book counts toward skill once |alpha| clears this share of its filled notional


def skill_estimate(fills, mid_history, classes):
    """Per class: the skill leg's net alpha, counted books and kappa, from these fills.

    debeta.accumulate_book_mtm at round granularity on the mid: the position (BASE bought less sold since the replay
    started; a constant offset cancels in the formula) earns each round's mid change, alpha = mtm - mean position x
    drift, and a book counts once |alpha| >= SKILL_HURDLE_BPS of its filled notional (debeta.kappa_floored). Prints
    are approximated by mids, so treat it as a comparison between agents on the same recording, not the published
    value."""
    from taos.im.validator.debeta import kappa_of_alpha
    by_book = defaultdict(list)
    for validator, book, side, price, qty, ts, *_ in fills:
        by_book[(validator, book)].append((ts, qty if int(side) == 0 else -qty, price * qty))
    out = defaultdict(lambda: {"net_alpha": 0.0, "skill_books": 0, "alphas": []})
    for key, book_fills in by_book.items():
        history = mid_history.get(key) or []
        if len(history) < 2:
            continue
        book_fills.sort()
        pos = mtm = invsum = drift = 0.0
        n, i = 0, 0
        notional = sum(f[2] for f in book_fills)
        last_mid = history[0][1]
        for ts, mid in history:
            dp = mid - last_mid
            mtm += pos * dp
            drift += dp
            while i < len(book_fills) and book_fills[i][0] <= ts:
                pos += book_fills[i][1]
                i += 1
            invsum += pos
            n += 1
            last_mid = mid
        alpha = mtm - invsum / n * drift
        if notional > 0 and abs(alpha) >= SKILL_HURDLE_BPS / 1e4 * notional:
            c = out[classes.get(key[1], "market")]
            c["net_alpha"] += alpha
            c["skill_books"] += 1
            c["alphas"].append(alpha)
    return {cls: {"net_alpha": c["net_alpha"], "skill_books": c["skill_books"],
                  "kappa": kappa_of_alpha(c["alphas"]),
                  # the per-book alphas themselves (#136): the validator's kappa pools ALL of a uid's books, so a pooled
                  # kappa needs every class's list, not the per-class kappas
                  "alphas": [round(a, 4) for a in c["alphas"]]} for cls, c in out.items()}


def skill_from_positions(pos_history, mid_history, classes, notional_by_book):
    """Per class: net alpha / counted books / kappa from the live agent's RECORDED positions (BASE total less initial,
    each round, read before the candidate's balances are swapped in) instead of estimated fills - the same formula as
    skill_estimate, so the gap between the two isolates the fill estimate, and the gap to the published alpha isolates
    the formula and the mid basis (review #50)."""
    from taos.im.validator.debeta import kappa_of_alpha
    out = defaultdict(lambda: {"net_alpha": 0.0, "skill_books": 0, "alphas": []})
    for key, positions in pos_history.items():
        history = mid_history.get(key) or []
        if len(history) < 2 or len(positions) < 2:
            continue
        pos_at = dict(positions)
        pos = positions[0][1]
        mtm = invsum = drift = 0.0
        n = 0
        last_mid = history[0][1]
        for ts, mid in history:
            dp = mid - last_mid
            mtm += pos * dp
            drift += dp
            pos = pos_at.get(ts, pos)
            invsum += pos
            n += 1
            last_mid = mid
        alpha = mtm - invsum / n * drift
        notional = notional_by_book.get(key, 0.0)
        if notional > 0 and abs(alpha) >= SKILL_HURDLE_BPS / 1e4 * notional:
            c = out[classes.get(key[1], "market")]
            c["net_alpha"] += alpha
            c["skill_books"] += 1
            c["alphas"].append(alpha)
    return {cls: {"net_alpha_recorded_pos": c["net_alpha"], "skill_books_recorded_pos": c["skill_books"]}
            for cls, c in out.items()}


# ── fill estimate, shared by the candidate and the live agent ────────────────────────────────────────────────────

# Price keys of the queue model: 6 decimals, so an order priced a millionth of a tick above its grid point (the agents'
# grid_nudge, ~1e-8 on a 0.01 tick) is matched to its level and to the trades printed there.
PRICE_KEY = 6
PRICE_TOL = 1e-6


def levels(raw_books):
    """{book: ({bid price: quantity}, {ask price: quantity})} of the visible levels, prices rounded to PRICE_KEY places."""
    out = {}
    for book_id, book in raw_books.items():
        out[int(book_id)] = ({round(lv["p"], PRICE_KEY): lv["q"] for lv in book.get("b") or []},
                             {round(lv["p"], PRICE_KEY): lv["q"] for lv in book.get("a") or []})
    return out


class FillBook:
    """Resting orders of one agent on one validator, filled by trades that print through them.

    `queue_model` "none" (the original rule): only a trade printing through the order's price fills it; a trade at
    its price never does. "prob": a trade through the price still fills it, and trades at its price fill it once the
    queue ahead of it is used up. The queue ahead starts as the level's visible quantity when the order is first seen
    (less the order itself for the live agent, whose quotes are in the recorded book), each trade at the price takes
    from it first, and a level that shrinks by more than it traded (cancels) moves the order up by the share
    ahead^n / (ahead^n + behind^n) of the cancelled quantity (hftbacktest's power probability queue model, n =
    `queue_power`). A price inside the spread starts at the front."""

    def __init__(self, live_uid, queue_model="none", queue_power=2.0, own_in_book=False):
        self.live_uid = live_uid
        self.fills = []         # (validator, book, side, price, qty, time, mid at fill, taker is the background)
        self.touches = 0
        self.queue_model = queue_model
        self.queue_power = queue_power
        self.own_in_book = own_in_book
        self.queue = {}         # (validator, book, order id) -> [quantity ahead, level quantity last seen]
        self.queue_fills = 0
        self.fill_trades = []   # per fill, parallel to self.fills: (validator, book, the recorded trade's id)
        self.fill_events = []   # per fill, parallel too: (the recorded trade dict, the filled order's id)

    def _level(self, book_levels, side, price):
        bids, asks = book_levels or ({}, {})
        return (bids if side == 0 else asks).get(round(price, PRICE_KEY))

    def match(self, validator, book_id, orders, book_trades, mid, prev_levels=None, now_levels=None, replaced=None):
        """Fills `orders` (dicts with s, p, q, i) against the round's trades; returns {order id: filled qty}.

        `prev_levels` / `now_levels`: the book's levels (from `levels`) at the round the orders rested from and at
        this round, for the "prob" queue model. `replaced`: {(side, price): quantity} the live agent had resting there,
        for the candidate, which takes the live agent's place: that quantity is not ahead of it in the queue, and
        the trades that filled it at the candidate's price count as flow reaching the candidate."""
        filled = {}
        through = [t for t in book_trades if t.get("Ma") != self.live_uid]
        prob = self.queue_model == "prob"
        # at the order's own price the candidate also meets the trades the live agent's quote took there
        at_price = book_trades if (prob and replaced is not None) else through
        live_ids = set()
        for o in orders:
            remaining = o["q"]
            buy = o["s"] == 0
            key = (validator, book_id, o["i"])
            live_ids.add(key)
            state = self.queue.get(key) if prob else None
            if prob and state is None:
                level = self._level(prev_levels, o["s"], o["p"]) or 0.0
                own = o["q"] if self.own_in_book else (replaced or {}).get((o["s"], round(o["p"], PRICE_KEY)), 0.0)
                ahead = max(0.0, level - own)
                state = self.queue[key] = [ahead, level]
            at_level = 0.0
            for t in (at_price if prob else through):
                if remaining <= 1e-12:
                    break
                if t["t"] <= o["t"]:
                    continue        # printed before the order was resting
                if o.get("_until") is not None and t["t"] >= o["_until"]:
                    continue        # printed after its cancel reached the book
                # a sell taker (s 1) hits bids, a buy taker (s 0) lifts asks
                tol = PRICE_TOL if prob else 1e-12
                if buy and t["s"] == 1 and t["p"] < o["p"] - tol or (not buy) and t["s"] == 0 and t["p"] > o["p"] + tol:
                    if t.get("Ma") == self.live_uid:
                        continue    # through its price: the old rule's flow only (the live agent's own fills left out)
                    qty = min(remaining, t["q"])
                    remaining -= qty
                    filled[o["i"]] = filled.get(o["i"], 0.0) + qty
                    self.fills.append((validator, book_id, o["s"], o["p"], qty, t["t"], mid, (t.get("Ta") or 0) < 0))
                    self.fill_trades.append((validator, book_id, t.get("i")))
                    self.fill_events.append((t, o["i"]))
                elif abs(t["p"] - o["p"]) <= tol and ((buy and t["s"] == 1) or (not buy and t["s"] == 0)):
                    self.touches += 1
                    if not prob:
                        continue
                    at_level += t["q"]
                    take = t["q"]
                    if state[0] > 0:
                        used = min(state[0], take)
                        state[0] -= used
                        take -= used
                    if take > 1e-12:
                        qty = min(remaining, take)
                        remaining -= qty
                        filled[o["i"]] = filled.get(o["i"], 0.0) + qty
                        self.queue_fills += 1
                        self.fills.append((validator, book_id, o["s"], o["p"], qty, t["t"], mid, (t.get("Ta") or 0) < 0))
                        self.fill_trades.append((validator, book_id, t.get("i")))
                        self.fill_events.append((t, o["i"]))
            if prob and now_levels is not None:
                # cancels ahead: the level shrank by more than traded at it since the last round
                level_now = self._level(now_levels, o["s"], o["p"])
                if level_now is not None:
                    cancelled = state[1] - level_now - at_level
                    if cancelled > 1e-12 and state[0] > 0:
                        behind = max(0.0, state[1] - state[0] - (o["q"] if self.own_in_book else 0.0))
                        n = self.queue_power
                        share = state[0] ** n / (state[0] ** n + behind ** n) if state[0] + behind > 0 else 1.0
                        state[0] = max(0.0, state[0] - cancelled * share)
                    state[1] = level_now
        return filled

    def forget(self, validator, keep):
        """Drops the queue state of this validator's orders not in `keep` ({(book, order id)}): no longer resting."""
        if self.queue_model != "prob":
            return
        for key in [k for k in self.queue if k[0] == validator and (k[1], k[2]) not in keep]:
            del self.queue[key]


class Virtual:
    """The candidate's own resting orders per validator and book, kept from its replies."""

    def __init__(self, takes=False):
        self.orders = defaultdict(dict)     # (validator, book) -> {id: raw order}
        # --takes 1 (28 Oct, shield_grid_log #157): a crossing limit order without postOnly, or a market order, is a TAKE -
        # queued here and filled against the next recorded round's visible levels (see take_fills), never rested
        self.takes = takes
        self.pending_takes = defaultdict(list)   # validator -> [(book, side, limit price or None, qty, placed ts)]
        self.taken = 0
        self.dying = defaultdict(dict)      # cancelled, still on the book until their cancel lands (`_until`)
        # order ids are uint32 on the wire; engine ids count up from 0, so the top of the range never meets them
        self.next_id = 3_500_000_000
        self.refused = 0
        self.placed = 0
        self.cancelled = 0

    def apply(self, validator, timestamp, instructions, touch, land=None):
        """`land`: when this reply takes effect on the book (default the round + ARRIVAL_DELAY_NS)."""
        land = timestamp + ARRIVAL_DELAY_NS if land is None else land
        for ins in instructions or []:
            kind = ins.get("type")
            book = int(ins.get("bookId", -1))
            key = (validator, book)
            if kind == "CANCEL_ORDERS":
                for c in ins.get("cancellations") or []:
                    order = self.orders[key].pop(c.get("orderId"), None)
                    if order is not None:
                        self.cancelled += 1
                        self.dying[key][order["i"]] = dict(order, _until=land)
            elif kind == "PLACE_ORDER_MARKET" and self.takes:
                self.pending_takes[validator].append((book, int(ins["direction"]), None, float(ins["quantity"]), timestamp))
                self.taken += 1
            elif kind == "PLACE_ORDER_LIMIT":
                side, price, qty = int(ins["direction"]), float(ins["price"]), float(ins["quantity"])
                best = touch.get(book)
                if self.takes and not ins.get("postOnly") and best and ((side == 0 and price >= best[2]) or (side == 1 and price <= best[1])):
                    self.pending_takes[validator].append((book, side, price, qty, timestamp))
                    self.taken += 1
                    continue
                if ins.get("postOnly") and best and ((side == 0 and price >= best[2]) or (side == 1 and price <= best[1])):
                    self.refused += 1
                    continue
                self.next_id += 1
                expiry = ins.get("expiryPeriod") if int(ins.get("timeInForce") or 0) == 1 else None
                self.orders[key][self.next_id] = {"y": "o", "i": self.next_id, "c": None,
                                                  "t": land, "q": qty,
                                                  "s": side, "p": price, "l": 0.0,
                                                  "_expires": timestamp + int(expiry) if expiry else None}
                self.placed += 1

    def expire(self, validator, timestamp):
        for (v, _), orders in self.orders.items():
            if v != validator:
                continue
            for oid in [i for i, o in orders.items() if o["_expires"] is not None and o["_expires"] <= timestamp]:
                del orders[oid]


def resting_after(orders_by_book, instructions, timestamp, touch, land=None, pending_cancels=None):
    """The live agent's resting orders until its next round: its account's orders less the ones its reply cancelled,
    plus the ones it placed (post-only ones that would cross the touch refused), as Virtual does for the candidate.
    `land`: when this reply takes effect (default the round + ARRIVAL_DELAY_NS). `pending_cancels`: {book: ids} the
    previous reply cancelled; in a lagged round those still on the book go at this round + LAGGED_ARRIVAL_NS."""
    land = timestamp + ARRIVAL_DELAY_NS if land is None else land
    out = {book: {o["i"]: o for o in orders} for book, orders in orders_by_book.items()}
    for book, ids in (pending_cancels or {}).items():
        for oid in ids:
            order = out.get(book, {}).get(oid)
            if order is not None:
                order["_until"] = timestamp + LAGGED_ARRIVAL_NS
    next_id = 4_000_000_000
    for ins in instructions or []:
        book = int(ins.get("bookId", -1))
        if ins.get("type") == "CANCEL_ORDERS":
            for c in ins.get("cancellations") or []:
                order = out.get(book, {}).get(c.get("orderId"))
                if order is not None:          # still fillable until the cancel lands
                    order["_until"] = min(order.get("_until") or land, land)
        elif ins.get("type") == "PLACE_ORDER_LIMIT":
            side, price = int(ins["direction"]), float(ins["price"])
            best = touch.get(book)
            if ins.get("postOnly") and best and ((side == 0 and price >= best[2]) or (side == 1 and price <= best[1])):
                continue
            next_id += 1
            out.setdefault(book, {})[next_id] = {"i": next_id, "t": land,
                                                 "q": float(ins["quantity"]), "s": side, "p": price}
    return {book: list(orders.values()) for book, orders in out.items()}


def take_fills(pending, book_levels, touch, validator, timestamp):
    """Fills the queued takes of one validator against this round's visible levels: a buy walks the asks upward (a sell
    the bids downward) while the level price is within its limit (any price for a market order), taking each level's
    quantity in turn. Returns [(validator, book, side, price, qty, ts, mid, False)] in the fills' layout. The recorded book
    is not changed (no impact on later rounds), so this is the 'last in the batch' price: other takers' prints of the
    previous round are already in it (shield_grid_log #155-#156)."""
    out = []
    for book, side, limit, qty, placed in pending:
        lv = book_levels.get(book)
        if not lv:
            continue
        bids, asks = lv
        ladder = sorted(asks.items()) if side == 0 else sorted(bids.items(), reverse=True)
        mid = (touch.get(book) or (None,))[0]
        left = qty
        for price, avail in ladder:
            if left <= 1e-12:
                break
            if limit is not None and ((side == 0 and price > limit + 1e-12) or (side == 1 and price < limit - 1e-12)):
                break
            q = min(left, avail)
            if q > 0:
                out.append((validator, book, side, price, q, timestamp, mid, False))
                left -= q
    return out


def fill_notices(fb, start, validator, uid):
    """The candidate's estimated fills from index `start` on, for `validator`, as the ET trade notices the live agent
    receives for its own maker fills (the recorded print it rode on, with this uid as maker, the filled quantity and the
    candidate's order id; the maker fee scaled to that quantity). Delivered in the next round's payload, as the engine
    reports fills since the previous state (--own-notices, review #51)."""
    out = []
    for fill, (t, oid) in zip(fb.fills[start:], fb.fill_events[start:]):
        if fill[0] != validator:
            continue
        qty = fill[4]
        share = qty / t["q"] if t.get("q") else 1.0
        n = {k: t.get(k) for k in ("t", "i", "Ta", "Ti", "Tf", "s", "p", "cr", "Toi") if k in t}
        n.update({"y": "ET", "a": uid, "b": int(fill[1]), "c": None, "Ma": uid, "Mi": int(oid), "q": qty,
                  "Mf": (t.get("Mf") or 0.0) * share})
        out.append(n)
    return out


OWN_VOLUME = False           # --own-volume: set in main()
MAKING_066 = False           # --making-rule 066: set in main()


def swap_account(raw, uid, validator, virtual, timestamp=None, offsets=None):
    """The round's payload with the live agent's resting orders and reservations replaced by the candidate's. With
    `timestamp` (lagged rounds), only what is on the book at that moment: placements that have not landed yet are
    left out, and cancelled orders whose cancel has not landed yet are still shown. `offsets`: {(validator, book):
    [BASE, QUOTE, traded volume]} added to the recorded totals (--own-balances), so the candidate holds its own position;
    the volume entry is added to the account's traded_volume `v` only under --own-volume 1."""
    accounts = (raw.get("accounts") or {}).get(uid) or (raw.get("accounts") or {}).get(str(uid)) or {}
    for book_id, account in accounts.items():
        key = (validator, int(book_id))
        if offsets is not None and key in offsets:
            for name, delta in (("bb", offsets[key][0]), ("qb", offsets[key][1])):
                balance = account.get(name) or {}
                if "t" in balance:
                    balance["t"] = max(0.0, balance["t"] + delta)
            if OWN_VOLUME and account.get("v") is not None:
                account["v"] = max(0.0, float(account["v"]) + offsets[key][2])
        visible = list(virtual.orders[key].values())
        if timestamp is not None:
            visible = [o for o in visible if o["t"] <= timestamp] + \
                      [o for o in virtual.dying[key].values() if o["_until"] > timestamp]
        orders = [{k: v for k, v in o.items() if not k.startswith("_")} for o in visible]
        account["o"] = orders
        base_reserved = sum(o["q"] for o in orders if o["s"] == 1)
        quote_reserved = sum(o["q"] * o["p"] for o in orders if o["s"] == 0)
        for key, reserved in (("bb", base_reserved), ("qb", quote_reserved)):
            balance = account.get(key) or {}
            if "t" in balance:
                balance["r"] = reserved
                balance["f"] = max(0.0, balance["t"] - reserved)
    notices = raw.get("notices") or {}
    for key in (uid, str(uid)):
        if key in notices:
            notices[key] = []
    return raw


# ── candidate agent, as the miner runs it ────────────────────────────────────────────────────────────────────────

def load_agent(path, uid, params, workdir):
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = getattr(module, Path(path).stem)
    config = types.SimpleNamespace(**params)
    config.data_dir = str(workdir / "data")
    argv, sys.argv = sys.argv, [str(REPO / "taos/im/neurons/miner.py"), "--netuid", "79"]   # as under the miner
    try:
        agent = cls(uid, config, str(workdir / "logs"))
    finally:
        sys.argv = argv
    return agent


def run_candidate(agent, frame, raw, virtual):
    """The candidate's reply to one round, delivered through the protocol's decompress; (instructions, seconds)."""
    from taos.im.protocol import MarketSimulationStateUpdate
    from taos.im.utils.compress import compress
    version = frame.get("version")
    books = raw.pop("books")
    synapse = MarketSimulationStateUpdate(timestamp=frame["timestamp"], version=version,
                                          compression_engine=frame.get("engine") or "lz4")
    synapse.compressed = {"books": frame["compressed"]["books"],
                          "payload": compress(raw, level=1, engine=synapse.compression_engine, version=version)}
    raw["books"] = books
    synapse.dendrite.hotkey = frame["validator"]
    start = time.time()
    synapse.decompress(lazy=bool(getattr(agent.config, "lazy_load", False)))
    response = agent.handle(synapse)
    seconds = time.time() - start
    dumped = response.model_dump(mode="json") if hasattr(response, "model_dump") else (response or {})
    return dumped.get("instructions") or [], seconds


# ── report ───────────────────────────────────────────────────────────────────────────────────────────────────────

BLOCK_NS = 300 * 10**9      # per-block output: 10 reseed cycles of 30 sim s


def block_stats(fills, mid_history, block_ns=BLOCK_NS, t0=None):
    """Per block of `block_ns` sim time (from `t0`, the replay's first round, so candidate, live and every variant of a
    grid share the same blocks; default the first fill) and class: fills, capture C and clamped realized R in QUOTE
    (as markouts / the validator's _capped_mark), buy and sell capture, and the per-book two-sided capture within the
    block. Additive across blocks except two_sided, so paired variant-minus-live differences can be bootstrapped over
    blocks and CSCV run on the block x variant matrix (shield_grid_log reviews #15, #25). "sides" keeps each book's
    [buy, sell] capture per block, from which the window's two-sided capture (min over summed blocks) follows."""
    import bisect
    times = {key: [t for t, _ in hist] for key, hist in mid_history.items()}
    t0 = min((f[5] for f in fills), default=0) if t0 is None else t0
    blank = lambda: {"n": 0, "C": 0.0, "R": 0.0, "buyC": 0.0, "sellC": 0.0}
    blocks = defaultdict(lambda: defaultdict(blank))
    sides = defaultdict(lambda: [0.0, 0.0])        # (block, cls, validator, book) -> [buy, sell] capture
    for validator, book, side, price, qty, ts, mid_then, _background, cls in fills:
        if not mid_then:
            continue
        hist = mid_history[(validator, book)]
        i = bisect.bisect_left(times[(validator, book)], ts + MARKOUT_NS)
        later = hist[i][1] if i < len(hist) else None
        sign = 1 if side == 0 else -1
        cap = sign * (mid_then - price) * qty
        k = int((ts - t0) // block_ns)
        s = blocks[k][cls]
        s["n"] += 1
        s["C"] += cap
        s["buyC" if side == 0 else "sellC"] += cap
        if later is not None:
            s["R"] += max(-abs(cap), min(abs(cap), sign * (later - price) * qty))
        sides[(k, cls, validator, book)][0 if side == 0 else 1] += cap
    for (k, cls, validator, book), (buy, sell) in sides.items():
        blocks[k][cls]["two_sided"] = blocks[k][cls].get("two_sided", 0.0) + max(0.0, 2.0 * min(buy, sell))
        # each book's buy / sell capture in the block, so a tool can take 2 x min over ANY set of blocks - the
        # validator's min is over its whole window, not per block (shield_grid_log free-time #74)
        blocks[k][cls].setdefault("sides", {})[f"{validator[:8]}:{book}"] = [buy, sell]
    return {"block_s": block_ns / 1e9, "t0": t0,
            "blocks": [{"k": k, **{cls: dict(v) for cls, v in blocks[k].items()}} for k in sorted(blocks)]}


def markouts(fills, mid_history):
    """Per class: notional, captured and realized edge (bps, notional-weighted), quality, buy/sell notional, the
    same by reseed phase under "phases"; and under "all" the uid-wide figures (the validator's quality is one ratio
    over every book)."""
    blank = lambda: {"fills": 0, "notional": 0.0, "captured": 0.0, "realized": 0.0, "marked": 0.0,
                     "buy": 0.0, "sell": 0.0}
    out, by_phase = defaultdict(blank), defaultdict(blank)
    # The validator's own basis (debeta.py): per book capture by side in QUOTE, making = sum over books of
    # 2 x min(buy, sell capture) (_two_sided_per_book), and a quality from each fill's realized spread clamped to
    # +/- its captured spread (_capped_mark), summed over every fill of the uid before the ratio (markout_quality).
    sides = defaultdict(lambda: [0.0, 0.0])
    q_cap, q_real = defaultdict(float), defaultdict(float)
    # S3 (debeta.py, s3_k 4): making credit sourced from other miners counts up to 4x the credit sourced from the
    # background market, per class; the taker of the trade an estimated fill rode on stands in for its counterparty.
    by_source = defaultdict(lambda: [0.0, 0.0])          # cls -> [background capture, miner capture]
    for validator, book, side, price, qty, ts, mid_then, background, cls in fills:
        notional = price * qty
        sign = 1 if side == 0 else -1
        later = next((m for t, m in mid_history[(validator, book)] if t >= ts + MARKOUT_NS), None)
        if mid_then:
            cap = sign * (mid_then - price) * qty
            sides[(cls, validator, book)][0 if side == 0 else 1] += cap
            q_cap[cls] += cap
            q_cap["all"] += cap
            by_source[cls][0 if background else 1] += max(0.0, cap)
            if later is not None:
                real = sign * (later - price) * qty
                real = max(-abs(cap), min(abs(cap), real))
                q_real[cls] += real
                q_real["all"] += real
        for s in (out[cls], out["all"], by_phase[(cls, phase_name(ts))]):
            s["fills"] += 1
            s["notional"] += notional
            s["buy" if side == 0 else "sell"] += notional
            if later is None or not mid_then:
                continue
            s["captured"] += sign * (mid_then - price) / price * 1e4 * notional
            s["realized"] += sign * (later - price) / price * 1e4 * notional
            s["marked"] += notional

    def figures(s):
        m = s["marked"]
        return {"fills": s["fills"], "notional": s["notional"],
                "captured_bps": s["captured"] / m if m else None, "realized_bps": s["realized"] / m if m else None,
                "quality": max(0.0, min(1.0, s["realized"] / s["captured"])) if s["captured"] > 0 else None,
                "buy_share": s["buy"] / s["notional"] if s["notional"] else None}
    result = {cls: figures(s) for cls, s in out.items()}
    two_sided = defaultdict(float)
    for (cls, _, _), (buy, sell) in sides.items():
        two_sided[cls] += max(0.0, 2.0 * min(buy, sell))
        two_sided["all"] += max(0.0, 2.0 * min(buy, sell))
    uid_quality = max(0.0, min(1.0, q_real["all"] / q_cap["all"])) if q_cap["all"] > 0 else 0.0
    for cls, r in result.items():
        r["two_sided_capture"] = two_sided.get(cls, 0.0)
        r["quality_clamped"] = max(0.0, min(1.0, q_real[cls] / q_cap[cls])) if q_cap[cls] > 0 else None
        r["uid_quality_clamped"] = uid_quality
        background, miners = by_source.get(cls, (0.0, 0.0))
        r["background_share"] = background / (background + miners) if background + miners > 0 else None
        r["s3"] = (background + min(miners, S3_K * background)) / (background + miners) if background + miners > 0 else 1.0
        r["making_no_s3"] = two_sided.get(cls, 0.0) * uid_quality
        r["making"] = r["making_no_s3"] * r["s3"]                 # this class's making credit, validator basis
    for (cls, phase), s in by_phase.items():
        result[cls].setdefault("phases", {})[phase] = figures(s)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("agent", help="Candidate agent file (class named like the file).")
    parser.add_argument("--params", nargs="*", default=[], help="key=value agent params, as for run_miner.sh -m.")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="Recording directory.")
    parser.add_argument("--validators", default="", help="Comma-separated validator hotkey prefixes (default: all).")
    parser.add_argument("--since", default="", help="Only rounds received after this UTC time, 'YYYY-mm-dd HH:MM'.")
    parser.add_argument("--rounds", type=int, default=0, help="At most this many rounds (default: all).")
    parser.add_argument("--uid", type=int, default=None, help="Live miner uid (default: the account in the recording).")
    parser.add_argument("--max-gap-s", type=float, default=600.0,
                        help="Stop at the first silence in the recording longer than this many wall seconds (the miner "
                             "was down or pruned and may have come back under another uid); 0 keeps everything.")
    parser.add_argument("--lag-rounds", default="auto", choices=("auto", "0", "1"),
                        help="Replies take effect one round late: auto (detected per round from the recording), "
                             "1 (always) or 0 (never: the round + ARRIVAL_DELAY_NS, the old timing).")
    parser.add_argument("--queue-model", default="none", choices=("none", "prob"),
                        help="Fills at the order's own price: none (never; only trades through it fill, the old rule) "
                             "or prob (once the queue ahead, from the recorded level, is traded or cancelled away).")
    parser.add_argument("--queue-power", type=float, default=2.0,
                        help="n of the prob queue model: the share of a level's cancels taken from ahead of the order.")
    parser.add_argument("--own-notices", type=int, default=1, choices=(0, 1),
                        help="1 (default from 26 Oct 03:40): deliver the candidate's own estimated fills back to it as "
                             "trade notices in the next round, so fill-driven logic (fill balance, markout adaptation, "
                             "Kelly edge stats) runs; 0: its own notices stay withheld, as before 26 Oct. Validated on "
                             "the final set since 04:13 UTC 25 Oct: c0 fills / live 1.53 -> 1.33, c0 making_vb / live "
                             "1.18 -> 0.95, c1 fills 0.83 -> 0.93 (c2 making_vb 0.54 -> 0.34).")
    parser.add_argument("--takes", type=int, default=0, choices=(0, 1),
                        help="1: the candidate's crossing non-post-only limit orders and market orders are TAKES, filled "
                             "against the next round's recorded levels up to their limit (take_fills; no impact on the "
                             "recorded book) and counted in its position, balances and skill leg - not in making. 0 "
                             "(default): as before 28 Oct (a crossing order rests as a virtual order).")
    parser.add_argument("--own-balances", type=int, default=1, choices=(0, 1),
                        help="1 (default from 25 Oct 10:40): the candidate sees its own BASE / QUOTE totals (the live "
                             "totals moved by its estimated fills less the live agent's real ones), so its inventory "
                             "skew and bands act on its own position; 0: it sees the live agent's balances (replays "
                             "before 25 Oct). On identical params, 1 took class 1's buy share from 0.13 to 0.49 (live "
                             "0.54) and class 2's alpha to -55 (live -54).")
    parser.add_argument("--making-rule", default="065", choices=("065", "066"),
                        help="065 (default): the candidate's takes (--takes 1) are booked as taker legs in its captured "
                             "sums, as the 0.6.5 validator does. 066: the 0.6.6 rule, making credit on maker fills only - "
                             "takes still print (they move the centred mid) but earn and cost no making credit.")
    parser.add_argument("--own-volume", type=int, default=0, choices=(0, 1),
                        help="1 (29 Oct): the candidate sees its OWN traded volume per book (the recorded v plus its "
                             "estimated fill notional less the live agent's real maker fills), so its pace / volume-cap "
                             "logic stops on its own budget, as the validator's rolling cap would (query.py ~398). "
                             "Needs --own-balances 1. 0 (default, parity with older replays): it sees the live v.")
    parser.add_argument("--field-making", default="16188,7056,1541",
                        help="The field's making credit per class (validator class_gauges making_credit, live 29 Oct run "
                             "20261029_0111) for the class-weighted index: the validator normalises making within each "
                             "class and mixes the classes at their book share (reward.py class_pool_shares), so a unit of "
                             "credit is worth w_c / field_c. Comma-separated in class order (simulation_0, 1, ...).")
    args = parser.parse_args()

    import bittensor as bt
    bt.logging.set_warning()
    since = calendar.timegm(time.strptime(args.since, "%Y-%m-%d %H:%M")) if args.since else None   # UTC, DST-safe
    frames = load_rounds(args.dir, [p for p in args.validators.split(",") if p], since, args.rounds, args.max_gap_s)
    if not frames:
        sys.exit(f"No recorded rounds in {args.dir}.")
    first = decode_raw(frames[0])
    uid = args.uid if args.uid is not None else int(next(iter(first.get("accounts") or {0: None})))
    last_accounts = decode_raw(frames[-1]).get("accounts") or {}
    if last_accounts and uid not in {int(k) for k in last_accounts}:
        sys.exit(f"The window starts as uid {uid} but ends as uid {', '.join(map(str, last_accounts))}: "
                 f"narrow it with --since / --rounds.")
    params = {"lazy_load": 1.0}
    for kv in args.params:
        k, _, v = kv.partition("=")
        try:
            params[k] = float(v)        # as the miner's ParseKwargs does: "0" must arrive as 0.0, not a true string
        except ValueError:
            params[k] = v
    name = Path(args.agent).stem
    # Unique per run: parallel replays of one agent (a parameter grid) finish within the same second.
    stamp = time.strftime('%Y%m%d_%H%M%S')
    # mkdir without exist_ok is the atomic claim: two replays started in the same second both passed an exists()
    # check and shared one folder, so the second summary.json overwrote the first (29 Oct grid_c0skill Z / H)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    workdir = OUT_DIR / f"{name}_{stamp}"
    n = 1
    while True:
        try:
            workdir.mkdir()
            break
        except FileExistsError:
            n += 1
            workdir = OUT_DIR / f"{name}_{stamp}_{n}"
    agent = load_agent(args.agent, uid, params, workdir)
    print(f"Replaying {len(frames)} rounds from {len({f['validator'] for f in frames})} validator(s) through {name} "
          f"(uid {uid}, lazy_load {int(bool(agent.config.lazy_load))})", flush=True)

    classes = book_classes(first)
    following = next_timestamps(frames)
    if args.lag_rounds == "auto":
        lagged = lag_by_frame(frames, uid)
        print(f"Lag regime: {sum(lagged)} of {len(frames)} rounds applied one round late (detected)", flush=True)
    else:
        lagged = [int(args.lag_rounds)] * len(frames)
    pending_live = {}                 # validator -> {book: ids} the live agent's previous reply cancelled
    virtual = Virtual(takes=bool(args.takes))
    cand_takes = []                   # the candidate's takes (--takes 1), in the fills' layout
    cand_fb = FillBook(uid, args.queue_model, args.queue_power)
    # (validator, book) -> [BASE, QUOTE] the candidate holds beyond the live agent's recorded totals (--own-balances)
    offsets = defaultdict(lambda: [0.0, 0.0, 0.0]) if args.own_balances else None
    global OWN_VOLUME, MAKING_066
    MAKING_066 = str(args.making_rule) == "066"
    OWN_VOLUME = bool(args.own_volume) and offsets is not None
    delivered = defaultdict(int)          # validator -> index into cand_fb.fills already delivered as notices
    live_fb = FillBook(uid, args.queue_model, args.queue_power, own_in_book=True)
    mid_history = defaultdict(list)
    recorded_prints = defaultdict(list)   # (validator, book) -> the round's trade prints, in order, for validator_making
    previous = {}                     # validator -> (live resting orders by book, candidate orders by book, mids)
    live_real = defaultdict(float)    # class -> real fill notional from the live agent's trade notices
    live_book_notional = defaultdict(float)   # (validator, book) -> the same per book, for the recorded-position alpha
    pos_history = defaultdict(list)           # (validator, book) -> [(timestamp, recorded BASE beyond initial)]
    seconds, live_seconds, failures = [], [], 0
    live_quotes = defaultdict(list)   # class -> half spread bps of the live agent's resting quotes
    cand_quotes = defaultdict(list)
    started = time.time()
    for n, frame in enumerate(frames, 1):
        validator = frame["validator"]
        raw = decode_raw(frame)
        books = raw.get("books") or {}
        touch = mids(books)
        for book, (mid, _, _) in touch.items():
            mid_history[(validator, book)].append((frame["timestamp"], mid))
        classes = classes or book_classes(raw)
        printed = trades(books)
        for book, book_trades in printed.items():
            if book_trades:
                recorded_prints[(validator, book)].extend(book_trades)
        now_levels = levels(books) if args.queue_model != "none" else {}
        if args.takes and virtual.pending_takes.get(validator):
            done = take_fills(virtual.pending_takes.pop(validator), now_levels or levels(books), touch, validator,
                              frame["timestamp"])
            cand_takes.extend(done)
            if offsets is not None:
                for _, book, side, price, qty, *_ in done:
                    sign = 1.0 if side == 0 else -1.0           # a buy adds BASE and spends QUOTE
                    offsets[(validator, book)][0] += sign * qty
                    offsets[(validator, book)][1] -= sign * qty * price
                    offsets[(validator, book)][2] += qty * price
        # fills between the previous round of this validator and this one, for orders resting at that round
        if validator in previous:
            live_orders, cand_orders, prev_touch, prev_levels = previous[validator]
            for book, book_trades in printed.items():
                mid = prev_touch.get(book, (None,))[0]
                pl, nl = prev_levels.get(book), now_levels.get(book)
                live_fb.match(validator, book, live_orders.get(book, []), book_trades, mid, pl, nl)
                replaced = None
                if args.queue_model != "none":
                    replaced = defaultdict(float)
                    for lo in live_orders.get(book, []):
                        replaced[(lo["s"], round(lo["p"], PRICE_KEY))] += lo["q"]
                filled = cand_fb.match(validator, book, cand_orders.get(book, []), book_trades, mid, pl, nl, replaced)
                for oid, qty in filled.items():
                    order = virtual.orders[(validator, book)].get(oid)
                    if offsets is not None:
                        o = order or virtual.dying[(validator, book)].get(oid)
                        if o is not None:
                            sign = 1.0 if o["s"] == 0 else -1.0       # a filled bid adds BASE and spends QUOTE
                            offsets[(validator, book)][0] += sign * qty
                            offsets[(validator, book)][1] -= sign * qty * o["p"]
                            offsets[(validator, book)][2] += qty * o["p"]
                    if order is None and oid in virtual.dying[(validator, book)]:
                        order = virtual.dying[(validator, book)][oid]
                        order["q"] -= qty
                        if order["q"] <= 1e-9:
                            del virtual.dying[(validator, book)][oid]
                        continue
                    if order is not None:
                        order["q"] -= qty
                        if order["q"] <= 1e-9:
                            del virtual.orders[(validator, book)][oid]
        # the live agent's real fills, from its own trade notices
        for notice in ((raw.get("notices") or {}).get(uid) or (raw.get("notices") or {}).get(str(uid)) or []):
            if notice.get("y") in ("ET", "EVENT_TRADE") and notice.get("Ma") == uid:
                live_real[classes.get(int(notice.get("b", notice.get("bookId", -1))), "other")] += notice.get("p", 0) * notice.get("q", 0)
                live_book_notional[(validator, int(notice.get("b", notice.get("bookId", -1))))] += notice.get("p", 0) * notice.get("q", 0)
                if offsets is not None:
                    # the recorded totals already carry this real fill: take it back out of the candidate's view
                    # (taker side 1 = a sell hit the live agent's bid, so the live agent bought)
                    bought = 1.0 if int(notice.get("s", 0)) == 1 else -1.0
                    key = (validator, int(notice.get("b", notice.get("bookId", -1))))
                    offsets[key][0] -= bought * float(notice.get("q", 0))
                    offsets[key][1] += bought * float(notice.get("q", 0)) * float(notice.get("p", 0))
                    offsets[key][2] -= float(notice.get("q", 0)) * float(notice.get("p", 0))
        # what the live agent had resting until the next round: its account now, after the reply it gave to it
        accounts = (raw.get("accounts") or {}).get(uid) or (raw.get("accounts") or {}).get(str(uid)) or {}
        for b, a in accounts.items():       # before swap_account replaces them with the candidate's
            bb = a.get("bb") or {}
            if "t" in bb:
                pos_history[(validator, int(b))].append((frame["timestamp"], (bb.get("t") or 0.0) - (bb.get("i") or 0.0)))
        lag = lagged[n - 1]
        nxt = following[n - 1]
        land = nxt + LAGGED_ARRIVAL_NS if lag and nxt is not None else None
        live_instructions = (frame.get("response") or {}).get("instructions")
        live_orders = resting_after({int(b): [dict(o) for o in a.get("o") or []] for b, a in accounts.items()},
                                    live_instructions, frame["timestamp"], touch, land=land,
                                    pending_cancels=pending_live.get(validator) if lag else None)
        if lag:
            pending = defaultdict(set)
            for ins in live_instructions or []:
                if ins.get("type") == "CANCEL_ORDERS":
                    for c in ins.get("cancellations") or []:
                        pending[int(ins.get("bookId", -1))].add(c.get("orderId"))
            pending_live[validator] = pending
        else:
            pending_live.pop(validator, None)
        for book, orders in live_orders.items():
            mid = touch.get(book, (None,))[0]
            for o in orders:
                if mid and o.get("_until") is None:
                    live_quotes[classes.get(book, "market")].append(abs(o["p"] - mid) / mid * 1e4)
        live_seconds.append(frame.get("seconds") or 0.0)
        virtual.expire(validator, frame["timestamp"])
        try:
            swapped = swap_account(raw, uid, validator, virtual, frame["timestamp"] if lag else None, offsets)
            if args.own_notices:
                own = fill_notices(cand_fb, delivered[validator], validator, uid)
                delivered[validator] = len(cand_fb.fills)
                if own:
                    notices = swapped.setdefault("notices", {})
                    key = uid if uid in (swapped.get("accounts") or {}) else str(uid)
                    notices[key] = list(notices.get(key) or []) + own
            instructions, took = run_candidate(agent, frame, swapped, virtual)
            seconds.append(took)
        except Exception as ex:
            failures += 1
            instructions = []
            if failures <= 3:
                print(f"  round {n}: candidate failed: {ex!r}", flush=True)
        virtual.apply(validator, frame["timestamp"], instructions, touch, land=land)
        cand_orders = defaultdict(list)
        for (v, book), orders in virtual.orders.items():
            if v != validator:
                continue
            mid = touch.get(book, (None,))[0]
            for o in orders.values():
                cand_orders[book].append(o)
                if mid:
                    cand_quotes[classes.get(book, "market")].append(abs(o["p"] - mid) / mid * 1e4)
        # cancelled: still fillable until the cancel lands, then gone (a lagged cancel lives into the next window)
        for (v, book), orders in list(virtual.dying.items()):
            if v == validator:
                cand_orders[book].extend(orders.values())
                if lag and nxt is not None:
                    for oid in [i for i, o in orders.items() if o["_until"] <= nxt]:
                        del orders[oid]
                    if not orders:
                        del virtual.dying[(v, book)]
                else:
                    del virtual.dying[(v, book)]
        if args.queue_model != "none":
            live_fb.forget(validator, {(b, o["i"]) for b, os in live_orders.items() for o in os})
            cand_fb.forget(validator, {(b, o["i"]) for b, os in cand_orders.items() for o in os})
        previous[validator] = (live_orders, cand_orders, touch, now_levels)
        if n % 200 == 0:
            print(f"  {n}/{len(frames)} rounds, {time.time() - started:.0f}s", flush=True)

    tag = lambda fills: [(*f, classes.get(f[1], "market")) for f in fills]
    cand_mk, live_mk = markouts(tag(cand_fb.fills), mid_history), markouts(tag(live_fb.fills), mid_history)
    for mk, fb in ((cand_mk, cand_fb), (live_mk, live_fb)):
        try:
            for cls, est in skill_estimate(fb.fills + (cand_takes if fb is cand_fb else []), mid_history, classes).items():
                mk.setdefault(cls, {}).update(est)
        except Exception as ex:
            print(f"  skill estimate failed: {ex!r}", flush=True)
    try:
        for cls, est in skill_from_positions(pos_history, mid_history, classes, live_book_notional).items():
            live_mk.setdefault(cls, {}).update(est)
    except Exception as ex:
        print(f"  recorded-position skill failed: {ex!r}", flush=True)
    for mk, fb, is_candidate in ((cand_mk, cand_fb, True), (live_mk, live_fb, False)):
        try:
            for cls, est in validator_making(recorded_prints, fb.fills, classes, uid, is_candidate, fb.fill_trades,
                                             takes=cand_takes if is_candidate else None).items():
                mk.setdefault(cls, {}).update(est)
        except Exception as ex:
            print(f"  validator-basis making failed: {ex!r}", flush=True)
    per_block = {}
    for who, fb in (("candidate", cand_fb), ("live", live_fb)):
        try:
            per_block[who] = block_stats(tag(fb.fills), mid_history, t0=frames[0]["timestamp"] if frames else None)
        except Exception as ex:
            print(f"  block stats failed: {ex!r}", flush=True)
    pct = lambda xs, q: sorted(xs)[min(len(xs) - 1, int(q * len(xs)))] if xs else None
    summary = {
        "per_block": per_block,
        "agent": name, "params": params, "rounds": len(frames), "lag_rounds": args.lag_rounds,
        "lagged_rounds": sum(lagged), "validators": sorted({f["validator"] for f in frames}),
        "live_uid": uid, "candidate_failed_rounds": failures,
        "reply_seconds": {"candidate_p50": pct(seconds, 0.5), "candidate_p90": pct(seconds, 0.9),
                          "candidate_max": max(seconds) if seconds else None,
                          "live_p50": pct(live_seconds, 0.5), "live_p90": pct(live_seconds, 0.9)},
        "orders": {"placed": virtual.placed, "refused_post_only": virtual.refused, "cancelled": virtual.cancelled},
        "own_balances": args.own_balances, "own_notices": args.own_notices, "queue_model": args.queue_model, "queue_fills": {"candidate": cand_fb.queue_fills, "live": live_fb.queue_fills},
        "classes": {}, "touches": {"candidate": cand_fb.touches, "live": live_fb.touches},
    }
    summary["all"] = {"candidate": cand_mk.pop("all", {}), "live": live_mk.pop("all", {})}
    if args.takes:
        taken = defaultdict(lambda: {"takes": 0, "notional": 0.0, "mk_num": 0.0})
        for v, book, side, price, qty, ts, mid, _ in cand_takes:
            h = mid_history.get((v, book)) or []
            j = bisect.bisect_left(h, (ts + 20_000_000_000, 0))
            c = taken[classes.get(book, "market")]
            c["takes"] += 1
            c["notional"] += price * qty
            if j < len(h):
                c["mk_num"] += price * qty * (1 if side == 0 else -1) * (h[j][1] - price) / price * 1e4
        summary["takes"] = {cls: {"takes": c["takes"], "notional": c["notional"],
                                  "markout_20s_bps": c["mk_num"] / c["notional"] if c["notional"] else None}
                            for cls, c in taken.items()}
        summary["takes_queued"] = virtual.taken
    for cls in sorted(set(cand_mk) | set(live_mk) | set(cand_quotes) | set(live_quotes)):
        summary["classes"][cls] = {
            "candidate": {**cand_mk.get(cls, {}), "quote_distance_bps_median": pct(cand_quotes[cls], 0.5)},
            "live": {**live_mk.get(cls, {}), "quote_distance_bps_median": pct(live_quotes[cls], 0.5),
                     "real_fill_notional": live_real.get(cls, 0.0)},
        }
    # class-weighted making index (29 Oct): sum_c w_c * making_c / field_c x 1e5, w_c = the class's share of the books.
    # The replay window is far shorter than the validator's, so only differences between variants mean anything.
    counts = Counter(classes.values()) if classes else Counter()
    field = [float(x) for x in str(args.field_making).split(",") if x.strip()]
    order = sorted(counts)
    weighted = {}
    for who in ("candidate", "live"):
        tot = 0.0
        for i, cls in enumerate(order):
            if i < len(field) and field[i] > 0:
                tot += counts[cls] / sum(counts.values()) * ((summary["classes"].get(cls) or {}).get(who) or {}).get("making_vb", 0.0) / field[i]
        weighted[who] = tot * 1e5
    summary["weighted_making"] = weighted
    (workdir / "summary.json").write_text(json.dumps(summary, indent=2))
    fmt = lambda v, s="{:.3g}": "–" if v is None else s.format(v)
    lines = [f"# Replay: {name} on {len(frames)} recorded rounds", "",
             f"Validators {', '.join(v[:8] for v in summary['validators'])}; live uid {uid}; params "
             f"{' '.join(f'{k}={v}' for k, v in params.items())}.", "",
             f"Reply time: candidate p50 {fmt(summary['reply_seconds']['candidate_p50'])} s, p90 "
             f"{fmt(summary['reply_seconds']['candidate_p90'])} s, max {fmt(summary['reply_seconds']['candidate_max'])} s "
             f"(live p50 {fmt(summary['reply_seconds']['live_p50'])} s). Failed rounds {failures}. Orders placed "
             f"{virtual.placed}, refused post-only {virtual.refused}, cancelled {virtual.cancelled}.", "",
             "| Class | Who | Est. fills | Est. notional | Captured bps | Realized bps | Quality | Buy share | Quote dist bps | Real fills (live) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for cls, both in summary["classes"].items():
        for who in ("candidate", "live"):
            s = both[who]
            lines.append(f"| {cls} | {who} | {s.get('fills', 0)} | {fmt(s.get('notional'), '{:.0f}')} | "
                         f"{fmt(s.get('captured_bps'), '{:+.2f}')} | {fmt(s.get('realized_bps'), '{:+.2f}')} | "
                         f"{fmt(s.get('quality'), '{:.2f}')} | {fmt(s.get('buy_share'), '{:.2f}')} | "
                         f"{fmt(s.get('quote_distance_bps_median'), '{:.1f}')} | "
                         f"{fmt(s.get('real_fill_notional'), '{:.0f}') if who == 'live' else ''} |")
    lines += ["", "Making credit on the validator's basis (sum over books of 2 x min(buy, sell capture), times the uid "
              "quality with each fill's realized spread clamped to its capture): "
              + "; ".join(f"{cls} candidate {fmt(both['candidate'].get('making'), '{:.3g}')} / live "
                          f"{fmt(both['live'].get('making'), '{:.3g}')}" for cls, both in summary["classes"].items())
              + f"; uid quality (clamped) candidate {fmt(summary['all']['candidate'].get('uid_quality_clamped'), '{:.2f}')}"
              f" / live {fmt(summary['all']['live'].get('uid_quality_clamped'), '{:.2f}')}."]
    lines += ["", f"Class-weighted making index (w_c x making_c / field_c x 1e5, field {args.field_making}): candidate "
              f"{fmt(weighted.get('candidate'), '{:.1f}')} / live {fmt(weighted.get('live'), '{:.1f}')}."]
    lines += ["", "Uid-wide quality (the validator's one ratio over every book): candidate "
              f"{fmt(summary['all']['candidate'].get('quality'), '{:.2f}')} at {fmt(summary['all']['candidate'].get('realized_bps'), '{:+.2f}')} bps, "
              f"live {fmt(summary['all']['live'].get('quality'), '{:.2f}')} at {fmt(summary['all']['live'].get('realized_bps'), '{:+.2f}')} bps.",
              "", "By reseed phase (sim seconds since the fundamental's 30 s boundary):", "",
              "| Class | Who | Phase | Est. fills | Est. notional | Captured bps | Realized bps | Quality |",
              "|---|---|---|---|---|---|---|---|"]
    for cls, both in summary["classes"].items():
        for who in ("candidate", "live"):
            phases = both[who].get("phases") or {}
            for _, _, phase in PHASES:
                s = phases.get(phase)
                if s:
                    lines.append(f"| {cls} | {who} | {phase} | {s['fills']} | {fmt(s['notional'], '{:.0f}')} | "
                                 f"{fmt(s['captured_bps'], '{:+.2f}')} | {fmt(s['realized_bps'], '{:+.2f}')} | "
                                 f"{fmt(s['quality'], '{:.2f}')} |")
    lines += ["", "Est. = fills estimated from trades printing through the order's price (see the module docstring); "
              "compare the live row's estimate with its real fills to see how far the estimate holds on this market."]
    (workdir / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nSaved {workdir.relative_to(REPO)}/summary.md")


if __name__ == "__main__":
    main()
