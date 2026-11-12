# SPDX-License-Identifier: MIT
"""
Collects the market the real validators run (their published books, trades, simulation clock and configuration)
for the newest simulation on a network, and summarizes it per asset class, as a reference for local validation.

Source: the validators' metrics at taos.simulate.trading (see collect_real.py). What is published, and so what this
can give: `simulation_config_info` (every simulation parameter, per class), `book_gauges` (per book: mid, the touch
and five levels, depth sums, fundamental price, traded volume by side, dynamic fee rates), refreshed about every
90 s of wall time, and `trades` (the last 25 trades per book at each refresh, with maker and taker agent ids; ids
below 0 are the background market). It is a sample of the market, not the stream a miner receives: too coarse to
fit a 20 s predictor, enough for price levels, spreads, depth, gaps to the fundamental, fee rates and who trades.

Writes agents/data/real_market/<network>_<sim id>/:
    meta.json        network, validator, sim id, wall/sim time range, book -> class, the published configuration
    books.npz        times (wall s), sim_times (sim s), book_ids, and one (books x times) array per gauge
    trades.csv.gz    deduplicated trades from the last --trade-hours: book, time, price, volume, side, maker, taker, fees
    summary.json/.md per class: price, spread, depth, fundamental gap, 90 s moves, trade flow, fee rates, miner share
and checks the published configuration against the local layout files (it matched on 23 October 2026).

Usage (from the repository root):
    python agents/network/collect_real_market.py --network testnet           # 0.6.5, three classes
    python agents/network/collect_real_market.py --network mainnet --hours 12 --trade-hours 2
"""
import argparse
import csv
import gzip
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect_real import NETWORKS, REPO, instant, newest_sim, number, query  # noqa: E402

OUT_DIR = REPO / "agents" / "data" / "real_market"
BOOK_GAUGES = ("mid", "bid", "ask", "bid_vol", "ask_vol", "bid_vol_sum", "ask_vol_sum", "fundamental_price",
               "trade_price", "trade_volume", "trade_buy_volume", "trade_sell_volume", "dynamic_maker_rate",
               "dynamic_taker_rate", "maker_taker_ratio")
TRADE_FIELDS = ("timestamp", "price", "volume", "side", "maker_agent_id", "taker_agent_id", "maker_fee", "taker_fee")
MAX_POINTS = 10_000     # points per series per range query, under the store's limit
# Depth sums are cumulative over levels, so the five-level depth is published at level 4; everything else at 0.
GAUGE_LEVEL = {"bid_vol_sum": "4", "ask_vol_sum": "4"}


def range_chunks(start, end, step):
    """[start, end] split so no range query returns more than MAX_POINTS points per series."""
    span = step * MAX_POINTS
    t = start
    while t < end:
        yield t, min(end, t + span - step)
        t += span


def books_series(sel, start, end, step, book_ids):
    """{gauge: (books x times) array} at the touch (level 0; depth sums over five levels), on a common time grid."""
    times = np.arange(start - start % step, end + 1, step, dtype=np.int64)
    index = {int(t): i for i, t in enumerate(times)}
    row = {b: i for i, b in enumerate(book_ids)}
    out = {}
    for name in BOOK_GAUGES:
        arr = np.full((len(book_ids), len(times)), np.nan)
        for a, b in range_chunks(int(times[0]), int(times[-1]), step):
            level = GAUGE_LEVEL.get(name, "0")
            data = query("query_range", {"query": f'book_gauges{{{sel},book_gauge_name="{name}",level="{level}"}}',
                                         "start": a, "end": b, "step": step}, timeout=180)
            for r in data["result"]:
                book = int(r["metric"]["book_id"])
                if book not in row:
                    continue
                for t, v in r["values"]:
                    i = index.get(int(float(t)))
                    if i is not None:
                        x = number(v)
                        arr[row[book], i] = np.nan if x is None else x
        out[name] = arr
        print(f"  books: {name}", flush=True)
    return times, out


