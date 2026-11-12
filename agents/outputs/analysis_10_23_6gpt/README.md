# UID 105 (SN79 mainnet): 5Gpt vs 6Gpt analysis bundle

Collected 23 Oct 2026, 19:00 CEST (17:00 UTC). Times in the tables are UTC.

## 1. Actual agent parameters (live miner)

```
--agent.name MyAgent10_23_6Gpt
--agent.params event_lookback_minutes=1 latency_s=0.05 shield_pre_s=1 shield_post_s=1 quiet_size_mult_c0=2 quiet_size_mult_c1=1.25
```

Everything else is at the file's defaults (see `agents/Agent_10_23/MyAgent10_23_6Gpt.py`), including `quiet_from_s=5`,
`quiet_to_s=28`, `reseed_s` from the validator's `fp_seed_interval` (30 s), `adapt_max=1.25`, `book_mult_max=1.25`,
`max_lean=0.5`, and `lazy_mode=auto` (lazy books on the real miner).

Agent history on UID 105:

| From (UTC) | Agent | Params beyond `event_lookback_minutes=1` |
|---|---|---|
| 12:03 | MyAgent10_23_2Gpt | `lazy_load=0` |
| 15:20 | MyAgent10_23_5Gpt | `latency_s=0.05 shield_post_s_c0=1` |
| 15:35 | MyAgent10_23_5Gpt | `latency_s=0.05 shield_pre_s=1 shield_post_s=1` |
| 16:23 | MyAgent10_23_6Gpt | the line above, plus `quiet_size_mult_c0=2 quiet_size_mult_c1=1.25` |

## 2. Validator results (published by the only active validator, 5EWwdZB7)

The validator's legs are computed over its rolling 3 sim-hour window. The mainnet simulation advances about 1 sim-second
per 4.4 wall seconds, so each snapshot mixes every agent that has run in the last several wall hours. The rows are
not a clean per-agent comparison. Section 3 has the clean per-agent numbers, measured from the agent's own fills.

| UTC | Agent live | Score | Trading rank /257 | Making | Quality (realized/captured) | Skill | Net alpha | Coverage books | Skill books | S3 | P11 | Call time |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 14:32 | 2Gpt | 0.000 | 251 | 1.3 | 0.11 | -0.152 | -197 | 117 | 93 | 0.57 | 0.84 | 0.78 s |
| 15:03 | 2Gpt -> 5Gpt | 0.000 | 246 | 5.0 | 0.17 | -0.199 | -521 | 123 | 103 | 0.68 | 0.84 | 0.85 s |
| 15:33 | 5Gpt | 0.000 | 252 | 11.8 | 0.22 | -0.224 | -689 | 126 | 103 | 0.76 | 0.85 | 0.88 s |
| 16:03 | 5Gpt | 0.000 | 242 | 14.6 | 0.21 | -0.247 | -704 | 128 | 100 | 0.79 | 0.86 | 0.63 s |
| 16:33 | 6Gpt | 0.004 | 242 | 18.3 | 0.23 | -0.258 | -763 | 128 | 99 | 0.81 | 0.86 | 0.64 s |
| 16:52 | 6Gpt | 0.011 | 241 | 19.6 | 0.24 | -0.254 | -745 | 128 | 101 | 0.81 | 0.86 | 0.63 s |

At 16:52 the chain incentive was 0.000015, the first non-zero value. Per class at 16:52: simulation_0 had making 15.4,
skill -0.259 and 85 coverage books; simulation_1 had making 5.7, skill -0.327, 16 coverage books and coverage factor 0.8.

The full snapshots, every 30 min with every gauge and the per-class legs, are in `validator_published_uid105.jsonl`.

## 3. The agent's own measurements (live fills)

| Card time (CEST) | Agent | Class 0 markout | Class 0 quality | Class 1 markout | Class 1 quality | Uid-wide quality | Fills (buy / sell) |
|---|---|---|---|---|---|---|---|
| 16:07 | 2Gpt | -1.60 bps | 0.00 | -1.27 bps | 0.00 | ~0 | 528 / 540 |
| 18:19 | 5Gpt | +1.97 bps | 0.29 | -0.42 bps | 0.00 | ~0.15 | 186 / 197 |
| 18:57 | 6Gpt | +3.07 bps | 0.45 | -0.52 bps | 0.00 | ~0.28 | 146 / 97 |

The latest PLAN CARD and SCORE CARD are in `plan_card_latest.txt`. They show 0 rejections and 0 runtime errors,
miner-side reply p50 80 ms / p90 100 ms, a simulation_1 background share of 6% (S3 ~0.28), and fill markout by
reseed phase.

**Latency:** the miner itself answers in p50 0.099 s and p90 0.125 s (`recordings/tap_stats.json`), but the validator
measures a 0.63 s call time. About 0.5 s of that is spent outside the agent, in network and payload transfer.

## 4. Files

| Path | What it is |
|---|---|
| `tests_6Gpt.txt` | `agents/Agent_10_23/tests/test_10_23_6Gpt.py`: **59/59 passed** |
| `tests_5Gpt.txt` | `agents/Agent_10_23/tests/test_10_23_5Gpt.py`: **52/52 passed** |
| `replay_6Gpt_mainnet/summary.md`, `.json` | 6Gpt (live params) replayed on 2,254 recorded mainnet rounds against the live agent's own rounds: estimated fills, captured and realized bps, and quality by class and by reseed phase |
| `replay_6Gpt_mainnet/shield_grid_log.md` | every replay grid and switch decision made today |
| `score_run_local_mainnet_like/` | `agents/eval/score_run.py` output (`score_run_output.log`, `leaderboard.json`, `final.json`) for the local mainnet-like run `mainnet_like_20261023_155412` (60 sim-min, 21 agents including 1Gpt/2Gpt/3Gpt; 3Gpt ranked 1st at 0.773) |
| `validator_published_uid105.jsonl` | the validator's published gauges for UID 105, every 30 min |
| `plan_card_latest.txt` | the latest PLAN CARD and SCORE CARD from the live miner log |
| `recordings/*.frames.zst` | links to the recorded mainnet rounds, one file per UTC hour (14, 15 and 16 UTC; 219, 264 and 297 MB) |
| `recordings/tap_stats.json` | recorder counters and the miner's own reply times |

## 5. Limits of this data

- **There is no `score_run.py` output for mainnet.** `score_run.py` replays a validator-side recording that holds every
  agent's account. The miner's recordings hold only UID 105's own view, so they can't be scored with it. The replay
  in `replay_6Gpt_mainnet/` (`agents/mainnet/replay_agent.py`) is the mainnet-side substitute. Its fills are
  estimates, from trades printing through each order's price.
- **No local run has scored 5Gpt or 6Gpt yet.** In this workspace only session sn-79-5d starts validation runs.
- **The recordings are large binary files**, zstd streams of length-prefixed msgpack frames, as written by
  `agents/mainnet/state_tap.py`. Read them with `state_tap.read_frames`. They are not meant for pasting.
