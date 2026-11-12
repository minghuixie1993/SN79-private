#!/usr/bin/env bash
# Scores the current agents/MyAgentVInput.py (or AGENT=<class>) for one improvement step, under fixed conditions:
#   1. snapshots the agent file to agents/eval/versions/step_<step>_<class>.py,
#   2. runs the local stack (agents/proxy/eval_config.json: same simulation XML, seed and agent params
#      every time) with state recording on, until <minutes> simulation minutes past the grace period,
#   3. stops the stack and replays the recording through the validator's scoring (score_run.py),
#      which stores every dashboard field and appends a row to agents/eval/SCOREBOARD.md.
#
# Usage (from anywhere):
#   agents/eval/run_step.sh <step> "<what changed>" [minutes=60]
# Example:
#   agents/eval/run_step.sh 0 "baseline" 60
#
# Another agent file is scored with AGENT=<class> (agents/<class>.py); it runs on its own defaults plus
# lazy_load=1 and any PARAMS ("name=value ..."), not on eval_config.json's MyAgentVInput params, which other
# agents may read in different units (e.g. MyAgentV2's skew is in half spreads):
#   AGENT=MyAgentVSimples PARAMS="lean_c0=1.5" agents/eval/run_step.sh vs_1 "lean 1.5" 60
# An agent in a subfolder of agents/ is named by its path there, without .py:
#   AGENT=Agent_10_23/MyAgent10_23_Comment agents/eval/run_step.sh c1023_0 "baseline" 60
#
# The dynamic (DIS) maker fee of a book moves with the passive share of MINER volume on it, against a target of
# 0.4, and stays 0 until the book has 300 miner trade slots. A lone maker is all passive, so over a long run its
# books drift to the full +1.5% maker fee and never see a rebate. TAKER=1 adds a RandomTakerAgent miner (uid 1;
# the agent scored stays uid 0) whose market orders pull that share down, so fees and rebates both happen;
# TAKER_PARAMS overrides its defaults. Its fills also count as a miner counterparty in the making discount.
#   TAKER=1 AGENT=MyAgentVComment agents/eval/run_step.sh vc_fee "DIS fees with a taker" 60
#
# Wall time is about 2 real minutes per simulation minute, plus ~5 minutes of grace and ~0.5 s per
# recorded state to score. Stop early with Ctrl-C: the stack is shut down and nothing is scored.
set -euo pipefail

STEP="${1:?usage: run_step.sh <step> \"<what changed>\" [minutes]}"
NOTE="${2:?usage: run_step.sh <step> \"<what changed>\" [minutes]}"
MINUTES="${3:-60}"
AGENT="${AGENT:-MyAgentVInput}"
PARAMS="${PARAMS:-}"
TAKER="${TAKER:-0}"
TAKER_PARAMS="${TAKER_PARAMS:-}"

EVAL_DIR=$(dirname "$(realpath "$0")")
REPO=$(realpath "$EVAL_DIR/../..")
STAMP=$(date +%Y%m%d_%H%M%S)
RECORD_DIR="$REPO/agents/data/records/step_${STEP}_${STAMP}"
LOG="$RECORD_DIR/run.log"

if pgrep -f "proxy.py --config" >/dev/null || pgrep -x taosim >/dev/null; then
    echo "ERROR: a proxy or taosim is already running; stop it first (tmux kill-session -t proxy_test)." >&2
    exit 1
fi

mkdir -p "$RECORD_DIR" "$EVAL_DIR/versions"
cp "$REPO/agents/$AGENT.py" "$EVAL_DIR/versions/step_${STEP}_$(basename "$AGENT").py"

# The fixed evaluation config, with the recording pointed at this step's directory.
python3 - "$REPO/agents/proxy/eval_config.json" "$RECORD_DIR" "$AGENT" "$PARAMS" "$TAKER" "$TAKER_PARAMS" <<'EOF'
import json, sys
config_file, record, agent, extra, taker, taker_extra = sys.argv[1:7]
config = json.load(open(config_file))
config["proxy"]["record_states"] = record
if agent != "MyAgentVInput" or extra:
    params = {"lazy_load": 1}
    for item in extra.split():
        key, value = item.split("=")
        params[key] = float(value)
    config["agents"] = {"path": config["agents"]["path"], "start_port": config["agents"].get("start_port", 8888),
                        agent: [{"params": params, "count": 1}]}
if taker == "1":
    # Added last, so the agent scored keeps uid 0. About two market orders per book per simulated minute.
    params = {"min_quantity": 0.5, "max_quantity": 1.5, "max_books": 16, "min_trade_interval_s": 2.0,
              "max_trade_interval_s": 6.0, "max_fee_rate": 0.02, "min_leverage": 0.0, "max_leverage": 0.0,
              "lazy_load": 1}
    for item in taker_extra.split():
        key, value = item.split("=")
        params[key] = float(value)
    config["agents"]["RandomTakerAgent"] = [{"params": params, "count": 1}]
json.dump(config, open(f"{record}/launcher_config.json", "w"), indent=4)
EOF

GRACE_S=$(python3 -c "
import xml.etree.ElementTree as ET
print(int(ET.parse('$REPO/simulate/trading/run/config/multiasset_simulation_0.xml').getroot().get('gracePeriod')) // 10**9)")
TARGET_S=$(( GRACE_S + MINUTES * 60 + 30 ))

# Job control puts the background job in its own process group whose id is its pid, so one kill stops
# the launcher, proxy, simulator and agent together. (setsid would fork when it is a group leader, and
# its pid would then be neither the group nor alive.)
set -m
"$REPO/agents/proxy/run" --no-tmux --config "$RECORD_DIR/launcher_config.json" >"$LOG" 2>&1 &
PGID=$!
set +m
stop_stack() { kill -- -"$PGID" 2>/dev/null || true; wait "$PGID" 2>/dev/null || true; }
trap 'echo; echo "Interrupted: stopping the stack."; stop_stack; exit 130' INT TERM

echo "Step $STEP: recording to $RECORD_DIR"
echo "Running until simulation time ${TARGET_S}s (grace ${GRACE_S}s + ${MINUTES} min); log: $LOG"
while true; do
    sleep 10
    if ! kill -0 "$PGID" 2>/dev/null; then
        echo "ERROR: the stack exited early; see $LOG" >&2
        exit 1
    fi
    NOW=$( (tail -c 2000000 "$LOG" | grep -a -o "TAOSIM | TIME : [0-9:]*" || true) | tail -1 | awk -F' : ' '{split($2,t,":"); print t[1]*3600+t[2]*60+t[3]}')
    [[ -n "$NOW" ]] || continue
    printf "\r  simulation time %6ss / %ss" "$NOW" "$TARGET_S"
    (( NOW >= TARGET_S )) && break
done
echo
stop_stack
trap - INT TERM
echo "Stack stopped. Scoring..."

python3 "$EVAL_DIR/score_run.py" "$RECORD_DIR" --step "$STEP" --note "$NOTE" --minutes "$MINUTES" \
    2>&1 | grep -v "De-beta warming" | tee "$RECORD_DIR/score.log"
echo
echo "Scoreboard: $EVAL_DIR/SCOREBOARD.md"
