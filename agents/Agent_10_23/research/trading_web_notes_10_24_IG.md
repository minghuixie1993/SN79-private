# Web research for MyAgent10_24IG, 24 Oct 2026 (evening)

This follows `trading_web_notes_10_24.md` and does not repeat it (AS/GLFT, microprice, Albers et al. fill-vs-markout,
markout curves, Hawkes widening, Sharpe objective, FCN background).
Collected 19:00-19:45 UTC 24 Oct. Nothing was run here: no simulator, validator, replay or pm2 commands. The only
network use was read-only HTTP GETs to public pages and to the public Grafana/VictoriaMetrics API at
taos.simulate.trading, the same endpoint `agents/network/collect_real.py` reads.

Each finding gives the source, the claim, what to change in MyAgent10_24IG (a parameter or mechanism, with a
suggested default), and a confidence rating.

---

## A. Upstream / validator status (things that can invalidate tuning)

### A1. No new upstream release; testnet gives no preview
- Source: https://api.github.com/repos/taos-im/sn-79/commits , /branches , /pulls , /issues , /releases
- Claim: `main` and `test` are both at `c36caa9` (23 Oct, "0.6.5 final"), with no GitHub releases. The open PRs
  #3 (LazyLevels slicing) and #4 (`_cfg_bool` floats) and issue #5 (activity factors in min_lookback) are from
  August and are unmerged. The public Grafana `neuron_info_info` labels for the testnet validator (netuid 366,
  sim 20261021_1609) and the mainnet validator (netuid 79, sim 20261024_0310) have **identical scoring and fee
  config**, so testnet does not preview any upcoming change.
- Live mainnet dials (Grafana labels, 24 Oct), all equal to the local 0.6.5 defaults:
  `debeta_weight 1.0`, `w_make 0.5`, `making_basis realized`, `making_horizon_s 20`, `centered_window 15`,
  `making_fee_weight 0.005`, `p11_strength 1.0`, `s3_k 4.0`, `skill_hurdle_bps 2.3`,
  `skill_min_books_share 0.15625`, `capital_turnover_cap 10`, **`max_instructions_per_book 5`**,
  `kappa_lookback 3 h` (this is also the de-beta window), `publish_interval 1 s`, `fp_seed_interval 30 s`,
  `fp_sigma_jump 0.025`. Background STA agents have `decision_latency_mean 10 s` (std 1 s) and
  `fundamentalist_weight 2.9`. HFT agents have feed latency >= 100 ms and order latency 1-50 ms.
- Correction to the earlier notes, section 1: the per-book instruction cap is **5** on mainnet, not 100.
  Cancels count against it. `dropped` = 0 for UID 8 today, so the cap does not bind yet. Any change that adds
  orders per book (for example a second quote level per side, or a take plus a requote in the same round) must
  keep the total at 5 or fewer instructions per book per reply.
- Mapping: the sim runs for 24 h, so the next simulation boundary is about 03:10 UTC on 25 Oct. Dials change at
  boundaries ("raised at later boundaries", see A2). Re-pull `neuron_info_info` after each boundary; the query is
  in section D.
- Confidence: high, read directly from the published labels.

### A2. A step-up of `making_fee_weight` is announced, and makers currently pay fees on 95% of books
- Source: `taos/im/config/__init__.py`, help text of `--scoring.debeta.making_fee_weight`: "Stepped: 0.005 first,
  raised at later boundaries as maker fees fall". Live per-book `book_gauges` on Grafana
  (`dynamic_maker_rate`, `dynamic_taker_rate`, `maker_taker_ratio`, 128 books, 24 Oct 19:40 UTC).
  Formula: `simulate/trading/src/cpp/matching/DynamicFeePolicy.cpp`.
- Claim: the dynamic fee is set per book from the **miners'** passive / (passive + aggressive) volume ratio x
  over the last 300 trade slots, with target 0.4 and a quadratic shape.
  - If x > 0.4, makers pay `0.015 * ((x - 0.4) / 0.6)^2` and takers receive the same amount as a rebate.
  - If x < 0.4, makers get a rebate of `0.015 * ((0.4 - x) / 0.4)^2`.
  Live: x has median 0.59 and q75 0.71. **121 of 128 books charge makers.** The maker rate has median 15.5 bps,
  q75 40 bps and max 109 bps. Takers are rebated (median -12.8 bps).
  UID 8 captures about 1.08 bps of spread per unit of two-sided maker notional, and the top makers 0.5-1.0 bps.
  If the weight goes to only 0.05, the median book's fee nets 0.78 bps per notional, which is about all of the
  captured spread. A weight of 0.25 or more makes making negative on most books.
