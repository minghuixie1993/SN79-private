# Trading research from the web — 24 Oct 2026

Collected for the Agent_10_23 market-making agents (live: MyAgent10_23_9Gpt).
Each section: what the source says, then what it could mean for our agent.

## 1. SN79 upstream status

- Upstream `taos-im/sn-79` main is `c36caa9` "20261023 - 0.6.5 final"; local is `c456c88` (0.6.5), one commit behind.
- That commit is validator-side only: per-miner Prometheus counters `placements_sent`, `cancels_sent`,
  `cancels_ok`, `cancels_failed`, `fills`, `dropped` (instructions dropped by the volume cap
  `capital_turnover_cap x miner_wealth` or by `max_instructions_per_book`), a fix so an undecompressable reply is
  refused for that miner alone, and passing `--neuron.reuse_connections` to the query process.
  No scoring or miner-interface change. FAQ.md is identical to local.
- Scoring unchanged from 0.6.3+: trading pool ~95% (GenTRX ~5%), two equal halves:
  **making** (spread captured by maker fills, judged by how much is still held shortly after) and
  **skill** (PnL with drift on held inventory removed); volume minimum for full payout; query timeout 3.0 s.

Use for us:
- Once validators run c36caa9, the public Grafana metrics will show **every miner's fills and cancels per block**.
  Extend `agents/mainnet/collect_real.py` to pull them: fills per request of top UIDs is a direct calibration target.
- Watch our own `dropped` counter: non-zero means the volume cap or the 100-instructions-per-book cap is cutting orders.

## 2. Inventory-aware quoting (Avellaneda–Stoikov, GLFT)

- AS reservation price `r = s - q*gamma*sigma^2*(T-t)`, half-spread `(1/gamma) ln(1 + gamma/kappa)`; kappa = fill
  intensity decay with distance. gamma sets both skew slope and spread width.
- Guéant–Lehalle–Fernandez-Tapia (2013) solve the full HJB (linear ODE system) under inventory limits; closed-form
  approximations exist. Near q=0 they match AS; **skew and spread grow non-linearly with |q|** in the tails.
- Hummingbot: infinite-horizon variant is the one suited to continuous markets.

Use for us: our skew is linear (`skew_c1=8`, `max_inv_ratio_c1`). A convex skew (e.g. `skew * q * (1 + a*|q|/q_max)`)
would match GLFT: little change when flat, faster unwind near the cap. Cheap offline A/B with replay_agent.py.

## 3. Short-term signals: imbalance and microprice

- Stoikov microprice = limit of expected future mids given top-of-book imbalance and spread; claimed better than mid
  and weighted mid. A Frankfurt replication (DAX40) found **no gain over mid / weighted mid at most horizons**.
- Queue imbalance predicts the next mid move (Gould & Bonart).
- Cartea–Jaimungal–Ricci: makers without short-term-alpha predictors are adversely selected out of the market.

Use for us: test the microprice vs weighted mid as our reference price per class on recorded mainnet books before
adopting; the evidence is market-dependent.

## 4. Fill probability vs. post-fill returns (Albers, Cucuringu, Howison, Shestopaloff, arXiv 2502.18625)

Binance BTC perp, live, fees taker 1.5 bp / maker -0.5 bp:

| Strategy | Mean return/trade (bp, net) |
|---|---|
| Imbalance taker | -1.96 |
| Naive maker (both touches) | -0.43 (lost ~60% in 3 days, Sharpe -109) |
| Imbalance maker, no cancel | -0.47 |
| Imbalance maker, with cancel | -0.49 |

- Fill probability ~50% with balanced queues; ~30% when own side is large and opposite small; >90% when own side
  tiny and opposite huge (OLS R^2 0.946).
- 1 s markout: **front-of-queue fills in a large own-side queue with a small opposite side are the only good case**
  (-0.06 bp); back-of-queue fills are adverse (-0.7 to -1.2 bp).
- "Reversals": imbalance falsely predicts the move; orders against imbalance fill and then win. Needs a model
  (volatility, volume, recent moves) to pick them; imbalance alone is not enough.

Use for us: the making half rewards spread kept after the fill, so this is the same trade-off.
- Prefer keeping existing resting orders (queue priority) over cancel/replace when the price is unchanged.
- Avoid joining the back of a thick queue on the side the book is leaning against us.

## 5. Adverse selection, markouts, toxicity

- Markout(h) = side x (mid(t+h) - fill price); report a **curve** (sub-second, 1–10 s, 1–10 min), weighted and
  median, split by side; handle stale quotes and gaps (KX).
