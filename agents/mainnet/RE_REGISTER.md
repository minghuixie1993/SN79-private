# Re-registering the SN79 miner after a pruning

Written 25 Oct 2026 for UID 8 (hotkey 5CMTnm7J, immunity ends ~12:47 UTC = 13:47 CET). The coldkey is password
encrypted, so only the owner can run step 2.

## 1. Check whether the uid was pruned

    python agents/mainnet/track_uid.py --network mainnet      # "is not registered" = pruned

`agents/data/real_track/miner_check.log` (pm2 `miner-check`, every 30 min) shows the same.

## 2. Register again (owner's terminal; asks for the coldkey password)

Coldkey balance must cover the burn: 0.0775 TAO at 10:10 UTC on 25 Oct, rising during the morning - keep >= 0.1 TAO.

    btcli wallet balance --wallet-name redpig-coldkey --network finney
    btcli subnets register --netuid 79 --wallet-name redpig-coldkey --hotkey redpig-miner --network finney

`register` shows the current registration cost and asks to confirm before it spends. (Not `btcli subnets burn-cost`:
that is the cost of creating a new subnet.) The track_uid line in miner_check.log also prints "burn ... TAO".

The same hotkey gets a new uid; the miner's data folder follows the uid (agents/data/<uid>/).

## 3. Restart the miner so it serves its axon under the new uid (the Claude session does this)

At reseed phase 1-2 (scratchpad phase_now.py prints "phase seconds-since-log-write"; after a pruning the miner gets no
states, so its phase is stale - checked 25 Oct: 15768 s old - and the phase wait is pointless: restart at once when the
second number is > ~60), with the live params from
`~/.claude/projects/-home-Trustvs-workspace-sn-79/memory/sn79-mainnet-miner-tools.md` (newest "LIVE since" line):

    # the agent keeps its learned state (class spread multipliers, markout averages, per-book inputs) in
    # agents/data/<uid>/MyAgent10_23_6Gpt_state.json (output_dir follows the uid): carry it to the new uid's folder, or
    # it cold-starts. It is only restored for the same simulation and <= 3600 sim s (~4 wall h) behind.
    mkdir -p agents/data/<new uid> && cp agents/data/8/MyAgent10_23_6Gpt_state.json agents/data/<new uid>/
    python agents/mainnet/rotate_agent_csv.py
    PM2_HOME=~/.pm2-trustvs python agents/mainnet/recorder_on.py --max-gb 30 --agent MyAgent10_23_9Gpt --agent-params "<live params>"

## 4. Verify

- pm2 `miner` online; the log shows the new uid, "restored learned state", PLAN CARD placed > 0, rejected ~0.
- `track_uid.py`: registered, axon served, immunity left ~7200 blocks.
- The first hours decide the next survival: a new uid starts from the previous occupant's incentive, then the
  12.7 h track-record EMA (review #20 in agents/data/replays/shield_grid_log.md).

## Start set for the next uid (25 Oct 20:40 CET)

Final live set (05:11 CET 25 Oct) plus the grid_c0size3 winner, c0 size 0.024 + cap 0.06 (vs the final set: R +70%, 5-95%
+95 .. +147, making_vb 190.9 vs 108.4; vs c0 0.018 + cap 0.045: R +30%; c0 markout unchanged +1.20; PBO 0.00;
shield_grid_log.md free-time #40 / #42):

    event_lookback_minutes=1 latency_s=1.05 shield_pre_s=3 shield_post_s=1 shield_post_s_c1=5 shield_post_s_c2=5
    quiet_size_mult_c0=2.5 quiet_size_mult_c1=1.25 quiet_size_mult_c2=1.6 skew_c1=8 max_inv_ratio_c1=0.06
    half_spread_c0=0.0004 size_fraction_c0=0.024 size_fraction_c1=0.024 size_fraction_c2=0.024 max_order_fraction=0.06
    coef_fund_c2=0.004 pace_burst=8 adapt_max=1.0 max_lean=0.75 pending_aware=1 fill_skew_c0=0.6 fill_skew_c2=0.6

(fill_skew 0.6 on c0 / c2: two-sided capture +12.7%, band +14.8 .. +45.2, PBO 0.00, budget-adjusted day making +12.7% -
shield_grid_log.md 26 Oct 02:40, replay with own fill notices.)

Fallback (the set UID 8 ended on): the same with size_fraction_c0=0.012 max_order_fraction=0.03. Roll back to it on the
SPRT (c0 clamped baseline +0.68, sd 2.79, delta 0.3) or if published c0 net alpha turns clearly negative - a bigger c0
quote scales c0 alpha both ways. Check grid_c0size3 (scratchpad grid_c0size3.md) first: if c0 0.024 + cap 0.06 also passes,
it may be the better start.

Volume budget (review #43): the validator drops a book's orders past 500,000 QUOTE (10 x wealth) per rolling sim day. At
c0 0.024 a c0 book trades ~39k per sim hour -> the budget lasts ~12.8 sim h (~53 wall h), past the 24 h immunity. After
immunity, lower size_fraction_c0 toward 0.018 or set pace_burst ~2 so the budget is spread. Do not use 0.036.

**Correction (25 Oct 22:15 CET, see shield_grid_log.md)**: the cap is 450k per book per ROLLING 24 sim h. UID 8's final set
(c0 0.012) already traded ~20k per sim hour = the sustainable rate. c0 0.024 is a front-load for the immunity window only:
keep it through the immunity end (the 3 h-sim score window and the ~2 tempo commit-reveal lag mean a step-down before
the deadline costs score exactly when it counts; at 0.024 the day stop arrives only at ~48 wall h), then step back to
size_fraction_c0=0.012 max_order_fraction=0.03 (or pace_burst ~1.5) right after it, or the c0 books pause for most of the
following sim day.


**Measured 26 Oct 13:05 CET (shield_grid_log free-time #75)**: sim time runs at ~0.208 x wall time, so the rolling 24 sim h
window is ~115 wall h. At c0 0.024 the median c0 book trades ~29k per sim h (1.56x the sustainable 18.75k), the busiest
~87k; at 0.012 UID 8 traded ~20k. The post-immunity step-down to 0.012 / 0.03 stays the plan.
