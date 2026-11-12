#!/bin/bash
# Starts a SECOND miner (UID-B) next to the live one (29 Oct, shield_grid_log free-time #179).
#     bash agents/mainnet/start_uid_b.sh --dry-run      # print the pm2 command only
#     bash agents/mainnet/start_uid_b.sh                # start / restart pm2 process 'miner2'
# Env overrides: HOTKEY (redpig-miner2), PORT (8092), NAME (miner2), AGENT (MyAgent10_27_2Gpt), EXTRA (k=v ... appended,
# a key already present is replaced).
# UID-B is DIFFERENTIATED from UID 229 (the making pool pays a clone nothing extra, reward.py:1138-1152): c0 quotes one
# level deeper (half_spread_c0 5 bps vs 4) so the two do not queue for the same fills, and c2 is off (its negative
# realized drags the uid-wide quality q, #175). No recorder here - UID 229's recorder already captures the market.
# The stock miner (taos/im/neurons/miner.py) is started with the live miner's environment and arguments, changed only
# in hotkey, axon port and agent params. The axon port must be reachable from the validator (check the firewall /
# cloud security group for PORT; 8091 is the live one).
set -euo pipefail
cd "$(dirname "$0")/../.."
export PM2_HOME=${PM2_HOME:-$HOME/.pm2-trustvs}
HOTKEY=${HOTKEY:-redpig-miner2}
PORT=${PORT:-8092}
NAME=${NAME:-miner2}
AGENT=${AGENT:-MyAgent10_27_2Gpt}
PARAMS="event_lookback_minutes=1 latency_s=1.05 shield_pre_s=3 shield_post_s=1 shield_post_s_c1=5 shield_post_s_c2=5 \
quiet_size_mult_c0=2.5 quiet_size_mult_c1=1.25 quiet_size_mult_c2=1.6 skew_c1=8 max_inv_ratio_c1=0.06 half_spread_c0=0.0005 \
size_fraction_c0=0.03 size_fraction_c1=0.024 size_fraction_c2=0.024 max_order_fraction=0.075 coef_fund_c2=0.004 pace_burst=8 \
adapt_max=1.0 max_lean=0.75 pending_aware=1 fill_skew_c0=0.6 fill_skew_c2=0.6 shield_post_s_c0=5 balance_basis=1 \
inv_target_k_c2=0.001 inv_target_hold_s_c2=600 fund_demean_s_c0=600 coef_fund_dm_c0=0.07 book_mult_max=1.0 \
fund_demean_s_c2=3600 coef_fund_dm_c2=0.003 quote_off_c2=1"
for kv in ${EXTRA:-}; do
    key="${kv%%=*}"
    PARAMS="$(echo " $PARAMS " | sed -E "s/ ${key}=[^ ]+ / /") $kv"
done
PARAMS="$(echo $PARAMS)"
[ -f "$HOME/.bittensor/wallets/redpig-coldkey/hotkeys/$HOTKEY" ] || { echo "hotkey '$HOTKEY' not found - create and register it first"; exit 1; }
# a restart of our own process frees its port first (29 Oct: the check refused every restart of a running miner2)
if [ "${1:-}" != "--dry-run" ] && pm2 describe "$NAME" > /dev/null 2>&1; then
    pm2 stop "$NAME" > /dev/null
    for _ in $(seq 1 30); do ss -ltn | grep -q ":$PORT " || break; sleep 1; done
fi
if [ "${1:-}" != "--dry-run" ] && ss -ltn | grep -q ":$PORT "; then echo "port $PORT already in use"; exit 1; fi
python3 - "$NAME" "$HOTKEY" "$PORT" "$AGENT" "$PARAMS" "${1:-}" <<'EOF'
import json, os, subprocess, sys
name, hotkey, port, agent, params, flag = sys.argv[1:7]
live = next(p for p in json.loads(subprocess.run(["pm2", "jlist"], capture_output=True, text=True).stdout) if p["name"] == "miner")
env = live["pm2_env"]
args = list(env.get("args") or [])
def setopt(a, opt, val):
    if opt in a:
        a[a.index(opt) + 1] = val
    else:
        a += [opt, val]
setopt(args, "--wallet.hotkey", hotkey)
setopt(args, "--axon.port", port)
setopt(args, "--agent.name", agent)
i = args.index("--agent.params")
j = i + 1
while j < len(args) and not args[j].startswith("--"):
    j += 1
args[i + 1:j] = params.split()
stock = os.path.join(os.getcwd(), "taos", "im", "neurons", "miner.py")
cmd = ["pm2", "start", stock, "--name", name, "--interpreter", env.get("exec_interpreter") or "python",
       "--cwd", env.get("pm_cwd") or os.path.dirname(stock), "--update-env", "--", *args]
print("pm2 process:", name, "| hotkey", hotkey, "| port", port, "| agent", agent)
print("args:", " ".join(args))
if flag == "--dry-run":
    print("dry run - would run:", " ".join(cmd[:8]), "...")
    sys.exit(0)
original = {k: str(v) for k, v in (env.get("env") or {}).items() if isinstance(v, (str, int, float))}
environ = {k: v for k, v in {**os.environ, **original}.items() if not k.startswith("MINER_RECORD_")}
subprocess.run(["pm2", "delete", name], check=False, capture_output=True)
subprocess.run(cmd, check=True, env=environ)
subprocess.run(["pm2", "save"], check=False)
EOF
