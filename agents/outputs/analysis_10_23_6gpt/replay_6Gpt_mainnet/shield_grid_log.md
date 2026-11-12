
## 2026-10-23 17:02 interim
659 rounds. Live 2Gpt (estimated): uid quality 0.41, realized +3.08 bps on 190643 notional

| Variant | uid q | uid realized | notional | c0 notional | c0 realized | c0 q | c1 notional | c1 realized | c1 q | c0 0-1s | c1 0-1s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 4Gpt | 0.45 | +3.52 | 286830 | 182928 | +5.32 | 0.63 | 103902 | +0.43 | 0.06 | 18515 @ -0.70 | 49723 @ -1.41 |
| post2 | 0.70 | +5.49 | 149743 | 116499 | +6.07 | 0.74 | 33244 | +3.53 | 0.53 | – @ – | – @ – |
| post3 | 0.73 | +5.81 | 133930 | 116499 | +6.07 | 0.74 | 17431 | +4.13 | 0.63 | – @ – | – @ – |
| post5 | 0.73 | +5.81 | 133930 | 116499 | +6.07 | 0.74 | 17431 | +4.13 | 0.63 | – @ – | – @ – |
| c0 post5, c1 post2 | 0.70 | +5.49 | 149743 | 116499 | +6.07 | 0.74 | 33244 | +3.53 | 0.53 | – @ – | – @ – |
| c0 post3, c1 post2 | 0.70 | +5.49 | 149743 | 116499 | +6.07 | 0.74 | 33244 | +3.53 | 0.53 | – @ – | – @ – |
| c0 post5, c1 post3 | 0.66 | +5.20 | 148276 | 119850 | +5.52 | 0.68 | 28427 | +3.82 | 0.56 | – @ – | – @ – |
| c0 post8, c1 post2 | 0.64 | +4.60 | 118926 | 85097 | +5.03 | 0.68 | 33828 | +3.53 | 0.53 | – @ – | – @ – |
| post2 one-sided | 0.45 | +3.48 | 214059 | 136481 | +5.13 | 0.62 | 77578 | +0.76 | 0.11 | 6521 @ -0.06 | 32056 @ -1.69 |
| post5 one-sided | 0.45 | +3.48 | 214059 | 136481 | +5.13 | 0.62 | 77578 | +0.76 | 0.11 | 6521 @ -0.06 | 32056 @ -1.69 |
| c0p5 c1p2 one-sided | 0.45 | +3.48 | 214059 | 136481 | +5.13 | 0.62 | 77578 | +0.76 | 0.11 | 6521 @ -0.06 | 32056 @ -1.69 |
| c0p5 c1p2 + fund lean x3 | 0.67 | +5.37 | 152200 | 107785 | +6.27 | 0.72 | 44414 | +3.23 | 0.52 | – @ – | – @ – |

## 2026-10-23 17:07 696 rounds (fixed: unique replay folders, validator-basis making)
706 rounds. Live 2Gpt (estimated): uid quality 0.41, realized +3.15 bps on 204452 notional

Live 2Gpt making (validator basis): c0 9.599, c1 3.547; index = 0.75 x c0 + 0.25 x c1 making, each relative to the best variant

