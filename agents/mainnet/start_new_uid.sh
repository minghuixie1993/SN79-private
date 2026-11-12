#!/bin/bash
# Starts the miner under a freshly registered uid with the prepared start set (RE_REGISTER.md, 26 Oct).
#     bash agents/mainnet/start_new_uid.sh <new uid>              # immunity window: c0 0.024 / cap 0.06 front-load
#     bash agents/mainnet/start_new_uid.sh <new uid> --post-immunity   # after immunity: c0 0.012 / cap 0.03 (day budget)
#     AGENT=MyAgent10_26_2Gpt bash agents/mainnet/start_new_uid.sh <uid> ...   # another agent (default MyAgent10_23_9Gpt)
#     PACE=1 AGENT=MyAgent10_26_6Gpt bash agents/mainnet/start_new_uid.sh <uid> --post-immunity   # budget pacing, no step-down
#     EXTRA="balance_basis=0 fund_demean_s_c0=600 coef_fund_dm_c0=0.07" AGENT=... bash ... --post-immunity   # tested extras
# Copies the learned state from UID 8's folder, rotates the agent CSVs, waits for reseed phase 1-2 only when the miner log
# is fresh (a pruned miner gets no states, so its phase is stale), then restarts with recorder_on.py --max-gb 30.
set -e
NEW_UID="$1"
[ -n "$NEW_UID" ] || { echo "usage: $0 <new uid>"; exit 1; }
cd "$(dirname "$0")/../.."
export PM2_HOME=${PM2_HOME:-$HOME/.pm2-trustvs}
AGENT=${AGENT:-MyAgent10_23_9Gpt}
PARAMS="event_lookback_minutes=1 latency_s=1.05 shield_pre_s=3 shield_post_s=1 shield_post_s_c1=5 shield_post_s_c2=5 \
quiet_size_mult_c0=2.5 quiet_size_mult_c1=1.25 quiet_size_mult_c2=1.6 skew_c1=8 max_inv_ratio_c1=0.06 half_spread_c0=0.0004 \
size_fraction_c0=0.024 size_fraction_c1=0.024 size_fraction_c2=0.024 max_order_fraction=0.06 coef_fund_c2=0.004 pace_burst=8 \
adapt_max=1.0 max_lean=0.75 pending_aware=1 fill_skew_c0=0.6 fill_skew_c2=0.6"
if [ "$2" = "--post-immunity" ]; then
    if [ "${PACE:-0}" = "1" ]; then
        # per-book budget pacing instead of the blanket step-down (MyAgent10_26_6Gpt; free-time #82): c0 keeps 0.024 /
        # cap 0.06 and each c0 book's size shrinks only while it trades above its even share of the 450k day budget
        PARAMS="$PARAMS budget_pace_c0=1"
    else
        # 450k per book per rolling 24 sim h (reviews #43-#45): back to the sustainable c0 rate once immunity is over
        PARAMS="${PARAMS/size_fraction_c0=0.024/size_fraction_c0=0.012}"
        PARAMS="${PARAMS/max_order_fraction=0.06/max_order_fraction=0.03}"
    fi
    # c0 waits 5 s after each fundamental reset (grid_post0, 26 Oct 14:20: making_vb +1.2% / +19.5%, c0 realized up on
    # both replay windows; shield_grid_log.md)
    PARAMS="$PARAMS shield_post_s_c0=5"
fi
# EXTRA="k=v ..." appends tested extras without editing this file; a key already in PARAMS is replaced, not duplicated
for kv in ${EXTRA:-}; do
    key="${kv%%=*}"
    PARAMS="$(echo " $PARAMS " | sed -E "s/ ${key}=[^ ]+ / /") $kv"
done
PARAMS="$(echo $PARAMS)"
mkdir -p "agents/data/$NEW_UID"
if [ -f agents/data/8/MyAgent10_23_6Gpt_state.json ] && [ ! -f "agents/data/$NEW_UID/MyAgent10_23_6Gpt_state.json" ]; then
    cp agents/data/8/MyAgent10_23_6Gpt_state.json "agents/data/$NEW_UID/"
    echo "learned state copied to agents/data/$NEW_UID/"
fi
python agents/mainnet/rotate_agent_csv.py | tail -1
LOG=$(ls -t ~/.pm2-trustvs/logs/miner-out*.log | head -1)
AGE=$(( $(date +%s) - $(stat -c %Y "$LOG") ))
if [ "$AGE" -lt 60 ]; then
    PHASE_TOOL=/tmp/claude-1001/-home-Trustvs-workspace-sn-79/39a0c2a3-00d0-4d3e-8d93-c3edd2df1760/scratchpad/phase_now.py
    for i in $(seq 1 400); do
        ph=$(python3 "$PHASE_TOOL" 2>/dev/null | cut -d' ' -f1)
        if [ "$ph" = "1" ] || [ "$ph" = "2" ]; then echo "reseed phase $ph"; break; fi
        sleep 0.5
    done
else
    echo "miner log ${AGE}s old (no states while pruned): restarting at once"
fi
date +%T
python agents/mainnet/recorder_on.py --max-gb 30 --agent "$AGENT" --agent-params "$PARAMS" 2>&1 | grep -o "agent.params.*" | head -1
