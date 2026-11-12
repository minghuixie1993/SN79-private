# SPDX-License-Identifier: MIT
"""
Tracks one registered miner on mainnet (netuid 79) or testnet (366): what the chain says and what the validators
publish about it, appended as one JSON line per snapshot.

Chain (the consensus that pays): uid, incentive and its rank among all uids, emission, consensus, stake, its place
by emission from the bottom of the uids past immunity (the lowest is replaced next), blocks since registration, immunity left,
whether the axon is served, and the weight each active validator
gives the uid (its share of that validator's weights) with the validator's stake.
Published (taos.simulate.trading, the validators that push metrics; see agents/network/collect_real.py): score,
de-beta trading score and its rank in the field, making, markout legs, S3, P11, skill, net alpha, books, notional,
P&L, the miner's call time, timeouts, failures, orders placed and refused, and the same legs per asset class.

Writes agents/data/real_track/<network>_<hotkey prefix>.jsonl and prints a one-line summary per snapshot.

Usage (from the repository root):
    python agents/mainnet/track_uid.py --wallet redpig-coldkey --wallet-hotkey redpig-miner          # once
    python agents/mainnet/track_uid.py --hotkey 5CMTnm7J... --network testnet --every 30              # every 30 min
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "agents" / "network"))
from collect_real import NETWORKS, instant, number  # noqa: E402

OUT_DIR = REPO / "agents" / "data" / "real_track"
ENDPOINTS = {"mainnet": "finney", "testnet": "test"}
PUBLISHED = ("score", "debeta_score", "debeta_making", "debeta_making_captured", "debeta_making_realized",
             "debeta_making_share", "debeta_s3_factor", "debeta_p11_factor", "debeta_skill", "debeta_skill_net_alpha",
             "debeta_skill_books_kept", "debeta_coverage_books", "debeta_notional", "pnl", "call_time",
             "call_time_median", "timeouts", "failures", "placed", "refused", "refused_contract", "rejections",
             "fills", "requests", "incentive")


def wallet_hotkey(wallet, hotkey):
    path = Path.home() / ".bittensor" / "wallets" / wallet / "hotkeys" / f"{hotkey}pub.txt"
    return json.loads(path.read_text())["ss58Address"]


def _burn(st, netuid):
    """The subnet's registration cost in TAO now (None if the query fails), for a re-registration after pruning."""
    try:
        return float(st.recycle(netuid))
    except Exception:
        return None


def chain_snapshot(network, hotkey):
    import bittensor as bt
    bt.logging.set_warning()
    st = bt.Subtensor(network=ENDPOINTS[network])
    netuid = int(NETWORKS[network])
    block = st.get_current_block()
    m = st.metagraph(netuid, lite=False)
    if hotkey not in m.hotkeys:
        return {"block": block, "registered": False}
    uid = m.hotkeys.index(hotkey)
    registered_at = int(m.block_at_registration[uid])
    immunity = int(getattr(st.substrate.query("SubtensorModule", "ImmunityPeriod", [netuid]), "value", 0))
    incentive = [float(x) for x in m.I]
    emission = [float(x) for x in m.E]
    # a new registration replaces the uid with the lowest emission among those past immunity
    exposed = [u for u in range(m.n) if block - int(m.block_at_registration[u]) > immunity]
    weights = []
    for v in range(m.n):
        if not m.validator_permit[v] or float(m.S[v]) <= 0 or block - int(m.last_update[v]) > 2 * 360 * 10:
            continue
        row = m.W[v]
        total = float(row.sum())
        weights.append({"uid": v, "hotkey": m.hotkeys[v], "stake": float(m.S[v]),
                        "share": float(row[uid]) / total if total > 0 else 0.0,
                        "updated_blocks_ago": block - int(m.last_update[v])})
    return {"block": block, "registered": True, "uid": uid, "incentive": incentive[uid],
            "incentive_rank": 1 + sum(1 for x in incentive if x > incentive[uid]), "n": int(m.n),
            "emission": emission[uid], "consensus": float(m.C[uid]), "stake": float(m.S[uid]),
            "pruning_rank_from_bottom": 1 + sum(1 for u in exposed if emission[u] < emission[uid]) if uid in exposed else None,
            "exposed_uids": len(exposed), "blocks_since_registration": block - registered_at,
            # while still immune: where the uid would stand if exposed now, and the floor it has to clear
            "rank_if_exposed": 1 + sum(1 for u in exposed if u != uid and emission[u] < emission[uid]),
            "lowest_exposed_emission": min((emission[u] for u in exposed if u != uid), default=None),
            "burn": _burn(st, netuid),
            "immunity_left_blocks": max(0, immunity - (block - registered_at)), "axon": str(m.axons[uid].ip),
            "axon_served": m.axons[uid].ip != "0.0.0.0",
            "validator_weights": sorted(weights, key=lambda w: -w["stake"])}