def sim_clock(sel, times, step):
    """Simulation seconds at each wall time (NaN where none was published)."""
    sim = np.full(len(times), np.nan)
    index = {int(t): i for i, t in enumerate(times)}
    for a, b in range_chunks(int(times[0]), int(times[-1]), step):
        data = query("query_range", {"query": f'simulation_gauges{{{sel},simulation_gauge_name="timestamp"}}',
                                     "start": a, "end": b, "step": step})
        for r in data["result"]:
            for t, v in r["values"]:
                i = index.get(int(float(t)))
                x = number(v)
                if i is not None and x is not None:
                    sim[i] = x / 1e9
    return sim


def collect_trades(sel, start, end, step):
    """Unique trades seen in the 25 published slots per book over [start, end]."""
    fields = {}
    for name in TRADE_FIELDS:
        for a, b in range_chunks(start, end, step):
            data = query("query_range", {"query": f'trades{{{sel},trade_gauge_name="{name}"}}',
                                         "start": a, "end": b, "step": step}, timeout=180)
            for r in data["result"]:
                key = (int(r["metric"]["book_id"]), int(r["metric"]["slot"]))
                for t, v in r["values"]:
                    fields.setdefault((key, int(float(t))), {})[name] = number(v)
        print(f"  trades: {name}", flush=True)
    seen, rows = set(), []
    for ((book, _), _), f in fields.items():
        if f.get("timestamp") is None or f.get("price") is None:
            continue
        ident = (book, f["timestamp"], f.get("price"), f.get("volume"), f.get("maker_agent_id"), f.get("taker_agent_id"))
        if ident in seen:
            continue
        seen.add(ident)
        rows.append({"book_id": book, **{k: f.get(k) for k in TRADE_FIELDS}})
    rows.sort(key=lambda r: (r["timestamp"], r["book_id"]))
    return rows


def pct(values, q):
    values = sorted(v for v in values if v == v)
    return values[min(len(values) - 1, int(q * len(values)))] if values else None


