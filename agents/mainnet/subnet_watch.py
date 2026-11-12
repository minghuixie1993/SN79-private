# SPDX-License-Identifier: MIT
"""
Watches finney for newly registered subnets and for registration burns the coldkey can pay.

All 128 subnet slots are full, so a new subnet takes over the netuid of a deregistered one: a new subnet shows up as a
changed NetworkRegisteredAt, not as a new netuid. Only one subnet can register per NetworkRateLimit (14400 blocks,
48 h); in 2026 a new one appeared roughly once a week (2-29 days apart), at a lock cost of a few hundred TAO.

Every poll checks the neuron registration burn of --netuids (default 79) and of every subnet younger than --young-days
against the coldkey's free balance. A netuid is affordable when registration is allowed, the hotkey is not already
registered there, and free balance >= burn + fee + --fee-margin, the fee being the chain's payment_info for this
coldkey's burned_register (0.00107 TAO on 26 Oct, more than the gap to a near-equal burn). Lines the log carries:
    STATUS ...                       every poll: balance and what each watched netuid needs (burn + fee + margin)
    AFFORDABLE netuid N ...          a netuid just became affordable
    NO LONGER AFFORDABLE netuid N    it stopped being affordable
    NEW SUBNET netuid N ...          a subnet was registered (also logged to agents/data/subnet_watch.jsonl)
    poll failed: ...                 a network error; the next poll reconnects
Read-only: it never registers anything.

Usage (from the repository root):
    python agents/mainnet/subnet_watch.py --once              # one poll, then the newest subnets
    python agents/mainnet/subnet_watch.py --interval 300      # keep watching
"""
import argparse
import json
import time
from pathlib import Path

import bittensor as bt
from bittensor_wallet import Keypair

REPO = Path(__file__).resolve().parents[2]
LOG = REPO / "agents" / "data" / "subnet_watch.jsonl"
WALLETS = Path.home() / ".bittensor" / "wallets"
BLOCK_S = 12


def ss58(path):
    return json.loads(path.read_text())["ss58Address"]


def snapshot(st, netuid, now):
    sub = st.substrate
    q = lambda name: sub.query("SubtensorModule", name, [netuid]).value
    reg = q("NetworkRegisteredAt")
    try:
        ident = sub.query("SubtensorModule", "SubnetIdentitiesV3", [netuid]).value or {}
    except Exception:
        ident = {}
    try:
        first = q("FirstEmissionBlockNumber")
    except Exception:
        first = None
    return {"at": time.time(), "block": now, "netuid": netuid, "registered_at": reg,
            "age_h": round((now - reg) * BLOCK_S / 3600, 2),
            "name": ident.get("subnet_name", "") if isinstance(ident, dict) else "",
            "uids": q("SubnetworkN"), "max_uids": q("MaxAllowedUids"),
            "registration_allowed": q("NetworkRegistrationAllowed"), "burn_tao": q("Burn") / 1e9,
            "emission_started_at": first}


def registered_at(st):
    return {n: st.substrate.query("SubtensorModule", "NetworkRegisteredAt", [n]).value
            for n in st.get_all_subnets_netuid() if n != 0}


def stamp():
    return time.strftime("%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="finney")
    ap.add_argument("--interval", type=float, default=300, help="seconds between polls")
    ap.add_argument("--wallet", default="redpig-coldkey")
    ap.add_argument("--hotkey", default="redpig-miner")
    ap.add_argument("--netuids", default="79", help="comma-separated netuids whose burn is always checked")
    ap.add_argument("--young-days", type=float, default=14, help="also check subnets registered this recently")
    ap.add_argument("--fee-margin", type=float, default=0.0001, help="TAO kept on top of the burn and the fee")
    ap.add_argument("--follow-hours", type=float, default=24, help="log a new subnet's snapshot this long")
    ap.add_argument("--newest", type=int, default=5, help="--once: how many of the newest subnets to print")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    coldkey = ss58(WALLETS / args.wallet / "coldkeypub.txt")
    hotkey = ss58(WALLETS / args.wallet / "hotkeys" / args.hotkey)
    fixed = [int(n) for n in args.netuids.split(",") if n.strip()]
    st, known, affordable, follow = None, None, set(), {}

    while True:
        try:
            if st is None:
                st = bt.Subtensor(args.network)
            current = registered_at(st)
            now = st.get_current_block()
            if known is not None:
                for n, reg in current.items():
                    if known.get(n) != reg:
                        follow[n] = reg
                        print("%s NEW SUBNET netuid %d registered at block %d" % (stamp(), n, reg), flush=True)
            known = current
            for n, reg in list(follow.items()):
                if (now - reg) * BLOCK_S / 3600 > args.follow_hours:
                    del follow[n]
                    continue
                with LOG.open("a") as f:
                    f.write(json.dumps(snapshot(st, n, now)) + "\n")

            balance = float(st.get_balance(coldkey))
            young = [n for n, reg in current.items() if (now - reg) * BLOCK_S / 86400 < args.young_days]
            parts, ok = [], set()
            for n in dict.fromkeys(fixed + sorted(young, key=current.get, reverse=True)):
                q = lambda name: st.substrate.query("SubtensorModule", name, [n]).value
                burn = q("Burn") / 1e9
                mine = st.is_hotkey_registered(hotkey, n)
                call = st.substrate.compose_call("SubtensorModule", "burned_register", {"netuid": n, "hotkey": hotkey})
                fee = st.substrate.get_payment_info(call, Keypair(ss58_address=coldkey))["partial_fee"] / 1e9
                need = burn + fee + args.fee_margin
                if q("NetworkRegistrationAllowed") and not mine and balance >= need:
                    ok.add(n)
                    if n not in affordable:
                        print("%s AFFORDABLE netuid %d burn %.6f + fee %.6f TAO, balance %.6f TAO (age %.1f d)" % (
                            stamp(), n, burn, fee, balance, (now - current[n]) * BLOCK_S / 86400), flush=True)
                parts.append("sn%d %.6f%s" % (n, need, " (registered)" if mine else ""))
            for n in affordable - ok:
                print("%s NO LONGER AFFORDABLE netuid %d" % (stamp(), n), flush=True)
            affordable = ok
            print("%s STATUS balance %.6f | need %s" % (stamp(), balance, " | ".join(parts)), flush=True)
        except Exception as e:                          # a dropped websocket or DNS failure: reconnect next poll
            print("%s poll failed: %s %s" % (stamp(), type(e).__name__, e), flush=True)
            st = None
        if args.once:
            if known:
                now = st.get_current_block()
                for n in sorted(known, key=known.get)[-args.newest:]:
                    print(json.dumps(snapshot(st, n, now)))
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