def published_snapshot(network, uid):
    """Per publishing validator: the uid's newest gauges, its trading-score rank in that field, and its class legs."""
    netuid = NETWORKS[network]
    out = {}
    newest = instant(f'max by (hotkey, sim_id) (timestamp(miner_gauges{{netuid="{netuid}",miner_gauge_name="score"}}))')
    for row in newest:
        hotkey, sim_id = row["metric"]["hotkey"], row["metric"]["sim_id"]
        if any(r["metric"]["hotkey"] == hotkey and float(r["value"][1]) > float(row["value"][1]) for r in newest):
            continue        # an older simulation of the same validator
        sel = f'netuid="{netuid}",hotkey="{hotkey}",sim_id="{sim_id}"'
        names = "|".join(PUBLISHED)
        mine = {r["metric"]["miner_gauge_name"]: number(r["value"][1])
                for r in instant(f'miner_gauges{{{sel},agent_id="{uid}",miner_gauge_name=~"{names}"}}')}
        field = [number(r["value"][1]) or 0.0
                 for r in instant(f'miner_gauges{{{sel},miner_gauge_name="debeta_score"}}')]
        classes = {}
        for r in instant(f'agent_class_gauges{{{sel},agent_id="{uid}"}}'):
            classes.setdefault(r["metric"]["asset_class"], {})[r["metric"]["agent_class_gauge_name"]] = number(r["value"][1])
        mine_score = mine.get("debeta_score") or 0.0
        out[hotkey] = {"sim_id": sim_id, "gauges": mine, "classes": classes,
                       "trading_rank": 1 + sum(1 for x in field if x > mine_score), "field": len(field)}
    return out


def snapshot(network, hotkey):
    snap = {"at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "network": network, "hotkey": hotkey}
    try:
        snap["chain"] = chain_snapshot(network, hotkey)
    except Exception as ex:
        snap["chain"] = {"error": str(ex)}
    uid = snap["chain"].get("uid")
    if uid is not None:
        try:
            snap["published"] = published_snapshot(network, uid)
        except Exception as ex:
            snap["published"] = {"error": str(ex)}
    return snap


def summary(snap):
    c = snap["chain"]
    if "error" in c:
        return f"{snap['at']} chain error: {c['error']}"
    if not c.get("registered"):
        return f"{snap['at']} block {c['block']}: {snap['hotkey'][:8]} is not registered on {snap['network']}"
    weighted = [w for w in c["validator_weights"] if w["share"] > 0]
    line = (f"{snap['at']} uid {c['uid']}: incentive {c['incentive']:.5f} (rank {c['incentive_rank']}/{c['n']}), "
            f"weighted by {len(weighted)}/{len(c['validator_weights'])} active validators, "
            f"immunity left {c['immunity_left_blocks'] * 12 / 3600:.1f} h"
            + (f", emission {c['emission']:.4f} vs lowest exposed {c['lowest_exposed_emission']:.4f} (rank if exposed "
               f"{c.get('rank_if_exposed')})" if c.get("lowest_exposed_emission") is not None else "")
            + (f", burn {c['burn']:.4f} TAO" if c.get("burn") is not None else "")
            + (f", pruning rank {c['pruning_rank_from_bottom']} from the bottom of {c['exposed_uids']}"
               if c.get("pruning_rank_from_bottom") else "")
            + f", axon {'served' if c['axon_served'] else 'NOT served'}")
    for hotkey, p in (snap.get("published") or {}).items():
        if not isinstance(p, dict) or "gauges" not in p:
            continue
        g = p["gauges"]
        fmt = lambda k, spec="{:.3f}": spec.format(g[k]) if g.get(k) is not None else "-"
        line += (f"\n    published by {hotkey[:8]} (sim {p['sim_id']}): trading {fmt('debeta_score')} "
                 f"(rank {p['trading_rank']}/{p['field']}), making {fmt('debeta_making', '{:.1f}')}, "
                 f"S3 {fmt('debeta_s3_factor', '{:.2f}')}, skill {fmt('debeta_skill')}, "
                 f"call {fmt('call_time', '{:.2f}')} s, timeouts {fmt('timeouts', '{:.0f}')}, "
                 f"refused {fmt('refused', '{:.0f}')}/{fmt('placed', '{:.0f}')}")
    return line


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", choices=list(NETWORKS), default="mainnet")
    parser.add_argument("--hotkey", help="The miner's hotkey ss58 address.")
    parser.add_argument("--wallet", default="redpig-coldkey", help="Wallet name, when --hotkey is not given.")
    parser.add_argument("--wallet-hotkey", default="redpig-miner", help="Hotkey name in the wallet.")
    parser.add_argument("--every", type=float, default=0, help="Minutes between snapshots; 0 takes one and exits.")
    args = parser.parse_args()
    hotkey = args.hotkey or wallet_hotkey(args.wallet, args.wallet_hotkey)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{args.network}_{hotkey[:8]}.jsonl"
    while True:
        snap = snapshot(args.network, hotkey)
        with open(out, "a") as fh:
            fh.write(json.dumps(snap) + "\n")
        print(summary(snap), flush=True)
        if args.every <= 0:
            break
        time.sleep(args.every * 60)


if __name__ == "__main__":
    main()