- Mapping (prepare now, switch when the label changes):
  - Keep the parent's `fee_weight` in step with the validator's dial; today it is 0.005. Pass the new value in
    --params the same hour the label moves.
  - Add a per-book fee gate: quote a book only while `fee_weight * maker_fee_rate < fee_gate_frac *
    expected_capture`. Suggested `fee_gate_frac` 0.5, with expected capture taken from the agent's own
    per-class markout record (about 1 bp).
  - Move size toward books with low x (rebate books). `account.fees.maker_fee_rate` already gives the agent the
    per-book dynamic rate.
  - With the current 0.005 weight the fee is irrelevant to the score, but it drains wealth: about 15 bps on
    every maker fill. Check the agent's `stats["fees"]` line before scaling volume (B1).
- Confidence: high on the mechanism and the current rates. The timing and size of the step are unknown.

### A3. A change of the skill mark is being measured live, and it would flip UID 8's skill negative
- Source: config `--scoring.debeta.mark_mode_alt` (default `mid`): "a second mark accumulated beside the paid
  one, its skill published per uid ... so a change of mark is judged on live data before it pays". Paid
  `mark_mode` = `last`. Live Grafana `debeta_skill_mark_alt` and `debeta_skill_net_alpha_mark_alt`.
- Claim (live, 3 h window):

  | uid | paid skill | paid net alpha | mid-mark skill | mid-mark net alpha |
  |---|---|---|---|---|
  | **8 (ours)** | **+0.117** | **+119** | **-0.048** | **-61** |
  | 222 | 5.68 | 1.14e4 | n/a | 1.71e4 |
  | 234 | 5.82 | 1.16e4 | n/a | 1.72e4 |
  | 44 | -0.17 | -2.6e3 | n/a | -5.1e3 |
  | 58 | -0.19 | -2.7e3 | n/a | -5.0e3 |

  222 and 234 are pure takers, 44 and 58 are top makers. A mid mark rewards takers more and punishes makers
  more. For a maker, the last print usually sits at its own quote, which flatters the inventory mark-to-market.
- Mapping: our skill rank (106 of 127 positive) would fall to 0 if the mark switches. Score IG variants offline
  under both marks (`mark_mode` mid in the local validator / replay scorer) and prefer the ones that are
  positive under both. Likely levers:
  - a stronger fundamental lean (`coef_fund_c*`), so inventory points the way the mid will move,
  - `inv_gamma` 1 -> 1.5-2, which holds less inventory, so the mark matters less.
- Confidence: medium-high that the mark is under evaluation; unknown whether it will ship.

### A4. A liquidity-at-the-touch making rule is being measured live
- Source: `taos/im/validator/trade.py` comment: "The liquidity a maker provides (0.6.5, read before a making rule
  pays for it): maker-side notional of its fills by side, the states it had an order resting at the best bid or
  ask, and the size resting there ... published, never paid." Live gauges `debeta_at_best_seconds`,
  `debeta_best_size` and `debeta_maker_notional_two_sided`.
- Claim (live, 3 h window):

  | | at_best (states x books) | best_size | two-sided maker notional |
  |---|---|---|---|
  | UID 8 (ours) | 18.0k | 0.79M | 0.68M |
  | top-20 makers | median ~165k (140k-190k) | ~2.3e7 | 8-11.6M |

  UID 8 is about 9x below the top makers on time at the touch and about 30x below on size at the touch. The
  reseed shield alone makes the agent dark for 4-8 s of every 30 s (13-27% of the time).
- Mapping:
  - Add `states_at_best` and `size_at_best` per class to the score card now, so a rule switch can be judged.
  - Hedge for the rule: an "anchor" order of `min_quantity` kept at the touch on the safe side through the
    shield's post window (see B3). Suggested `anchor_qty` = the class minimum, off by default.
- Confidence: medium. A rule is clearly planned, but its form is unknown.

---

