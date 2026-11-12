# MyAgent scoreboard

One row per improvement step, scored by `agents/eval/score_run.py` with the validator's own code.
Rank, share and score fields are left out: locally there is only one miner, so they do not compare.
Full field dumps are in `agents/eval/scores/<step>/`.

| Step | Change | Run | Window | Making | Captured | Realized | Skill | Net alpha | Coverage books | Skill books | Scored books | P11 | S3 | BG share | Notional | PnL | Realized PnL | Activity |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| v2_R1 | V2 + Q1-Q4 | v2_R1_20261021_130354 | 60 min | 0 | 702.4 | 0 | 0.06453 | 201.1 | 128 | 82 | 82 | 1.000 | 1.000 | 1.000 | 2.71e+06 | 58.17 | -584.01 | 0.000 |
| v2_R0 | V2 baseline (before Q1-Q4) | v2_R0_20261021_152411 | 60 min | – | – | – | – | – | – | – | – | – | – | – | – | -6841.37 | -1286.96 | 0.000 |
| v2_R2 | V2 + Q1-Q4, fill_window 10800 | v2_R2_20261021_174755 | 60 min | – | – | – | – | – | – | – | – | – | – | – | – | 1241.94 | -765.38 | 0.000 |
| v2_R3 | V2 + Q1-Q4, max_fee_rate 1.0 | v2_R3_20261021_201819 | 60 min | – | – | – | – | – | – | – | – | – | – | – | – | -1302.98 | -525.04 | 0.000 |
| v2_R5 | V2 predictive fair price, class spreads, queue keeping | v2_R5_20261022_183511 | 60 min | 0 | 393.3 | 0 | 2.828 | 1911 | 128 | 111 | 111 | 1.000 | 1.000 | 1.000 | 1.286e+06 | -623.48 | 1578.04 | 0.000 |
| vs_S0 | VSimples baseline: V2 fixes, 3-input fair price, no spread adaptation | step_vs_S0_20261023_042007 | 60 min | 135.1 | 452.9 | 135.1 | 1.731 | 808.6 | 128 | 96 | 96 | 1.000 | 1.000 | 1.000 | 1.38e+06 | 1669.28 | 954.49 | 0.000 |
| g1023_v3 | MyAgentV3, same run as g1023_gpt | gpt1023_vs_v3_20261023_071823 | 60 min | 124.9 | 396.3 | 124.9 | 3.05 | 2581 | 128 | 91 | 91 | 1.000 | 1.000 | 1.000 | 1.618e+06 | -1722.70 | 1733.35 | 0.000 |
| g1023_gpt | Agent_10_23/MyAgent10_23_Gpt (V3 + planned quotes), same run as g1023_v3 | gpt1023_vs_v3_20261023_071823 | 60 min | 118.8 | 390.7 | 118.8 | 3.149 | 2263 | 128 | 94 | 94 | 1.000 | 1.000 | 1.000 | 1.557e+06 | -451.94 | 1478.71 | 0.000 |
