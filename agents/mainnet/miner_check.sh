#!/bin/bash
# Every 30 minutes: miner health (process, memory, reply p90), pm2 restarts, and the latest validator gauges
# from track_uid.py. Runs under pm2 (name miner-check) so it keeps going with no Claude session open.
# One line per check in agents/data/real_track/miner_check.log; ALERT lines also go to miner_check_alerts.log.
cd "$(dirname "$0")/../.." || exit 1
LOG=agents/data/real_track/miner_check.log
ALERTS=agents/data/real_track/miner_check_alerts.log
export PM2_HOME=${PM2_HOME:-$HOME/.pm2-trustvs}
while true; do
    now=$(date -u '+%Y-%m-%d %H:%M UTC')
    health=$(python3 agents/mainnet/miner_health.py 2>&1); hstatus=$?
    health=$(echo "$health" | tail -1 | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    h = json.loads(raw)
except ValueError:
    print(raw.strip()[:200]); sys.exit()
rs = "; ".join("%s p50 %.3f p90 %.3f" % (k, v["p50"], v["p90"]) for k, v in (h.get("reply_seconds") or {}).items())
print("healthy=%s free_mb=%s problems=%s reply %s" % (h.get("healthy"), h.get("available_mb"), h.get("problems"), rs))
')
    pm2info=$(pm2 jlist 2>/dev/null | python3 -c '
import json, sys
for p in json.load(sys.stdin):
    if p["name"] == "miner":
        e = p["pm2_env"]; a = e.get("args") or []
        agent = a[a.index("--agent.name") + 1] if "--agent.name" in a else "?"
        print(e["status"], "restarts=%s" % e.get("restart_time"), "agent=" + agent)
' 2>/dev/null)
    gauges=$(python3 - <<'EOF' 2>/dev/null
import json, glob, os
f = max(glob.glob("agents/data/real_track/mainnet_*.jsonl"), key=os.path.getmtime)
r = json.loads(open(f).readlines()[-1])
c = r["chain"]
if "error" in c:      # a failed chain query is not "uid None" (25 Oct: DNS / handshake failures read as unregistered)
    out = [f"track {r['at'][11:16]} chain ERROR {str(c['error'])[:60]}"]
elif not c.get("registered"):
    out = [f"track {r['at'][11:16]} NOT REGISTERED (block {c.get('block')})"]
else:
    out = [f"track {r['at'][11:16]} uid {c.get('uid')} inc {c.get('incentive')} immunity {c.get('immunity_left_blocks')}"]
for v in (r.get("published") or {}).values():
    g = v["gauges"]
    out.append(f"making {g.get('debeta_making', 0):.2f} skill {g.get('debeta_skill', 0):+.3f} alpha {g.get('debeta_skill_net_alpha', 0):+.0f}")
    out.append(" ".join(f"c{k[-1]} {d['skill']:+.2f}/{d['net_alpha']:+.0f}" for k, d in v.get("classes", {}).items()))
print(" | ".join(out))
EOF
)
    line="$now | miner $pm2info | $gauges | health $health"
    echo "$line" >> "$LOG"
    if [ "$hstatus" -ne 0 ] || [[ "$pm2info" != online* ]]; then
        echo "ALERT $line" | tee -a "$ALERTS" >> "$LOG"
    fi
    sleep 1800
done