| Variant | making c0 | making c1 | index | uid q clamped | uid q | uid realized | notional | c0 realized | c0 buy share | c1 realized | c1 buy share | c0 0-1s | c1 0-1s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4Gpt | 14.14 | 6.663 | 0.87 | 0.17 | 0.42 | +3.26 | 305744 | +4.89 | 0.53 | +0.31 | 0.68 | 19039 @ -0.51 | 51067 @ -1.26 |
| post2 | 17.02 | 1.388 | 0.80 | 0.26 | 0.57 | +4.71 | 199864 | +5.06 | 0.54 | +3.14 | 0.76 | – @ – | – @ – |
| post3 | 15.5 | 1.293 | 0.73 | 0.27 | 0.65 | +5.32 | 182600 | +5.72 | 0.54 | +3.33 | 0.78 | – @ – | – @ – |
| post5 | 11.82 | 0.6488 | 0.55 | 0.26 | 0.68 | +5.47 | 149322 | +5.69 | 0.55 | +4.07 | 0.68 | – @ – | – @ – |
| c0 post5, c1 post2 | 12.02 | 1.407 | 0.58 | 0.26 | 0.65 | +5.13 | 165134 | +5.69 | 0.55 | +3.14 | 0.76 | – @ – | – @ – |
| c0 post3, c1 post2 | 15.53 | 1.476 | 0.74 | 0.27 | 0.64 | +5.22 | 188002 | +5.72 | 0.54 | +3.14 | 0.76 | – @ – | – @ – |
| c0 post5, c1 post3 | 12.67 | 2.37 | 0.65 | 0.26 | 0.65 | +5.21 | 171366 | +5.76 | 0.55 | +3.02 | 0.73 | – @ – | – @ – |
| c0 post8, c1 post2 | 7.575 | 2.854 | 0.44 | 0.26 | 0.62 | +4.58 | 141411 | +5.27 | 0.55 | +2.89 | 0.72 | – @ – | – @ – |
| post2 one-sided | 16.29 | 3.046 | 0.83 | 0.19 | 0.44 | +3.62 | 273719 | +5.05 | 0.54 | +0.59 | 0.84 | 7329 @ +0.43 | 34505 @ -1.70 |
| post5 one-sided | 12.17 | 2.482 | 0.63 | 0.19 | 0.48 | +3.77 | 242383 | +5.51 | 0.54 | +0.65 | 0.87 | 7329 @ +0.43 | 34505 @ -1.70 |
| c0p5 c1p2 one-sided | 12.08 | 2.937 | 0.64 | 0.19 | 0.47 | +3.73 | 244196 | +5.51 | 0.54 | +0.59 | 0.84 | 7329 @ +0.43 | 34505 @ -1.70 |
| c0p5 c1p2 + fund lean x3 | 12.64 | 2.364 | 0.65 | 0.27 | 0.66 | +5.28 | 179193 | +6.41 | 0.55 | +2.73 | 0.88 | – @ – | – @ – |

## 2026-10-23 17:11 741 rounds, class-0 shields
744 rounds. Live 2Gpt (estimated): uid quality 0.44, realized +3.40 bps on 230820 notional

Live 2Gpt making (validator basis): c0 10.32, c1 4.127; index = 0.75 x c0 + 0.25 x c1 making, each relative to the best variant

score = 0.5 x making index + 0.5 x PnL index (PnL = est. notional x realized bps, the skill proxy); each index = 0.75 x c0 + 0.25 x c1 relative to the best variant

| Variant | score | PnL c0 | PnL c1 | making c0 | making c1 | index | uid q clamped | uid q | uid realized | notional | c0 realized | c0 buy share | c1 realized | c1 buy share | c0 0-1s | c1 0-1s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4Gpt | 0.79 | 101.33 | 2.52 | 15.05 | 7.517 | 0.81 | 0.16 | 0.39 | +3.07 | 338068 | +4.74 | 0.52 | +0.20 | 0.66 | 23038 @ -1.66 | 55724 @ -1.53 |
| 5Gpt no shield | 0.79 | 101.33 | 2.52 | 15.05 | 7.517 | 0.81 | 0.16 | 0.39 | +3.07 | 338068 | +4.74 | 0.52 | +0.20 | 0.66 | 23038 @ -1.66 | 55724 @ -1.53 |
| c0 post2 | 0.70 | 88.61 | 2.52 | 12.21 | 8.125 | 0.72 | 0.17 | 0.39 | +3.08 | 297256 | +5.12 | 0.53 | +0.20 | 0.66 | – @ – | 55724 @ -1.53 |
| c0 post3 | 0.69 | 91.63 | 2.52 | 10.72 | 8.466 | 0.67 | 0.18 | 0.43 | +3.34 | 283321 | +5.76 | 0.54 | +0.20 | 0.66 | – @ – | 55724 @ -1.53 |
| c0 post1 | 0.80 | 103.58 | 2.52 | 14.3 | 8.207 | 0.80 | 0.17 | 0.43 | +3.36 | 316376 | +5.39 | 0.53 | +0.20 | 0.66 | – @ – | 55724 @ -1.53 |
| c0 post2 one-sided | 0.77 | 93.11 | 2.52 | 14.74 | 8.318 | 0.82 | 0.17 | 0.39 | +3.09 | 310461 | +5.00 | 0.54 | +0.20 | 0.66 | 7629 @ -0.12 | 55724 @ -1.53 |
| c0 post2, c1 post1 1-sided | 0.72 | 97.50 | 4.76 | 13.76 | 4.211 | 0.66 | 0.18 | 0.45 | +3.58 | 283196 | +5.40 | 0.54 | +0.46 | 0.78 | – @ – | 34505 @ -1.68 |
| c0 post2, c1 post2 | 0.90 | 97.50 | 15.18 | 19.12 | 3.109 | 0.84 | 0.25 | 0.59 | +4.89 | 230475 | +5.40 | 0.54 | +3.05 | 0.71 | – @ – | – @ – |