## B. Where UID 8 stands against the field (public Grafana, mainnet, sim 20261024_0310, 3 h window)

257 uids; 154 have positive making and 127 positive skill. **UID 8: score 0.00035, making rank 143 of 154,
skill rank 106 of 127.** The top score is 0.023.

### B1. Making is limited by volume, not by quality
| | making | realized / captured | captured per notional | maker notional (2-sided) |
|---|---|---|---|---|
| UID 8 | 22 | 0.34 | 1.08 bps | 0.68M |
| making median | 107 | n/a | n/a | n/a |
| top makers (51, 29, 44, 58, 4) | 270-381 | 0.36-0.40 | 0.72-0.85 bps | 10.3-11.6M |
| uids 132, 197, 36 | 187-223 | 0.50-0.52 | ~0.5 bps | ~9.5M |

- Quality and capture per notional are already as good as the leaders'. **The gap is about 15x in volume.** The
  making pool pays the realized spread, so volume is the lever.
- Mapping (IG parameters):
  - `size_fraction` x3 (c0 0.036, c1 and c2 0.072),
  - `max_order_fraction` 0.03 -> 0.06, so the bigger sizes are not cut back to the old cap,
  - keep `quiet_size_mult`,
  - check that the 24 h volume cap does not bind: `capital_turnover_cap` 10 x miner wealth 50k gives
    `activity_max_volume` 500k, the published value; the agent's pacing reads it.
  - Run first on the replay grid. Watch quality: the leaders hold 0.36-0.40 at 15x our volume, so quality should
    stay above 0.3.
- Expected effect: making 22 -> roughly 80-150 (making rank about 143 -> 60-90). This is the largest gain that
  needs no new mechanism.
- Confidence: medium-high on the direction. The size of the gain depends on whether fill rate scales with size
  at the touch.

### B2. The skill pool is held by pure takers
- The four top scores (uids 234, 9, 222, 225; score 0.016-0.023, **66x ours**) all have making 0, skill 3.2-5.8,
  net alpha **6.4 bps per notional** on 13-18M notional across ~105 books (hurdle 2.3 bps), and only ~15-25
  placements per counter window. The next skill tier (207, 90, 148, 203, 56, 209, 14, 1, 185) is the same:
  makers of nothing, takers with 6-14 bps alpha per notional.
- Interpretation (inferred from the config, not verified on prints): after each 30 s reseed the validator sends
  the new fundamental. The background fundamentalists move the price toward it only as they wake (decision
  latency ~10 s), so a taker that trades toward the new fundamental within the first seconds earns the drift.
  Takers also collect the dynamic taker rebate (A2). These are the same flows that pick off stale maker quotes
  just after a reseed.
- Mapping: IG already inherits `_take` (IovC, `take_edge_bps` 0 = off). Proposed reseed-gap take (new IG params):
  - fire only within `take_window_s` (suggest 0.5-8 s) after a reseed boundary,
  - predicted move = `take_beta_c<k>` x (F_new - F_prev) / mid. Fit beta per class on the recordings; start at
    0.5 for c0 and c1, and use the c2 fundamental-lean coefficient for c2, where the persistent ~600 bps gap
    makes the level unusable.
  - take when the predicted move minus half the spread exceeds `take_edge_bps`; suggested 3, above the 2.3 bps
    hurdle,
  - `take_cooldown` 20 -> 5 s, `take_share` 0.25 -> 0.5,
  - the taker-fee gate already passes, since the taker rate is negative,
  - keep it at 5 or fewer instructions per book.
- Expected effect: the largest possible gain (skill is half the trading pool, and the taker uids hold most of
  it), but this is a new strategy leg.
- Validate on `agents/mainnet/replay_agent.py` with the one-round reply lag before going live: check the
  printed taker uids (222, 234, ...) for the timing of their fills relative to the reseed phase.
- Confidence: high that pure takers dominate skill; medium on the reseed-gap mechanism behind it.

### B3. Fill efficiency is 8x worse than the top makers
- Counters (same window): UID 8 placed 3,673, cancelled 2,781 and got 16 fills (0.4% per placement). Top makers
  (44, 58, 4, 51) placed about 1.4k, cancelled about 0.9k and got 34-52 fills (2.5-3.6% per placement).
