# Local dashboard

A web view of `agents/network` runs, laid out like the mainnet agents dashboard: a runs list, a leaderboard per run
(overall and per asset class) with score-over-time charts, and a page per agent (per-validator legs, asset classes,
per-book volume and realized P&L, the agent's log).

```bash
python agents/dashboard/server.py            # then open http://127.0.0.1:8050
```

It reads `agents/data/network/<run>/` only; nothing about a run changes until you press a button:

- **Score now** replays each validator's recording so far into `<validator>/score_live/`, for a run still going.
  The final `score/` is left to `score_network.py` (`run_network.py --score`), so the two never mix.
- **Rescore** replays the whole recording into `score/` again. Runs scored before 23 October 2026 lack the
  per-agent timelines and per-class/per-book detail; a rescore adds them.

Both run `agents/eval/score_run.py --all-uids` at `nice 19`, a few minutes per validator. Pages refresh by themselves
while a run or a replay is going.

The trading score shown is the validator's de-beta trading score averaged over every 5 s scoring boundary, median
across validators. "Score (EMA)" is the weight input after the 3 sim-hour track-record EMA, so on a 30-60 minute run
it mostly rewards an early start; compare making, S3, skill and net alpha for quoting changes.