def summarize(book_ids, classes, times, sim, g, trades):
    """Per class: the market's shape and who trades in it."""
    summary = {}
    hours = (times[-1] - times[0]) / 3600 if len(times) > 1 else 0
    for name, books in classes.items():
        rows = [i for i, b in enumerate(book_ids) if b in books]
        mid, bid, ask = g["mid"][rows], g["bid"][rows], g["ask"][rows]
        fund = g["fundamental_price"][rows]
        with np.errstate(invalid="ignore", divide="ignore"):
            spread = (ask - bid) / mid * 1e4
            gap = (fund - mid) / mid * 1e4
            move = np.diff(np.log(mid), axis=1) * 1e4
            touch = (g["bid_vol"][rows] + g["ask_vol"][rows]) * mid
            depth = (g["bid_vol_sum"][rows] + g["ask_vol_sum"][rows]) * mid
        # traded volume is a running per-refresh value; its sum over refreshes undercounts between refreshes,
        # so trade flow is taken from the deduplicated trades instead
        t = [r for r in trades if r["book_id"] in books]
        notional = lambda rs: sum((r["price"] or 0) * (r["volume"] or 0) for r in rs)
        background = lambda agent: agent is None or agent < 0
        bg_maker = [r for r in t if background(r["maker_agent_id"])]
        bg_taker = [r for r in t if background(r["taker_agent_id"])]
        miner_vs_miner = [r for r in t if not background(r["maker_agent_id"]) and not background(r["taker_agent_id"])]
        total = notional(t)
        # trade timestamps are simulation seconds, so the rate is per simulation hour
        trade_hours = (t[-1]["timestamp"] - t[0]["timestamp"]) / 3600 if len(t) > 1 else 0
        flat = lambda a: a[np.isfinite(a)].tolist()
        summary[name] = {
            "books": len(rows),
            "price_median": pct(flat(mid), 0.5),
            "spread_bps_median": pct(flat(spread), 0.5), "spread_bps_p90": pct(flat(spread), 0.9),
            "touch_notional_median": pct(flat(touch), 0.5), "depth_notional_median": pct(flat(depth), 0.5),
            "fundamental_gap_bps_mean_abs": float(np.nanmean(np.abs(gap))) if np.isfinite(gap).any() else None,
            "fundamental_gap_bps_p90_abs": pct([abs(x) for x in flat(gap)], 0.9),
            "move_bps_std_per_step": float(np.nanstd(move)) if np.isfinite(move).any() else None,
            "drift_bps_over_window": float(np.nanmedian(np.log(mid[:, -1] / mid[:, 0]) * 1e4)) if mid.shape[1] > 1 else None,
            "dynamic_maker_rate_median": pct(flat(g["dynamic_maker_rate"][rows]), 0.5),
            "dynamic_maker_rate_p90": pct(flat(g["dynamic_maker_rate"][rows]), 0.9),
            "dynamic_taker_rate_median": pct(flat(g["dynamic_taker_rate"][rows]), 0.5),
            "maker_taker_ratio_median": pct(flat(g["maker_taker_ratio"][rows]), 0.5),
            "trades_sampled": len(t),
            "trade_notional_per_book_sim_hour_sampled": total / len(rows) / trade_hours if trade_hours > 0 and rows else None,
            "miner_share_of_maker_notional": 1 - notional(bg_maker) / total if total > 0 else None,
            "miner_share_of_taker_notional": 1 - notional(bg_taker) / total if total > 0 else None,
            "miner_vs_miner_share": notional(miner_vs_miner) / total if total > 0 else None,
        }
    clock = {"wall_hours": hours,
             "sim_seconds": [float(np.nanmin(sim)) if np.isfinite(sim).any() else None,
                             float(np.nanmax(sim)) if np.isfinite(sim).any() else None]}
    return summary, clock