- Literature:
  - Moallemi & Yuan, queue position valuation: an order's value has a static part (spread vs adverse
    selection, with adverse selection *increasing* with queue position) and a dynamic option part from holding
    the position.
  - Mounjid / Lehalle-type Glosten-Milgrom LOB model (arXiv 1902.10743): queue-position value is of the same
    order as the bid-ask spread.
  - The earlier note (section 4) supports the "keep queue" idea. What is new here is the size of the gap.
- Mapping:
  - (a) Every shield cancels both quotes, and the agent rejoins at the back of the queue each 30 s cycle.
    Instead, after the reseed, re-place the **safe side first**: the side that benefits from F_new - mid, i.e.
    the bid if F rose and the ask if F fell. Do it at the first post-reseed state, ahead of the makers who come
    back after 5 s, and bring the endangered side back only after `shield_post_s`. Suggested
    `shield_post_safe_s` 0 for the safe side; keep 1 / 5 / 5 s for the endangered side.
  - (b) Requote hysteresis at the touch on every class, not only class 1: `requote_ticks` 2, and replace on a
    toward-move only beyond 2 ticks.
  - (c) Class 0 `expiry_period` 60 s -> 300 s: a 60 s expiry sends the quote to the back of the queue every
    minute, the same reasoning the parent already applies to class 1.
- Confidence: medium-high. Fewer cancels with the same quotes cannot hurt making, and (a) is the IG version of
  the one-sided quoting below (C1).

### B4. P11 costs us 12%
- UID 8 has `p11_factor` 0.883 and `bg_share` 0.77; the top makers have 0.94-0.99. Our maker volume is
  concentrated on a few miner takers.
- Mapping: in situation sizing, add a per-taker axis. Where the top-2 miner takers make up more than 50% of a
  book's recent maker volume for us, scale that book's size by 0.7 (`p11_book_mult`), which moves volume toward
  books with background or mixed flow. It also lowers exposure to the toxic takers in B2.
- Confidence: medium. The gain is at most 12% of making.

---

## C. Literature ideas for the reseed-specific problems

### C1. One-sided quoting when the direction of the next move is known
- Sources:
  - Cartea & Wang, "Market making with alpha signals", IJTAF 23(3) 2020,
    https://ideas.repec.org/a/wsi/ijtafx/v23y2020i03ns0219024920500168.html
  - Wang, Ventre & Polukarov, "Robust Market Making: To Quote, or not To Quote", ICAIF 2023,
    https://arxiv.org/abs/2508.16588 . They add bid-only and ask-only actions; a 4-action agent trained in a low
    volatility regime still quotes two-sided at least 92% of the time in high volatility (arXiv 2508.16589).
- Claim: with a directional signal, the optimal maker uses it to cut adverse selection by skewing and, when the
  signal is strong, by withdrawing the side the move runs through, while keeping the other. Strategies that may
  quote one side do better than always-two-sided ones under adversarial flow.
- Mapping: the post-reseed signal F_new - F_prev is the strongest signal the agent ever has and is known one
  state after the boundary.
  - Replace the symmetric `shield_post_s` with a clamp: bid <= min(planned bid, P* - h_f) and
    ask >= max(planned ask, P* + h_f).
  - P* = mid + beta_c x (F_new - F_prev), with beta_c fitted as in B2.
  - `shield_clamp_bps` h_f = 1-2 bps.
  - This is B3(a) stated as a price rule. The pre-reseed window (direction unknown) stays a full shield.
- Confidence: medium. The theory is solid; beta_c needs fitting on the recordings.

### C2. Sniping cost sets how wide a quote must be to stay through a known-time jump
- Sources:
  - Budish, Cramton & Shim, "The HFT arms race", QJE 2015, https://conference.nber.org/conf_papers/f69665.pdf
  - Aquilina, Budish & O'Neill, QJE 2022, https://www.nber.org/papers/w29011 and
    https://www.fca.org.uk/publications/occasional-papers/occasional-paper-no-50-quantifying-high-frequency-trading-arms-race-new-methodology
- Claim: a stale quote is sniped whenever a public jump exceeds its distance from the new value. The competitive
  half-spread solves `lambda_noise * h = lambda_jump * E[(J - h)+]`. Empirically, races are about 1/3 of the
  effective spread and happen about once a minute per symbol. Each race is worth about half a tick, and the
  liquidity provider's cancel attempt loses by microseconds.
