# Mainnet / testnet miner tools

For a registered miner: record what the real validators send it, and track how the network scores it. Nothing here
changes a tracked file, so `run_miner.sh`'s `git pull` stays clean.

| File | What it does |
|---|---|
| `recorded_miner.py` | Runs the stock `taos/im/neurons/miner.py` with every validator round recorded |
| `recorder_on.py` | Switches the pm2 `miner` process from `run_miner.sh` to the recorder (same args and env); `--off` back |
| `state_tap.py` | The recorder: frame format, `read_frames`, `decode_state`, `recordings` |
| `track_uid.py` | Chain consensus + validator-published legs for one hotkey, appended to `agents/data/real_track/` |
| `replay_agent.py` | Replays recorded rounds through a candidate agent; estimated fills and 20 s markouts vs the live agent |
| `miner_health.py` | Is the live miner healthy enough to run a local validation beside it? (exit 0 / 1 / 2) |

## Testing a new agent on the real market

```bash
python agents/mainnet/replay_agent.py agents/Agent_10_23/MyAgent10_23_4Gpt.py --params event_lookback_minutes=1
python agents/mainnet/replay_agent.py agents/Agent_10_23/MyAgent10_23IOV.py --params lazy_load=0 --since "2026-10-24 00:00"
```

The candidate gets every recorded round as the miner would deliver it, with its own simulated orders in the account
(the live agent's resting orders swapped out) and without the live agent's fill notices. Fills are estimated from
trades that print through an order's price after it reaches the book; trades against the live agent are left out.
The live agent gets the same estimate on what it really had resting, and its estimate is checked against its real
fills: on the 23 October test recording the estimate came to 89% / 80% / 64% of the real fill notional on
simulation_0 / 1 / 2 (fills at exactly the order's price are not counted). Compare candidate and live rows, not
absolute numbers; a replay cannot show how the market would have answered different orders. Results:
`agents/data/replays/<agent>_<time>/summary.md`. Agents without 3Gpt's automatic lazy_load need `lazy_load=0`.

## Running local validations beside the live miner

Same machine is fine (ports, IPC and data folders do not collide), with four rules, since the miner's reply time is
its score (order delay, timeouts) and this host has no swap:
- run validations at the lowest priority (`nice -n 19`),
- set `min_available_mb` to 6000 in network configs, so a run stops itself before the miner is at risk,
- keep runs small: 2 validators, only the agents being compared,
- gate and watch with `miner_health.py` (reply p90, free memory); scale down when it reports a problem.
Never edit the agent file the live miner runs: pm2 restarts the miner from the file on a crash.

## After registering

```bash
./run_miner.sh ...                                   # as usual: wallet, agent (-n), params (-m)
python agents/mainnet/recorder_on.py                 # record every validator round
python agents/mainnet/miner_health.py                # recording works when reply_seconds appear (within a minute)
python agents/mainnet/track_uid.py --every 30        # chain + published snapshot every 30 min (default wallet
                                                     # redpig-coldkey / redpig-miner; --hotkey to override)
python agents/network/collect_real.py --network mainnet --track <uid or hotkey> --books   # dashboard board
```

`run_miner.sh` restarts the stock miner each time it runs; run `recorder_on.py` again after it.

pm2 on this host: `~/.pm2` belongs to root (pm2 was once run with sudo), so this account uses its own pm2 home:
`~/.bashrc` exports `PM2_HOME=~/.pm2-trustvs` (open a new terminal once), and `recorder_on.py` falls back to it.

## What is recorded

One frame per validator round, as it arrived: the compressed books and payload untouched, the validator hotkey,
version and compression engine, the simulation timestamp, the arrival time, the miner's reply time, and the agent's
reply. Files: `agents/data/mainnet_records/<validator hotkey>/<YYYYmmdd_HH>.frames.zst`, plus `tap_stats.json`
(written, dropped, per-validator counts), refreshed every minute.

Recording never delays a reply: the request only queues references; a background thread writes. A full queue or a
spent disk budget drops frames (counted), it never waits. Settings (`recorder_on.py` flags or environment):

| Flag | Environment | Default |
|---|---|---|
| `--dir` | `MINER_RECORD_DIR` | `agents/data/mainnet_records` |
| `--validators` | `MINER_RECORD_VALIDATORS` | `all` (or comma-separated hotkey prefixes) |
| `--every` | `MINER_RECORD_EVERY` | `1` (every round) |
| `--max-gb` | `MINER_RECORD_MAX_GB` | `50` |

Local runs wrote about 4 GB per validator-hour of raw states; with many validators querying, narrow
`--validators` or raise `--every`.

```python
import sys; sys.path.insert(0, "agents/mainnet")
from state_tap import recordings, read_frames, decode_state
for validator, files in recordings().items():
    for frame in read_frames(files[-1]):
        state = decode_state(frame)          # the MarketSimulationStateUpdate the agent saw
        reply = frame["response"]            # what the agent answered, frame["seconds"] how fast
```

A validator sends a miner every book and every trade, but only that miner's own account, so a recording replays
the miner's own view (its agent, its fills, the market) rather than every miner's score.