## 2026-10-23 17:38 841 rounds, replay fixed: cancels land after 25 ms (were instant), ARRIVAL_DELAY 20 -> 25 ms

841 rounds. Live 2Gpt (estimated): uid quality 0.37, realized +2.77 bps on 278594 notional

Live 2Gpt making (validator basis): c0 11.73, c1 3.482; index = 0.75 x c0 + 0.25 x c1 making, each relative to the best variant

score = 0.5 x making index + 0.5 x PnL index (PnL = est. notional x realized bps, the skill proxy); each index = 0.75 x c0 + 0.25 x c1 relative to the best variant

| Variant | score | PnL c0 | PnL c1 | making c0 | making c1 | index | uid q clamped | uid q | uid realized | notional | c0 realized | c0 buy share | c1 realized | c1 buy share | c0 0-1s | c1 0-1s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 5Gpt no shield | 0.69 | 99.05 | -5.15 | 14.65 | 7.043 | 0.80 | 0.12 | 0.29 | +2.26 | 419285 | +3.87 | 0.51 | -0.32 | 0.63 | 32656 @ -2.60 | 61376 @ -1.70 |
| c0 post1 (LIVE) | 0.66 | 99.98 | -5.15 | 13.34 | 7.002 | 0.74 | 0.12 | 0.31 | +2.39 | 401077 | +4.21 | 0.52 | -0.32 | 0.63 | 12948 @ -6.45 | 61376 @ -1.70 |
| c0 pre1 post1 | 0.68 | 100.21 | -5.15 | 13.35 | 7.999 | 0.77 | 0.14 | 0.33 | +2.53 | 379747 | +4.63 | 0.53 | -0.32 | 0.63 | – @ – | 61376 @ -1.70 |
| c0 pre1 post2 | 0.59 | 85.80 | -5.15 | 11.7 | 7.944 | 0.71 | 0.14 | 0.30 | +2.26 | 359751 | +4.37 | 0.54 | -0.32 | 0.63 | – @ – | 61376 @ -1.70 |
| c0 pre1 post3 | 0.57 | 86.53 | -5.15 | 10.41 | 8.038 | 0.66 | 0.14 | 0.32 | +2.40 | 342253 | +4.84 | 0.54 | -0.32 | 0.63 | – @ – | 61376 @ -1.70 |
| c0 pre1 post1, c1 pre1 | 0.61 | 100.21 | -8.52 | 14.51 | 5.477 | 0.74 | 0.15 | 0.35 | +2.64 | 349533 | +4.63 | 0.53 | -0.64 | 0.67 | – @ – | 10960 @ -5.50 |
| pre1 post1 both | 0.95 | 100.21 | 7.96 | 19.04 | 4.783 | 0.90 | 0.20 | 0.44 | +3.43 | 314390 | +4.63 | 0.53 | +0.81 | 0.69 | – @ – | – @ – |
| c0 pre1 post2, c1 pre1 post1 | 0.86 | 85.80 | 7.96 | 17.03 | 4.851 | 0.82 | 0.20 | 0.40 | +3.18 | 294395 | +4.37 | 0.54 | +0.81 | 0.69 | – @ – | – @ – |
| c0 post2, c1 post2 | 0.72 | 85.65 | 2.03 | 13.81 | 6.015 | 0.73 | 0.14 | 0.33 | +2.54 | 344870 | +3.94 | 0.53 | +0.16 | 0.67 | 12697 @ -6.52 | 60226 @ -1.98 |



Finding: with the cancel delay modelled, pre1 cancels matter: 'pre1 post1 both' leads (score 0.95 vs live c0 post1 0.66; uid q clamped 0.20 vs 0.12; c1 PnL +8.0 vs -5.2). Live card 17:28 agreed: c0 0-1s fills -16.4 bps despite post1.

## 2026-10-23 17:50 1120 rounds, full recording (state_tap reader fixed: rounds after each restart now included)

1120 rounds. Live 2Gpt (estimated): uid quality 0.32, realized +2.37 bps on 355800 notional