- Mapping: on SN79 the jump time is known (every 30 s) and the agent's reaction lag is one 1 s round, so the
  pre-reseed window is a scheduled race the agent always loses. Use the formula to decide *dark vs wide* per
  class instead of grid-searching `shield_pre_s`:
  - J = |beta_c x dF| per reseed. Its distribution comes from `fp_sigma_jump` 0.025, but measure it on the
    recordings, since the jump enters the price only through the agents.
  - lambda_noise = the background fill rate at the touch in the 3 s before the reseed.
  - If the solved h* is at most `shield_wide_max` (3x) the normal half-spread, quote at h* during `shield_pre_s`
    rather than go dark. This helps time at the touch (A4) and captured spread (wide fills earn the most spread
    per fill). Otherwise stay dark, as now.
  - New params: `shield_mode` (dark | wide), `shield_wide_max` 3.0.
- Confidence: medium-low that "wide" beats "dark" in the pre-window (the jump sigma looks large compared with
  the spread). It costs one replay grid to check.

### C3. Per-counterparty toxicity scoring
- Source: Cartea, Duran-Martin & Sánchez-Betancourt, "Detecting toxic flow" (2023), https://arxiv.org/abs/2312.05827
- Claim: per-client online Bayesian toxicity prediction (PULSE) beats logistic regression, random forests and a
  recursive MLE. The broker routes toxic clients' trades away and keeps the rest; this gave the highest PnL and
  took under 1 ms per update.
- Mapping: the agent sees the taker id on each print (IovC `_prints`), and the validator's P11/S3 work from the
  same ids.
  - Keep a decayed Bayesian clamped-markout estimate per taker id, using the same machinery as situation sizing.
    The prior is the class prior, with `prior_n` 20.
  - When a taker whose lower confidence bound is below -1 bp has printed on the book within the last
    `toxic_taker_window_s` (5 s), scale that side's size by 0.5, or widen by `book_mult_max`.
  - This targets the B2 takers directly and helps P11 (B4).
- Confidence: medium. The effect depends on how strongly toxic takers' activity clusters in time.

### C4. Latency makes reactive imbalance signals worth less than scheduled ones
- Source: Lehalle & Mounjid, "Limit order strategic placement with adverse selection risk and the role of
  latency", https://arxiv.org/abs/1610.00261 ; Gao & Wang, https://arxiv.org/abs/1806.05849
- Claim: imbalance-based cancel/replace has value only if the maker can act before the flow arrives. Latency
  removes much of that value and is itself a risk that lowers the maker's profitability.
- Mapping: with a one-round reply lag, put modelling effort into clock-based features (reseed phase, dF, the
  STA wake-up window ~10 s) rather than reactive imbalance cancels. In practice: do not add new imbalance-cancel
  logic, and give the 5-10 s post-reseed STA convergence window its own situation bucket in the situation sizing
  (`post_window_s` 5 -> a second bucket at 5-12 s).
- Confidence: medium-low. This is a prioritisation hint, not a change.

### C5. Fills whose 20 s markout spans the next reseed (derived from the live config, not from literature)
- The making mark is taken `making_horizon_s` = 20 s after the fill. A fill at phase phi > 10 s into the cycle
  is marked after the next reseed jump, so its realized spread carries the jump noise and is clamped to +/- its
  captured spread. Symmetric noise under a symmetric clamp pulls the expected realized spread toward 0.
- Mapping: split the quiet window in the score card and the situation sizing into 5-10 s (horizon inside the
  cycle) and 10-27 s (horizon across the reseed). If the late part marks out worse, cut `quiet_to_s` 28 -> about
  12, or put the size multiplier only on 5-12 s. The 5Gpt phase markouts (+2 to +5 bps over 5-28 s) do not
  separate the two parts.
- Confidence: medium-low until it is measured.

---

## D. Monitoring queries (public Grafana, read-only)

Base URL: `https://taos.simulate.trading/api/datasources/proxy/uid/c7665619-8b04-4227-9882-a44e8dad57a6/api/v1/query`
- Config at each boundary: `last_over_time(neuron_info_info[2h])`. Watch
  `config_scoring_debeta_making_fee_weight` and `config_scoring_max_instructions_per_book`. A `mark_mode` label is
  not published, so watch for a jump in `debeta_skill` toward `debeta_skill_mark_alt` across the field.
