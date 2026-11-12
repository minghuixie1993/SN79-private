# SPDX-License-Identifier: MIT
"""
Collects the real validators' published scores (mainnet netuid 79, testnet netuid 366) into the same files a local
run_network.py run produces, so agents/dashboard shows them beside local runs and they compare field for field.

The subnet's validators push their per-miner metrics to the public Grafana at taos.simulate.trading (the same
VictoriaMetrics store also backs testnet.simulate.trading): `miner_gauges` (score, de-beta legs, volumes, call
times, ...), `agent_class_gauges` (the de-beta legs per asset class), `agent_gauges` (per book) and `miner_identity`
(coldkey, hotkey, axon). This reads them through Grafana's datasource proxy, the way the dashboard pages do; it
starts no simulation and replays nothing.

For each network it takes the newest simulation, writes agents/data/network/real_<network>_<sim id>/ with
network.json and <validator>/score/{leaderboard.json, timelines.json}, and overwrites that folder on every
collection. Field names follow agents/eval/score_run.py: `trading_score` is the validator's `debeta_score`, `score`
its weight input after the track-record EMA, and the run averages are taken over the last --hours of wall time
(the validator's own window is 3 sim hours). Time charts run in wall minutes from the start of that window.

Usage (from the repository root):
    python agents/network/collect_real.py                    # testnet and mainnet, last 3 h, 1-minute steps
    python agents/network/collect_real.py --network testnet --hours 6 --books
    python agents/network/collect_real.py --network mainnet --track <your uid or hotkey>    # always kept, highlighted
"""
import argparse
import json
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO / "agents" / "data" / "network"
GRAFANA = "https://taos.simulate.trading"
DATASOURCE = "c7665619-8b04-4227-9882-a44e8dad57a6"
NETWORKS = {"mainnet": "79", "testnet": "366"}

# score_run.py's per-miner keys <- the validators' miner_gauge_name
MINER_FIELDS = {
    "score": "score", "trading_score": "debeta_score", "debeta_score": "debeta_score",
    "making_raw": "debeta_making", "making_captured": "debeta_making_captured",
    "making_realized": "debeta_making_realized", "making_share": "debeta_making_share",
    "making_rank": "debeta_making_rank", "skill_raw": "debeta_skill", "skill_rank": "debeta_skill_rank",
    "skill_net_alpha": "debeta_skill_net_alpha", "skill_books_kept": "debeta_skill_books_kept",
    "coverage_books": "debeta_coverage_books", "p11_factor": "debeta_p11_factor", "s3_factor": "debeta_s3_factor",
    "bg_share": "debeta_bg_share", "notional": "debeta_notional", "pnl": "pnl",
    "num_scored_books": "num_scored_books", "activity_factor": "activity_factor",
}
# Also kept, under their own names: what the network adds that a local replay cannot know.
EXTRA_FIELDS = ("incentive", "emission", "consensus", "call_time", "call_time_median", "timeouts", "failures",
                "placed", "refused", "refused_contract", "rejections", "fills", "requests", "success",
                "total_daily_volume", "total_daily_maker_volume", "total_daily_taker_volume")
# score_run.py's per-class debeta detail keys <- agent_class_gauge_name
CLASS_FIELDS = {"making_raw": "making", "skill_raw": "skill", "skill_books": "skill_books",
                "coverage_books": "coverage_books", "making_s3_factor": "s3_factor", "p11_factor": "p11_factor",
                "skill_net_alpha": "net_alpha", "notional": "notional", "bg_share": "bg_share", "present": "present",
                "making_rank": "making_rank", "skill_rank": "skill_rank"}
# Books per class on the published default layouts (multiasset_simulation_0.xml): 0.6.4 had two classes, 0.6.5 three.
LAYOUT_BOOKS = {2: [96, 32], 3: [64, 32, 32]}
# Timeline columns, as score_run.py writes them (timelines.json "keys" after "timestamp").
TIMELINE_KEYS = ("score", "trading_score", "making_raw", "making_captured", "making_realized", "making_share",
                 "skill_raw", "skill_net_alpha", "skill_books_kept", "coverage_books", "p11_factor", "s3_factor",
                 "notional", "pnl")
BOOK_FIELDS = ("daily_maker_volume", "daily_taker_volume", "daily_volume", "realized_pnl")