- Toxic flow -> makers widen and cut size or pull quotes (CoinAPI glossary).
- "Price reading" (Barzykin, Bergault, Guéant, Lemmel, arXiv 2508.20225): your skew reveals your inventory to others.
- Raw markouts can mislead; control for drift/factors (arXiv 2605.30643). This is what the skill half does with drift.

Use for us: the 5Gpt phase markouts are already this; add per-class markout curves at 1, 5, 30, 300 s from
mainnet recordings and compare them to the making-half score.

## 6. Clustered order flow (Hawkes)

- Market orders are self- and cross-exciting (Bacry et al.; Jusselin arXiv 2003.05958).
- Tarditi (PoliMi 2026, HJB on (t, q, lambda+, lambda-)): **optimal quotes widen and skew more when Hawkes
  intensities and their imbalance are high, and return to tight symmetric quotes in calm regimes.**

Use for us: our reseed shield and quiet-size multipliers are a hand-made version. A continuous replacement:
exponentially decayed counts of recent buy/sell trades per book (lambda+, lambda-), widen by `k*(lambda+ + lambda-)`
and skew by `k2*(lambda+ - lambda-)`. Avoids hard on/off windows.

## 7. Objective: Sharpe vs PnL

- Higher inventory aversion can more than double Sharpe for ~5% less PnL (Fodra & Labadie, arXiv 1206.4810).
- Robust (ambiguity-averse) market making raises Sharpe (Cartea, Donnelly, Jaimungal).
- Caveat (arXiv 2606.01477): Sharpe barely moved across a wide penalty sweep while the true loss changed a lot;
  Sharpe alone is a weak tuning signal.

## 8. Simulator background (FCN agents)

- Standard agent-based books (Chiarella–Iori FCN): fundamental + chartist + noise forecasts with random weights,
  plus an AS-style passive market maker. Market makers cancel far more than others. Matches the SN79 background agents.

## Candidate experiments (offline, replay_agent.py on mainnet_records)

1. Convex inventory skew (GLFT shape) on class 1.
2. Microprice vs current reference price, per class.
3. Keep-queue rule: don't cancel/replace an order whose price is unchanged.
4. Decayed trade-intensity widening/skew instead of hard shield windows.
5. Markout curves per class at 1/5/30/300 s vs making-half score.

## Sources

- https://raw.githubusercontent.com/taos-im/sn-79/main/README.md
- https://api.github.com/repos/taos-im/sn-79/compare/c456c88...c36caa9
- https://subnetalpha.ai/subnet/%CF%84aos/
- https://deepwiki.com/taos-im/sn-79
- https://hummingbot.org/strategies/avellaneda-market-making
- https://themicrostructurelab.substack.com/p/microstructure-essentials-avellaneda
- https://arxiv.org/abs/1105.3115 (Guéant, Lehalle, Fernandez-Tapia)
- https://alphaxiv.org/abs/1605.01862 (Guéant, Optimal market making)
- https://www.ma.imperial.ac.uk/~ajacquie/Gatheral60/Slides/Gatheral60%20-%20Stoikov.pdf
- https://www.efinance.wiwi.uni-frankfurt.de/en/teaching/master-theses/selection-of-completed-theses/the-time-adjusted-micro-price-and-the-relevance-of-deeper-limit-order-book-levels.html
- https://ora.ox.ac.uk/objects/uuid:a43beae7-ed40-4848-b092-6aa1aef4ae96/files/m1969a15b75797755bb674aaeeb4db70b (Cartea, Jaimungal, Ricci)
- https://arxiv.org/html/2502.18625v2 (Market Maker's Dilemma)
- https://arxiv.org/html/2508.20225v2 (Adverse selection and price reading)
- https://arxiv.org/pdf/2605.30643 (Quality-adjusted hit ratio)
- https://kx.com/glossary/what-is-markout-and-how-is-it-calculated/
- https://www.coinapi.io/learn/glossary/order-flow-toxicity
- https://arxiv.org/pdf/2003.05958 (Jusselin, persistent order flow)
- https://arxiv.org/pdf/2201.10173 (extended Hawkes bid/ask)
- https://www.politesi.polimi.it/retrieve/9e004d66-6645-4c2a-9be3-0a3c730c13ac/2026_03_Tarditi_ExecutiveSummary.pdf
- https://arxiv.org/pdf/1206.4810 (Fodra & Labadie)
- https://ora.ox.ac.uk/objects/uuid:32248d98-3e1a-499a-ae2f-8cddd64beb4c (Robust market making)
- https://arxiv.org/pdf/2606.01477 (AS and CJ as one framework)
- https://arxiv.org/pdf/1907.11053 (Optimal make-take fees)
- https://arxiv.org/pdf/2402.17359 (LOB simulations review)