Live 2Gpt making (validator basis): c0 14.74, c1 4.449; index = 0.75 x c0 + 0.25 x c1 making, each relative to the best variant

score = 0.5 x making index + 0.5 x PnL index (PnL = est. notional x realized bps, the skill proxy); each index = 0.75 x c0 + 0.25 x c1 relative to the best variant

| Variant | score | PnL c0 | PnL c1 | making c0 | making c1 | index | uid q clamped | uid q | uid realized | notional | c0 realized | c0 buy share | c1 realized | c1 buy share | c0 0-1s | c1 0-1s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 5Gpt no shield | 0.67 | 108.00 | -6.02 | 16.97 | 8.369 | 0.73 | 0.12 | 0.26 | +1.94 | 523860 | +3.57 | 0.51 | -0.27 | 0.67 | 42022 @ -3.05 | 78290 @ -1.84 |
| c0 post1 (LIVE) | 0.67 | 111.26 | -6.02 | 16.02 | 8.683 | 0.71 | 0.12 | 0.28 | +2.09 | 501850 | +3.96 | 0.51 | -0.27 | 0.67 | 18571 @ -6.09 | 78290 @ -1.84 |
| c0 pre1 post1 | 0.69 | 112.99 | -6.02 | 15.93 | 9.931 | 0.73 | 0.14 | 0.30 | +2.25 | 473125 | +4.48 | 0.52 | -0.27 | 0.67 | – @ – | 78290 @ -1.84 |
| c0 pre1 post2 | 0.62 | 100.73 | -6.02 | 14.12 | 10.04 | 0.68 | 0.14 | 0.28 | +2.10 | 449002 | +4.42 | 0.53 | -0.27 | 0.67 | – @ – | 78290 @ -1.84 |
| c0 pre1 post3 | 0.61 | 103.04 | -6.02 | 12.5 | 10.28 | 0.64 | 0.15 | 0.30 | +2.25 | 428071 | +4.98 | 0.54 | -0.27 | 0.67 | – @ – | 78290 @ -1.84 |
| c0 pre1 post1, c1 pre1 | 0.69 | 113.10 | -7.59 | 18.22 | 7.629 | 0.75 | 0.16 | 0.32 | +2.34 | 449348 | +4.39 | 0.52 | -0.40 | 0.71 | – @ – | 14896 @ -4.89 |
| pre1 post1 both | 0.96 | 113.10 | 15.21 | 24.28 | 7.214 | 0.93 | 0.21 | 0.42 | +3.20 | 400875 | +4.39 | 0.52 | +1.06 | 0.73 | – @ – | – @ – |
| c0 pre1 post2, c1 pre1 post1 | 0.89 | 100.77 | 15.25 | 22.11 | 7.452 | 0.86 | 0.22 | 0.40 | +3.08 | 376457 | +4.32 | 0.53 | +1.06 | 0.73 | – @ – | – @ – |
| c0 post2, c1 post2 | 0.73 | 98.85 | 4.43 | 17.39 | 7.802 | 0.73 | 0.15 | 0.30 | +2.35 | 438195 | +3.77 | 0.51 | +0.25 | 0.72 | 18853 @ -6.00 | 79580 @ -1.85 |

Confirms the 17:35 switch: 'pre1 post1 both' (LIVE since 17:35) still leads, score 0.96 vs 0.67 for c0 post1; no further switch. Note: the 'Live 2Gpt' row now mixes 2Gpt and 5Gpt rounds.

## 2026-10-23 18:00 1304 rounds, class-1 balance (Avellaneda-Stoikov style skews) on the live pre1 post1

1304 rounds. Live 2Gpt (estimated): uid quality 0.32, realized +2.32 bps on 385982 notional

Live 2Gpt making (validator basis): c0 16.7, c1 5.275; index = 0.75 x c0 + 0.25 x c1 making, each relative to the best variant

score = 0.5 x making index + 0.5 x PnL index (PnL = est. notional x realized bps, the skill proxy); each index = 0.75 x c0 + 0.25 x c1 relative to the best variant