def check_local(config_rows):
    """The published configuration against the local layout files; returns the differences."""
    import xml.etree.ElementTree as ET
    argv, sys.argv = sys.argv, sys.argv[:1]
    try:
        sys.path.insert(0, str(REPO))
        from taos.im.protocol.models import MultiAssetSimulationConfig
    finally:
        sys.argv = argv
    xml = REPO / "simulate" / "trading" / "run" / "config" / "multiasset_simulation_0.xml"
    layout = MultiAssetSimulationConfig.from_multiasset_xml(ET.parse(xml).getroot(), xml.parent)
    local = {}
    for cls in layout.asset_classes():
        flat = {}

        def walk(prefix, x):
            if isinstance(x, dict):
                for k, v in x.items():
                    walk(f"{prefix}_{k}" if prefix else k, v)
            else:
                flat[prefix] = x
        walk("", cls.config.model_dump())
        local[cls.name] = flat
    diffs = []
    for m in config_rows:
        name = m["asset_class"]
        if name not in local:
            diffs.append({"class": name, "key": "*", "real": "class", "local": "missing"})
            continue
        for k, v in m.items():
            key = k[len("simulation_"):] if k.startswith("simulation_") else None
            if key is None or key not in local[name]:
                continue
            lv = local[name][key]
            try:
                same = abs(float(v) - float(lv)) <= 1e-6 * max(1.0, abs(float(v)))
            except (TypeError, ValueError):
                same = str(v) == str(lv)
            if not same:
                diffs.append({"class": name, "key": key, "real": v, "local": lv})
    return diffs


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", choices=list(NETWORKS), default="testnet")
    parser.add_argument("--hours", type=float, default=None, help="Book history window (default: the whole simulation).")
    parser.add_argument("--step", type=int, default=90, help="Seconds between book samples (the publish cadence).")
    parser.add_argument("--trade-hours", type=float, default=3.0, help="Window of the trade sample.")
    args = parser.parse_args()

    netuid = NETWORKS[args.network]
    hotkey, sim_id = newest_sim(netuid)
    sel = f'netuid="{netuid}",hotkey="{hotkey}",sim_id="{sim_id}"'
    print(f"{args.network}: validator {hotkey[:8]}, simulation {sim_id}", flush=True)
    config_rows = [r["metric"] for r in instant(f"simulation_config_info{{{sel}}}")]
    classes = {}
    for r in instant(f"book_asset_class{{{sel}}}"):
        classes.setdefault(r["metric"]["asset_class"], set()).add(int(r["metric"]["book_id"]))
    if not classes:
        for m in config_rows:
            lo, hi = (int(x) for x in m["books"].split("-"))
            classes[m["asset_class"]] = set(range(lo, hi + 1))
    book_ids = sorted(b for books in classes.values() for b in books)

    end = int(time.time())
    if args.hours:
        start = end - int(args.hours * 3600)
    else:
        first = instant(f'min_over_time(timestamp(simulation_gauges{{{sel},simulation_gauge_name="timestamp"}})[7d:{args.step}s])')
        start = int(float(first[0]["value"][1])) if first else end - 3 * 3600
    print(f"  window {time.strftime('%m-%d %H:%M', time.gmtime(start))} .. {time.strftime('%m-%d %H:%M', time.gmtime(end))} UTC, "
          f"{len(book_ids)} books", flush=True)
    times, gauges = books_series(sel, start, end, args.step, book_ids)
    sim = sim_clock(sel, times, args.step)
    trades = collect_trades(sel, max(start, end - int(args.trade_hours * 3600)), end, args.step)
    summary, clock = summarize(book_ids, classes, times, sim, gauges, trades)
    diffs = check_local(config_rows)

    out = OUT_DIR / f"{args.network}_{sim_id}"
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out / "books.npz", times=times, sim_times=sim, book_ids=np.array(book_ids), **gauges)
    with gzip.open(out / "trades.csv.gz", "wt", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["book_id", *TRADE_FIELDS])
        writer.writeheader()
        writer.writerows(trades)
    meta = {"network": args.network, "netuid": int(netuid), "validator_hotkey": hotkey, "sim_id": sim_id,
            "collected_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(end)), "step_s": args.step,
            "wall_start": int(times[0]), "wall_end": int(times[-1]), **clock,
            "classes": {k: sorted(v) for k, v in classes.items()}, "config": config_rows,
            "local_config_differences": diffs}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    keys = list(next(iter(summary.values())).keys())
    lines = [f"# Real market: {args.network} (netuid {netuid}), simulation {sim_id}", "",
             f"Collected {meta['collected_at']} from the validator {hotkey[:8]}'s published metrics; "
             f"{clock['wall_hours']:.1f} wall hours sampled every {args.step} s, sim time "
             f"{(clock['sim_seconds'][0] or 0) / 3600:.1f}-{(clock['sim_seconds'][1] or 0) / 3600:.1f} h; "
             f"{len(trades)} trades sampled over the last {args.trade_hours:g} h.", "",
             "Local layout configuration: " + ("identical to the published one." if not diffs else
                                                 f"{len(diffs)} differences (see meta.json)."), "",
             "| Metric | " + " | ".join(summary) + " |", "|---|" + "---|" * len(summary)]
    for k in keys:
        cells = []
        for name in summary:
            v = summary[name][k]
            cells.append("–" if v is None else (f"{v:.4g}" if isinstance(v, float) else str(v)))
        lines.append(f"| {k} | " + " | ".join(cells) + " |")
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"  wrote {out.relative_to(REPO)}: {len(times)} samples x {len(book_ids)} books, {len(trades)} trades, "
          f"config differences {len(diffs)}", flush=True)
    print("\n".join(lines[6:]))


if __name__ == "__main__":
    main()
