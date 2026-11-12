# Local validator network

Several local validators, each running its own simulation, with one set of agents serving all of them, the way a
miner serves every validator that queries it. Each run produces per-validator recordings and a leaderboard scored
with the validator's own code.

| File | What it does |
|---|---|
| `run_network.py` | Starts the agents once, then a proxy and a simulator per validator; stops at the target simulation time |
| `agent_server.py` | Serves one agent to every validator, one state at a time, as the miner does |
| `score_network.py` | Replays each validator's recording through `agents/eval/score_run.py --all-uids` and writes `leaderboard.md` |
| `network.example.json` | 2 validators x 128 books, every agent in `agents/` |
| `network.light.json` | The same without the agents this host cannot carry (below) |
| `network.64.json` | 2 validators x 64 books, 30 simulation minutes: what fits this host |

## Run

From the repository root:

```bash
python agents/network/run_network.py agents/network/network.64.json --score
python agents/network/run_network.py agents/network/network.light.json --minutes 2    # a short check
python agents/network/score_network.py agents/data/network/<run>                         # score a finished run
```

Each run writes `agents/data/network/<name>_<timestamp>/`:

- `network.json`: agents (uid, file, params, port) and validators (name, seed, port)
- `leaderboard.md` / `leaderboard.json`: combined and per-validator boards
- `agents/<uid>_<file>.log`: each agent's log
- `<validator>/`: `config/` (that validator's layout, with its seed and port), `proxy.log`, `taosim.log`,
  `record/` (states for replay and scoring), `sim/` (simulator output), `score/`

## Config

From 23 October 2026 every config runs **2 validators** (the first two: names and seeds unchanged, so runs
compare with earlier ones) and `min_available_mb` 6000: a third validator cost a third more time and ~2 GB for
little extra information, and the user's mainnet miner shares this host (see agents/mainnet/README.md). Keep new
configs at 2. The combined board's median over 2 validators is their mean.

| Key | Meaning |
|---|---|
| `validators` | `[{"name", "seed"}]`; the name is the hotkey agents see in `state.dendrite.hotkey` |
| `simulation_xml` | Layout every validator copies: `multiasset_simulation_0.xml` (128 books), `_64`, `_32` |
| `agents` | `"all"` (every `agents/*.py` with a `launch(...)` call, minus `exclude`) or `[{"file", "params"}]` |
| `common_params` | Passed to every agent; `gtx_enabled=false` keeps GenTRX data collection and training off |
| `agent_params` | Per-file overrides, on top of `bin/agent_params.sh` defaults |
| `minutes` | Simulation minutes after the 10-minute grace period |
| `min_available_mb`, `stall_minutes` | The run stops itself below this much free memory, or when no simulation advances |

## What this host can carry (8 cores, 24 GB, no swap), measured 2026-10-23

- 3 validators x 128 books x 16 light agents: a step takes 4.5-7 s and memory grows ~80 MB/min, so a 60-minute run
  (5-7 h) runs out of memory. Use 64 or 32 books, fewer validators, or a larger machine.
- Excluded in `network.light.json`: `HybridTrainingAgent` and `ImbalanceAgent` (~5.7 GB each after 40 states for one
  validator), `DevAgent` and `MovingHurstAgent` (~5 s per state, past the 5 s timeout), `SimpleRegressorAgent`
  (did not finish 40 states in 10 min), `CustomTrainingAgent` (needs `pyarrow`, not installed).
- Without swap, running out of memory lets the kernel kill desktop-session processes; the memory guard exists for that.

## Scoring notes

- Per validator, agents are ranked by their de-beta trading score averaged over every 5 s scoring boundary; the
  combined board takes the median over validators, a stand-in for the network's stake-weighted consensus.
- `taos/im/validator/debeta.py` was fixed on 2026-10-22: uid 0's fills never reached the realized-markout queue
  (`t.get("Ma") or -1` reads 0 as -1), so uid 0's making was always 0. Recordings scored before the fix understate
  uid 0's making; re-score them.
- Rank and share fields compare agents within one run only; P&L in the board is the validator dashboard's field.