| Variant | score | PnL c0 | PnL c1 | making c0 | making c1 | index | uid q clamped | uid q | uid realized | notional | c0 realized | c0 buy share | c1 realized | c1 buy share | c0 0-1s | c1 0-1s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| LIVE pre1 post1 | 0.97 | 115.38 | 17.97 | 25.84 | 7.953 | 0.97 | 0.21 | 0.40 | +3.04 | 437947 | +4.17 | 0.52 | +1.11 | 0.74 | – @ – | – @ – |
| + c1 fill_skew 0.6 | 0.97 | 115.38 | 17.97 | 25.84 | 7.953 | 0.97 | 0.21 | 0.40 | +3.04 | 437947 | +4.17 | 0.52 | +1.11 | 0.74 | – @ – | – @ – |
| + c1 fill_skew 1.0 | 0.97 | 115.38 | 17.97 | 25.84 | 7.953 | 0.97 | 0.21 | 0.40 | +3.04 | 437947 | +4.17 | 0.52 | +1.11 | 0.74 | – @ – | – @ – |
| + c1 fill_skew 0.6 conf 1 | 0.97 | 115.38 | 17.97 | 25.84 | 7.953 | 0.97 | 0.21 | 0.40 | +3.04 | 437947 | +4.17 | 0.52 | +1.11 | 0.74 | – @ – | – @ – |
| + c1 fill_skew 1.0 conf 1 | 1.00 | 117.22 | 19.07 | 26.74 | 8.226 | 1.00 | 0.21 | 0.40 | +3.00 | 454529 | +4.09 | 0.52 | +1.14 | 0.73 | – @ – | – @ – |
| + all fill_skew 0.6 | 1.00 | 117.22 | 19.07 | 26.74 | 8.226 | 1.00 | 0.21 | 0.40 | +3.00 | 454529 | +4.09 | 0.52 | +1.14 | 0.73 | – @ – | – @ – |
| + skew 8 (inventory) | 0.90 | 116.67 | 19.41 | 20.96 | 7.177 | 0.81 | 0.17 | 0.38 | +2.44 | 558331 | +2.99 | 0.54 | +1.16 | 0.70 | – @ – | – @ – |
| + skew 8, c1 fill_skew 0.6 | 0.90 | 116.76 | 19.41 | 20.95 | 7.172 | 0.81 | 0.17 | 0.38 | +2.44 | 558635 | +2.99 | 0.54 | +1.16 | 0.70 | – @ – | – @ – |

Not testable in replay: the replay withholds the candidate's own fill notices (fill balance stays 0) and swaps in only its orders, not its inventory, so fill_skew / skew react to the LIVE account, not the candidate's. Identical rows confirm it. No change to live.

## 2026-10-23 18:03 cycle (state_tap multi-frame fix, 25 ms arrival)
1453 rounds. Live 2Gpt (estimated): uid quality 0.32, realized +2.30 bps on 410595 notional

Live 2Gpt making (validator basis): c0 18.63, c1 5.769; index = 0.75 x c0 + 0.25 x c1 making, each relative to the best variant

score = 0.5 x making index + 0.5 x PnL index (PnL = est. notional x realized bps, the skill proxy); each index = 0.75 x c0 + 0.25 x c1 relative to the best variant

| Variant | score | PnL c0 | PnL c1 | making c0 | making c1 | index | uid q clamped | uid q | uid realized | notional | c0 realized | c0 buy share | c1 realized | c1 buy share | c0 0-1s | c1 0-1s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4Gpt | 0.56 | 103.87 | -12.44 | 16.36 | 9.11 | 0.59 | 0.10 | 0.19 | +1.39 | 658266 | +2.85 | 0.50 | -0.42 | 0.66 | 55826 @ -3.80 | 109070 @ -2.40 |
| live: pre1 post1 | 0.90 | 117.77 | 19.52 | 27.4 | 8.636 | 0.86 | 0.21 | 0.39 | +2.88 | 477628 | +3.96 | 0.51 | +1.08 | 0.73 | – @ – | – @ – |
| pre1 post1, c0 only | 0.63 | 117.77 | -12.44 | 16.21 | 11.81 | 0.65 | 0.12 | 0.25 | +1.78 | 591660 | +3.96 | 0.51 | -0.42 | 0.66 | – @ – | 109070 @ -2.40 |
| pre1 post2 | 0.86 | 105.62 | 23.47 | 27.55 | 6.061 | 0.81 | 0.24 | 0.43 | +3.31 | 390890 | +3.91 | 0.52 | +1.95 | 0.76 | – @ – | – @ – |
| pre1 post3 | 0.79 | 105.25 | 19.33 | 25.01 | 4.847 | 0.72 | 0.25 | 0.49 | +3.81 | 327741 | +4.29 | 0.52 | +2.34 | 0.74 | – @ – | – @ – |
| pre1 post1, c1 post3 | 0.90 | 117.77 | 19.33 | 30.52 | 4.448 | 0.84 | 0.23 | 0.46 | +3.61 | 380340 | +3.96 | 0.51 | +2.34 | 0.74 | – @ – | – @ – |
| pre1 post1, c1 post3 1-sd | 0.73 | 119.25 | 8.53 | 22.09 | 3.97 | 0.63 | 0.17 | 0.33 | +2.50 | 514300 | +3.96 | 0.51 | +0.40 | 0.89 | – @ – | 67109 @ -2.11 |
| pre2 post1 | 0.89 | 115.17 | 19.67 | 26.77 | 9.043 | 0.85 | 0.20 | 0.38 | +2.80 | 483443 | +3.87 | 0.51 | +1.06 | 0.73 | – @ – | – @ – |

