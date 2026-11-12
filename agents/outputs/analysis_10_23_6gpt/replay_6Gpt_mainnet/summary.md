# Replay: MyAgent10_23_6Gpt on 2254 recorded rounds

Validators 5EWwdZB7; live uid 105; params lazy_load=1.0 persist_state=0.0 shield_pre_s=1.0 shield_post_s=1.0 quiet_size_mult_c0=2.0 quiet_size_mult_c1=1.25.

Reply time: candidate p50 0.0712 s, p90 0.0874 s, max 0.208 s (live p50 0.116 s). Failed rounds 0. Orders placed 112667, refused post-only 0, cancelled 110951.

| Class | Who | Est. fills | Est. notional | Captured bps | Realized bps | Quality | Buy share | Quote dist bps | Real fills (live) |
|---|---|---|---|---|---|---|---|---|---|
| simulation_0 | candidate | 1498 | 591109 | +7.54 | +3.52 | 0.47 | 0.53 | 7.1 |  |
| simulation_0 | live | 1258 | 362548 | +7.71 | +3.78 | 0.49 | 0.52 | 7.7 | 411515 |
| simulation_1 | candidate | 616 | 286245 | +5.82 | +1.37 | 0.24 | 0.75 | 7.1 |  |
| simulation_1 | live | 366 | 167120 | +5.87 | -1.01 | 0.00 | 0.59 | 8.0 | 279637 |

Making credit on the validator's basis (sum over books of 2 x min(buy, sell capture), times the uid quality with each fill's realized spread clamped to its capture): simulation_0 candidate 61.2 / live 28.9; simulation_1 candidate 13.7 / live 4.81; uid quality (clamped) candidate 0.24 / live 0.15.

Uid-wide quality (the validator's one ratio over every book): candidate 0.40 at +2.82 bps, live 0.32 at +2.27 bps.

By reseed phase (sim seconds since the fundamental's 30 s boundary):

| Class | Who | Phase | Est. fills | Est. notional | Captured bps | Realized bps | Quality |
|---|---|---|---|---|---|---|---|
| simulation_0 | candidate | 1-5s | 409 | 112889 | +8.39 | +2.99 | 0.36 |
| simulation_0 | candidate | 5-28s | 1063 | 470728 | +7.36 | +3.57 | 0.49 |
| simulation_0 | candidate | 28-30s | 26 | 7492 | +6.05 | +8.12 | 1.00 |
| simulation_0 | live | 0-1s | 149 | 36152 | +6.46 | -2.40 | 0.00 |
| simulation_0 | live | 1-5s | 350 | 90632 | +8.17 | +1.97 | 0.24 |
| simulation_0 | live | 5-28s | 718 | 223258 | +7.72 | +5.37 | 0.70 |
| simulation_0 | live | 28-30s | 41 | 12506 | +7.67 | +6.42 | 0.84 |
| simulation_1 | candidate | 1-5s | 403 | 176897 | +5.67 | +0.48 | 0.08 |
| simulation_1 | candidate | 5-28s | 211 | 108159 | +6.07 | +2.81 | 0.46 |
| simulation_1 | candidate | 28-30s | 2 | 1189 | +6.47 | +4.02 | 0.62 |
| simulation_1 | live | 0-1s | 96 | 46868 | +5.63 | -3.97 | 0.00 |
| simulation_1 | live | 1-5s | 209 | 91001 | +6.20 | +0.12 | 0.02 |
| simulation_1 | live | 5-28s | 61 | 29251 | +5.25 | +0.24 | 0.05 |

Est. = fills estimated from trades printing through the order's price (see the module docstring); compare the live row's estimate with its real fills to see how far the estimate holds on this market.