def query(path, params, timeout=90):
    """One Prometheus API call through Grafana's datasource proxy; returns `data`."""
    url = f"{GRAFANA}/api/datasources/proxy/uid/{DATASOURCE}/api/v1/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sn79-collect-real"}),
                                timeout=timeout) as r:
        body = json.load(r)
    if body.get("status") != "success":
        raise RuntimeError(f"{path} {params}: {body}")
    return body["data"]


def instant(expr):
    return query("query", {"query": expr})["result"]


def number(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def newest_sim(netuid):
    """(validator hotkey, sim id) of the simulation that published last on this netuid."""
    rows = instant(f'max by (hotkey, sim_id) (timestamp(miner_gauges{{netuid="{netuid}",miner_gauge_name="score"}}))')
    if not rows:
        raise RuntimeError(f"no published scores for netuid {netuid}")
    best = max(rows, key=lambda r: float(r["value"][1]))
    return best["metric"]["hotkey"], best["metric"]["sim_id"]


def collect(network, hours, step, books, include_idle, track=()):
    netuid = NETWORKS[network]
    hotkey, sim_id = newest_sim(netuid)
    sel = f'netuid="{netuid}",hotkey="{hotkey}",sim_id="{sim_id}"'
    print(f"{network}: validator {hotkey[:8]}, simulation {sim_id}", flush=True)

    identity = {}
    for r in instant(f"miner_identity{{{sel}}}"):
        m = r["metric"]
        identity[int(m["agent_id"])] = {"coldkey": m.get("coldkey"), "hotkey": m.get("exported_hotkey"),
                                        "axon": f"{m.get('axon_ip')}:{m.get('axon_port')}"}
    gauges = {}
    for r in instant(f"miner_gauges{{{sel}}}"):
        gauges.setdefault(int(r["metric"]["agent_id"]), {})[r["metric"]["miner_gauge_name"]] = number(r["value"][1])
    classes = {}
    for r in instant(f"agent_class_gauges{{{sel}}}"):
        m = r["metric"]
        classes.setdefault(int(m["agent_id"]), {}).setdefault(m["asset_class"], {})[m["agent_class_gauge_name"]] = number(r["value"][1])
    class_names = sorted({c for per in classes.values() for c in per})
    class_books = dict(zip(class_names, LAYOUT_BOOKS.get(len(class_names), [None] * len(class_names))))

    def active(uid):
        # notional filled inside the validator's window: the EMA'd score and daily volume linger on idle uids
        g = gauges.get(uid, {})
        return (g.get("debeta_notional") or 0) > 0 or (g.get("debeta_making") or 0) > 0
    # tracked miners (uid or hotkey) are always kept, and named MyAgent_* so the dashboard highlights them
    tracked = set()
    for entry in track:
        entry = str(entry).strip()
        if entry.isdigit():
            tracked.add(int(entry))
        else:
            tracked.update(u for u, i in identity.items() if (i.get("hotkey") or "").startswith(entry))
    for entry in track:
        if not any(str(entry) == str(u) or (identity.get(u, {}).get("hotkey") or "").startswith(str(entry)) for u in tracked):
            print(f"  tracked {entry}: not registered on {network}", flush=True)
    uids = sorted(u for u in gauges if u != 0 and (include_idle or active(u) or u in tracked))
    print(f"  {len(gauges)} uids published, {len(uids)} kept ({'all' if include_idle else 'active'}"
          f"{f', tracking {sorted(tracked)}' if tracked else ''}), classes {class_names or '-'}", flush=True)

    def agent_name(uid, identity):
        name = default_agent_name(uid, identity)
        return f"MyAgent_{name}" if uid in tracked else name

    # time series of the timeline fields, for the kept uids only
    end = int(time.time())
    start = end - int(hours * 3600)
    names = sorted({MINER_FIELDS[k] for k in TIMELINE_KEYS})
    series = {}
    if uids:
        data = query("query_range", {
            "query": f'miner_gauges{{{sel},miner_gauge_name=~"{"|".join(names)}",agent_id=~"{"|".join(map(str, uids))}"}}',
            "start": start, "end": end, "step": step})
        for r in data["result"]:
            series.setdefault(int(r["metric"]["agent_id"]), {})[r["metric"]["miner_gauge_name"]] = {
                int(t): number(v) for t, v in r["values"]}
    stamps = sorted({t for per in series.values() for s in per.values() for t in s})

    per_book = {}
    if books and uids:
        for r in instant(f'agent_gauges{{{sel},agent_gauge_name=~"{"|".join(BOOK_FIELDS)}",agent_id=~"{"|".join(map(str, uids))}"}}'):
            m = r["metric"]
            per_book.setdefault(int(m["agent_id"]), {}).setdefault(m["agent_gauge_name"], {})[str(int(m["book_id"]))] = number(r["value"][1])

    board, timelines = [], {}
    for uid in uids:
        g = gauges.get(uid, {})
        final = {k: g.get(v) for k, v in MINER_FIELDS.items()}
        final.update({k: g.get(k) for k in EXTRA_FIELDS})
        cls = {}
        for name in class_names:
            c = classes.get(uid, {}).get(name)
            cls[name] = {"books": class_books.get(name), "books_traded": None, "books_positive_realized_pnl": None,
                         "volume": None,
                         "debeta": None if c is None else {k: c.get(v) for k, v in CLASS_FIELDS.items()}}
        own = series.get(uid, {})
        rows = []
        for t in stamps:
            rows.append([(t - start) * 1_000_000_000,
                         *[own.get(MINER_FIELDS[k], {}).get(t) for k in TIMELINE_KEYS]])
        timelines[str(uid)] = rows

        def mean(key):
            values = [v for v in own.get(MINER_FIELDS[key], {}).values() if v is not None]
            return statistics.mean(values) if values else final.get(key)
        entry = {"uid": uid, "agent": agent_name(uid, identity), "final": final, "debeta_detail": None,
                 "classes": cls, "average": {k: mean(k) for k in ("score", "trading_score", "making_raw", "skill_raw")}
                 | {"debeta_score": mean("trading_score")},
                 "boundaries": len(rows)}
        if uid in per_book:
            b = per_book[uid]
            entry["per_book"] = {
                "volume": {book: {"maker": b.get("daily_maker_volume", {}).get(book) or 0.0,
                                  "taker": b.get("daily_taker_volume", {}).get(book) or 0.0,
                                  "total": b.get("daily_volume", {}).get(book) or 0.0}
                           for book in b.get("daily_volume", {})},
                "realized_pnl": b.get("realized_pnl", {})}
        board.append(entry)

    run = RUNS_DIR / f"real_{network}_{sim_id}"
    validator = f"{network}_{hotkey[:8]}"
    out = run / validator / "score"
    out.mkdir(parents=True, exist_ok=True)
    window = f"last {hours:g} h (wall)"
    (out / "leaderboard.json").write_text(json.dumps({"run": run.name, "window_after_grace": window,
                                                      "simulation_timestamp": None, "agents": board}, indent=2))
    (out / "timelines.json").write_text(json.dumps({"run": run.name, "grace_period": 0,
                                                    "keys": ["timestamp", *TIMELINE_KEYS], "agents": timelines}))
    network_json = {
        "config": {"name": run.name, "source": f"{GRAFANA} (validator-published metrics)", "network": network,
                   "netuid": int(netuid), "validator_hotkey": hotkey, "sim_id": sim_id,
                   "collected_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(end)), "hours": hours,
                   "step_s": step},
        "minutes": hours * 60,
        "simulation_xml": f"real {network} (netuid {netuid}), sim {sim_id}",
        "agents": [{"uid": u, "file": agent_name(u, identity), "params": identity.get(u, {}), "port": None}
                   for u in uids],
        "validators": [{"name": validator, "seed": None, "port": None}],
    }
    (run / "network.json").write_text(json.dumps(network_json, indent=2))
    print(f"  wrote {run.relative_to(REPO)} ({len(board)} agents, {len(stamps)} time points"
          f"{', per-book detail' if per_book else ''})", flush=True)
    return run


def default_agent_name(uid, identity):
    """uid and the first characters of its coldkey, which groups the UIDs one team runs."""
    coldkey = (identity.get(uid) or {}).get("coldkey") or "?"
    return f"uid{uid}_{coldkey[:8]}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", choices=["both", *NETWORKS], default="both")
    parser.add_argument("--hours", type=float, default=3.0, help="Window of the time series and the averages.")
    parser.add_argument("--step", type=int, default=60, help="Seconds between time-series points.")
    parser.add_argument("--books", action="store_true", help="Also fetch per-book volume and realized P&L.")
    parser.add_argument("--all", action="store_true", help="Keep idle uids too (no score, making or notional).")
    parser.add_argument("--track", action="append", default=[],
                        help="A uid or hotkey (prefix) to always keep and highlight as MyAgent_*; repeatable.")
    args = parser.parse_args()
    for network in (NETWORKS if args.network == "both" else [args.network]):
        collect(network, args.hours, args.step, args.books, args.all, args.track)


if __name__ == "__main__":
    main()