## 2026-10-23 18:27 monitor cycle (live 6Gpt since 18:22)
1817 rounds. Live 2Gpt (estimated): uid quality 0.31, realized +2.22 bps on 459025 notional

Live 2Gpt making (validator basis): c0 21, c1 3.917; index = 0.75 x c0 + 0.25 x c1 making, each relative to the best variant

score = 0.5 x making index + 0.5 x PnL index (PnL = est. notional x realized bps, the skill proxy); each index = 0.75 x c0 + 0.25 x c1 relative to the best variant

| Variant | score | PnL c0 | PnL c1 | making c0 | making c1 | index | uid q clamped | uid q | uid realized | notional | c0 realized | c0 buy share | c1 realized | c1 buy share | c0 0-1s | c1 0-1s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 5Gpt pre1 post1 | 0.65 | 130.73 | 27.26 | 33.83 | 10.63 | 0.59 | 0.22 | 0.39 | +2.77 | 570783 | +3.78 | 0.51 | +1.21 | 0.74 | – @ – | – @ – |
| live 6Gpt quiet x2/x1.25 | 0.85 | 183.96 | 29.84 | 50 | 11.51 | 0.78 | 0.23 | 0.40 | +2.88 | 742767 | +3.61 | 0.52 | +1.28 | 0.74 | – @ – | – @ – |
| 6Gpt quiet x2/x1 | 0.83 | 183.96 | 27.26 | 49.6 | 11.01 | 0.76 | 0.22 | 0.40 | +2.88 | 734996 | +3.61 | 0.52 | +1.21 | 0.74 | – @ – | – @ – |
| 6Gpt quiet x3/x1.25 | 0.99 | 202.84 | 29.84 | 71.43 | 10.43 | 0.98 | 0.20 | 0.33 | +2.42 | 963062 | +2.78 | 0.51 | +1.28 | 0.74 | – @ – | – @ – |
| 6Gpt x2/x1.25, c1 post3 | 0.79 | 183.96 | 26.35 | 54.22 | 5.4 | 0.69 | 0.25 | 0.46 | +3.42 | 614402 | +3.61 | 0.52 | +2.52 | 0.77 | – @ – | – @ – |
| 6Gpt x2/x1.25, pre2 | 0.83 | 177.22 | 29.65 | 48.68 | 11.16 | 0.75 | 0.22 | 0.39 | +2.81 | 736398 | +3.52 | 0.52 | +1.28 | 0.74 | – @ – | – @ – |

## 2026-10-23 19:00 survey: ArbitrageAgent-style taking (edge x fair_alpha x cross_frac), 2200 recorded rounds
Taking the touch when it stands `edge` through an EMA(mid, fair_alpha) and marking at the mid 20 s later loses about the
edge on class 0 (alpha 0.05: -6 / -20 / -41 / -73 bps at edge 10 / 20 / 40 / 80) - the move continues; class 1 almost never
triggers. Only tiny samples (alpha 0.2-0.5, edge 80) are positive. With the current class 0 taker REBATE (~25-50 bps,
MTR well above 0.4) the take is wealth-positive, but de-beta skill counts MTM only (debeta.py carries Mf/Tf without
scoring them), so for the score it would lose skill alpha. The next round's touch is within 5 bps of the trigger price
82-98% of the time, so cross_frac 5 bps suffices; it must stay well below edge (capture ~ fair x (edge - cross_frac)).
Decision: no taker leg. The useful part is the reverse: a price that just ran through its lagged EMA keeps running,
which is the side a maker gets run over on (supports 7Gpt's toxic-flow edge; a momentum input fit is next).