- Field table: `last_over_time(miner_gauges{netuid="79",sim_id="<sim>",miner_gauge_name=~"score|debeta_making|debeta_making_realized|debeta_making_captured|debeta_skill|debeta_skill_net_alpha|debeta_skill_net_alpha_mark_alt|debeta_at_best_seconds|debeta_best_size|debeta_maker_notional_two_sided|debeta_p11_factor|fills|placements_sent|cancels_sent|dropped|debeta_notional"}[30m])`.
  The uid is in the `agent_id` label.
- Per-book fees: `last_over_time(book_gauges{netuid="79",sim_id="<sim>",book_gauge_name=~"dynamic_maker_rate|dynamic_taker_rate|maker_taker_ratio"}[10m])`.
- Suggest adding these three to `agents/network/collect_real.py` (not done here).

---

## E. Ranked by expected score effect

1. **Reseed-gap taker leg** (B2): `take_edge_bps` 3, a 0.5-8 s post-reseed window, beta_c x dF, `take_cooldown` 5 s.
   The skill pool is held by pure takers at 66x our score.
2. **Volume x3** (B1): `size_fraction` c0 0.036 / c1 and c2 0.072, `max_order_fraction` 0.06. Quality already
   matches the top makers; volume is 15x short.
3. **Safe-side-first re-entry after the reseed, and the dF clamp** (B3a, C1): first back at the touch, fewer pick-offs.
4. **Fee-weight step readiness** (A2): per-book fee gate, size moved toward low-MTR books, `fee_weight` kept equal to
   the validator dial. Protects against a step-up that would wipe out making on 95% of books.
5. **Mid-mark robustness of skill** (A3): score variants under `mark_mode=mid`; our skill is negative there today.
6. **Queue keeping** (B3b, c): `requote_ticks` 2 on every class, class 0 expiry 300 s. Our fills per placement
   are 8x lower than the leaders'.
7. **Per-taker toxicity and P11** (C3, B4): size cut after toxic takers print, `p11_book_mult` 0.7.
8. **Dark vs wide pre-reseed by the BCS rule, and the phase split of the quiet window** (C2, C5): cheap replay
   checks, smaller expected effect.

## Sources
- https://github.com/taos-im/sn-79 , https://api.github.com/repos/taos-im/sn-79/commits , /branches , /pulls , /issues
- https://taos.simulate.trading (public Grafana API: neuron_info_info, simulation_config_info, miner_gauges, book_gauges)
- https://taos.im/ and https://taos.im/2025/12/02/dis/ (the Dynamic Incentive Structure; the paper's Proton
  Drive link could not be read, so the formula above is from `DynamicFeePolicy.cpp`)
- https://subnetalpha.ai/?p=1986 (roadmap: 40 -> 1000+ books, MVTRX exchange, bidirectional streaming; no dated
  validator change)
- https://ideas.repec.org/a/wsi/ijtafx/v23y2020i03ns0219024920500168.html (Cartea & Wang, alpha signals)
- https://arxiv.org/abs/2508.16588 , https://arxiv.org/abs/2508.16589 (to quote or not to quote; Hawkes variant)
- https://conference.nber.org/conf_papers/f69665.pdf (Budish, Cramton, Shim)
- https://www.nber.org/papers/w29011 , https://www.fca.org.uk/publications/occasional-papers/occasional-paper-no-50-quantifying-high-frequency-trading-arms-race-new-methodology
- https://web.mit.edu/orc/www/archive/seminars/2014s/abs/Moallemi_abs.html (queue position value)
- https://arxiv.org/pdf/1902.10743 (Glosten-Milgrom to the whole LOB; queue value ~ spread)
- https://arxiv.org/abs/2312.05827 (Detecting toxic flow)
- https://arxiv.org/abs/1610.00261 (strategic placement, adverse selection and latency)
- https://arxiv.org/abs/1806.05849 (market making with latency)
- https://papers.ssrn.com/abstract=1748633 (VPIN / exchange of flow toxicity; an alternative to the Hawkes
  intensity of the earlier notes, section 6)
- Community: no SN79-specific Discord summaries, X posts (x.com returned 402), Medium or Substack mining posts
  turned up in search.
