# SPDX-License-Identifier: MIT
"""
Scores a recorded local proxy run with the validator's own code and stores every per-miner field the
agents dashboard shows.

The proxy records the raw simulator states (`"record_states": true` in the launcher config, see
agents/proxy/state_recorder.py). This script replays them exactly as the validator's scoring shadow does:
`trade.update_trade_volumes` every round, `scoring_shadow.shadow_score` (score_uids, the track-record EMA
and the making pool) at every `--scoring.interval` boundary, then `report.report_worker` for the dashboard
fields. Validator settings are the defaults of `taos/im/config`, which the published validator runs, except
`scoring.debeta.min_books` (an activation guard counting miners), lowered to 1 for a single-miner run.

Locally there is one real miner among idle remote slots, so the rank, share and score fields are not
comparable with mainnet; the raw legs (making, skill, net alpha, factors, books, notional, P&L, volumes) are.

Usage (from the repository root):
    python agents/eval/score_run.py agents/data/records/<run> --step 0 --note "baseline"
    python agents/eval/score_run.py agents/data/records/<run> --minutes 60     # score the first hour after grace
"""
import argparse
import copy
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents" / "proxy"))

# Fields copied into the scoreboard table, in column order: (miner metric key, column title, format).
SCOREBOARD_COLUMNS = [
    ("making_raw", "Making", "{:.4g}"),
    ("making_captured", "Captured", "{:.4g}"),
    ("making_realized", "Realized", "{:.4g}"),
    ("skill_raw", "Skill", "{:.4g}"),
    ("skill_net_alpha", "Net alpha", "{:.4g}"),
    ("coverage_books", "Coverage books", "{:.0f}"),
    ("skill_books_kept", "Skill books", "{:.0f}"),
    ("num_scored_books", "Scored books", "{:.0f}"),
    ("p11_factor", "P11", "{:.3f}"),
    ("s3_factor", "S3", "{:.3f}"),
    ("bg_share", "BG share", "{:.3f}"),
    ("notional", "Notional", "{:.4g}"),
    ("pnl", "PnL", "{:.2f}"),
    ("total_realized_pnl", "Realized PnL", "{:.2f}"),
    ("activity_factor", "Activity", "{:.3f}"),
]


# Per-boundary fields kept for every agent with --all-uids, and the ones averaged over the run.
BOARD_KEYS = ("score", "trading_score", "debeta_score", "making_raw", "making_captured", "making_realized",
              "making_share", "skill_raw", "skill_net_alpha", "skill_books_kept", "coverage_books", "p11_factor",
              "s3_factor", "notional", "pnl", "total_realized_pnl")
AVERAGED_KEYS = ("score", "trading_score", "debeta_score", "making_raw", "skill_raw")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("record_dir", help="Directory written by the proxy's state recorder.")
    parser.add_argument("--uid", type=int, default=0, help="Agent id to report (the first launcher agent is 0).")
    parser.add_argument("--minutes", type=float, default=None,
                        help="Stop after this many simulation minutes past the grace period (default: whole recording).")
    parser.add_argument("--step", default=None, help="Improvement step this run belongs to; appends a scoreboard row.")
    parser.add_argument("--note", default="", help="What changed in this step, for the scoreboard.")
    parser.add_argument("--scoreboard", default=str(REPO / "agents" / "eval" / "SCOREBOARD.md"))
    parser.add_argument("--out", default=None, help="Result directory (default: agents/eval/scores/<step or run>).")
    parser.add_argument("--all-uids", action="store_true",
                        help="Also write leaderboard.json: every recorded agent's final legs and averages over the run.")
    return parser.parse_args()


def build_validator_config():
    """The validator's default config, built without touching the chain or this script's own arguments."""
    argv, sys.argv = sys.argv, sys.argv[:1]
    try:
        from taos.common.neurons import BaseNeuron
        from taos.im.neurons.validator import Validator
        config = Validator.config()
        config.merge(copy.deepcopy(BaseNeuron.config()))
    finally:
        sys.argv = argv
    # The one local override. min_books is an activation guard only (see its --help): a cycle publishes
    # de-beta when at least this many MINERS score positive, default 4. Locally there is a single miner,
    # so at 4 the making/skill decomposition would never appear; at 1 it does, with every leg unchanged.
    config.scoring.debeta.min_books = 1
    return config


def load_simulation(xml_path):
    """The simulation layout exactly as the proxy loads it."""
    import xml.etree.ElementTree as ET
    from taos.im.protocol import MarketSimulationConfig
    from taos.im.protocol.models import MultiAssetSimulationConfig
    root = ET.parse(xml_path).getroot()
    if root.tag == "MultiAssetSimulation":
        return MultiAssetSimulationConfig.from_multiasset_xml(root, Path(xml_path).parent)
    return MarketSimulationConfig.from_xml(root)


def fresh_structs(uids, book_ids):
    """Empty validator histories, shaped as persistence.load_state builds them for a fresh validator."""
    from taos.im.validator.scoring_shadow import _rebuild_structs
    structs = _rebuild_structs({})
    structs["trade_volumes"] = {uid: {b: {'total': {}, 'maker': {}, 'taker': {}, 'self': {}} for b in book_ids}
                                for uid in uids}
    structs["inventory_history"] = {uid: {} for uid in uids}
    structs["initial_balances"] = {uid: {b: {'BASE': None, 'QUOTE': None, 'WEALTH': None} for b in book_ids}
                                   for uid in uids}
    structs["recent_trades"] = {b: [] for b in book_ids}
    structs["recent_miner_trades"] = {uid: {b: [] for b in book_ids} for uid in uids}
    structs["activity_factors"] = {uid: {b: 0.0 for b in book_ids} for uid in uids}
    structs["pnl_factors"] = {uid: {b: 1.0 for b in book_ids} for uid in uids}
    structs["kappa_values"] = {uid: {
        'books': {b: None for b in book_ids}, 'books_weighted': {b: None for b in book_ids},
        'total': None, 'average': None, 'median': None,
        'normalized_average': 0.0, 'normalized_median': 0.0, 'normalized_total': 0.0,
        'activity_weighted_normalized_median': 0.0, 'penalty': 0.0, 'score': 0.0,
    } for uid in uids}
    return structs


def make_shadow(config, simulation, uids):
    """A ShadowState over fresh histories, with the knobs main ships to the scoring child."""
    from types import SimpleNamespace
    from taos.im.validator.reward import build_scoring_config, build_simulation_config_dict
    from taos.im.validator.scoring_shadow import ShadowState
    book_ids = list(simulation.book_ids)
    effective_max_uids = max(uids) + 1
    # build_* read a validator; this carries just what they touch.
    host = SimpleNamespace(config=config, simulation=simulation, reward_cores=1, engine=None,
                           effective_max_uids=effective_max_uids)
    knobs = {
        "step": 0,
        "effective_max_uids": effective_max_uids,
        "config": config,
        "simulation": simulation,
        "scoring_config": build_scoring_config(host),
        "simulation_config": build_simulation_config_dict(host),
        "scoring_interval": config.scoring.interval,
        "validator_uid": None,
    }
    shadow = ShadowState(fresh_structs(range(effective_max_uids), book_ids), knobs)
    shadow.unnormalized_scores = {uid: 0.0 for uid in range(effective_max_uids)}
    return shadow


def parse_frame(raw):
    """Raw recorded bytes -> (flattened message dict, parsed state), as the proxy and the engine see them."""
    import msgpack
    from taos.im.protocol import MarketSimulationStateUpdate
    from taos.im.validator.multiasset_state import flatten_multiasset_state
    message = msgpack.unpackb(raw, raw=False, use_list=True, strict_map_key=False)
    message, _ = flatten_multiasset_state(message)
    return message, MarketSimulationStateUpdate.parse_dict(message)


def report_metrics(shadow, message, sim_ts, scores):
    """report.report_worker on the shadow's histories: the exact per-miner dict the dashboard gauges read."""
    from taos.im.validator.report import report_worker
    validator_data = {
        'simulation_timestamp': sim_ts,
        'step': shadow.step,
        'volume_sums': {uid: dict(b) for uid, b in shadow.volume_sums.items()},
        'maker_volume_sums': {uid: dict(b) for uid, b in shadow.maker_volume_sums.items()},
        'taker_volume_sums': {uid: dict(b) for uid, b in shadow.taker_volume_sums.items()},
        'self_volume_sums': {uid: dict(b) for uid, b in shadow.self_volume_sums.items()},
        'roundtrip_volume_sums': {uid: dict(b) for uid, b in shadow.roundtrip_volume_sums.items()},
        'inventory_history': shadow.inventory_history,
        'total_realized_pnl': dict(shadow.agent_pnl_total),
        'realized_pnl_by_book': {uid: dict(b) for uid, b in shadow.agent_pnl_by_book.items()},
        'fee_sums': {uid: dict(b) for uid, b in shadow.fee_sums.items()},
        'activity_factors': shadow.activity_factors,
        'pnl_factors': shadow.pnl_factors,
        'kappa_values': shadow.kappa_values,
        'unnormalized_scores': shadow.unnormalized_scores,
        'scores': scores,
        'debeta_scores': getattr(shadow, 'debeta_scores', {}) or {},
        'gentrx_scores': {},
        'book_count': shadow.simulation.book_count,
        'book_ids': list(shadow.simulation.book_ids),
        'simulation_config': {
            'volumeDecimals': shadow.simulation.volumeDecimals,
            'baseDecimals': getattr(shadow.simulation, 'baseDecimals', shadow.simulation.volumeDecimals),
            'quoteDecimals': getattr(shadow.simulation, 'quoteDecimals', shadow.simulation.volumeDecimals),
        },
    }
    state_data = {'accounts': message.get('accounts'), 'books': message.get('books'), 'notices': message.get('notices')}
    result = report_worker(validator_data, state_data)
    if result.get('error'):
        raise RuntimeError(f"report_worker failed: {result['error']}")
    return result['metrics']


def plain(value):
    """JSON-safe copy: dict keys to str, tensors and numpy scalars to floats, NaN/inf to None."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def class_summary(shadow, metrics, uid, simulation):
    """Per asset class: the validator's per-class de-beta detail for this uid (the dashboard's agent_class_gauges)
    plus books traded, books with positive realized P&L and volume from the report's per-book maps."""
    by_book = metrics['realized_pnl_by_book'].get(uid, {})
    volumes = metrics['daily_volumes'].get(uid, {})
    details = getattr(shadow, '_debeta_class_details', None) or {}
    out = {}
    for index, cls in enumerate(simulation.asset_classes() or []):
        books = list(cls.books)
        out[cls.name] = {
            'books': len(books),
            'books_traded': sum(1 for b in books if volumes.get(b, {}).get('total', 0.0) > 0),
            'books_positive_realized_pnl': sum(1 for b in books if by_book.get(b, 0.0) > 0),
            'volume': sum(volumes.get(b, {}).get('total', 0.0) for b in books),
            'debeta': (details.get(index) or {}).get(uid),
        }
    return out


def append_scoreboard(path, step, note, run_name, window, miner):
    """Adds one row to the markdown scoreboard, writing the header first if the file is new."""
    path = Path(path)
    if not path.exists():
        titles = " | ".join(title for _, title, _ in SCOREBOARD_COLUMNS)
        path.write_text(
            "# MyAgent scoreboard\n\n"
            "One row per improvement step, scored by `agents/eval/score_run.py` with the validator's own code.\n"
            "Rank, share and score fields are left out: locally there is only one miner, so they do not compare.\n"
            "Full field dumps are in `agents/eval/scores/<step>/`.\n\n"
            f"| Step | Change | Run | Window | {titles} |\n"
            f"|---|---|---|---|{'---|' * len(SCOREBOARD_COLUMNS)}\n")
    cells = []
    for key, _, fmt in SCOREBOARD_COLUMNS:
        value = miner.get(key)
        cells.append(fmt.format(value) if isinstance(value, (int, float)) else "–")
    with open(path, "a") as fh:
        fh.write(f"| {step} | {note} | {run_name} | {window} | {' | '.join(cells)} |\n")


def main():
    args = parse_args()
    import bittensor as bt
    bt.logging.set_warning()
    from state_recorder import META_FILE, STATES_FILE, read_frames
    from taos.im.validator.scoring_shadow import shadow_score
    from taos.im.validator.trade import update_trade_volumes

    record_dir = Path(args.record_dir).resolve()
    meta = json.loads((record_dir / META_FILE).read_text())
    simulation = load_simulation(meta["simulation_xml"])
    config = build_validator_config()
    interval = int(config.scoring.interval)
    stop_ts = None
    if args.minutes is not None:
        stop_ts = int(simulation.grace_period + args.minutes * 60e9)

    shadow = None
    carried = {}
    board_rows, board_last = {}, None
    timeline = []
    last = None
    frames = 0
    started = time.time()
    for raw in read_frames(record_dir / STATES_FILE):
        message, state = parse_frame(raw)
        if stop_ts is not None and state.timestamp > stop_ts:
            break
        if shadow is None:
            shadow = make_shadow(config, simulation, sorted(int(u) for u in state.accounts))
        shadow.step += 1
        update_trade_volumes(shadow, state)
        frames += 1
        if state.timestamp % interval != 0:
            continue
        result = shadow_score(shadow, state.timestamp, [],
                              trading_ema=carried.get('trading_ema'), trading_ema_n=carried.get('trading_ema_n'),
                              trading_ema_ts=carried.get('trading_ema_ts'),
                              debeta_pool_ema=carried.get('debeta_pool_ema'))
        carried = {k: result[k] for k in ('trading_ema', 'trading_ema_n', 'trading_ema_ts', 'debeta_pool_ema')}
        scores = {uid: s for uid, s in enumerate(result['trading'])}
        shadow.unnormalized_scores = dict(scores)
        metrics = report_metrics(shadow, message, state.timestamp, scores)
        if args.all_uids:
            # one compact row per agent and scoring boundary; the averages in leaderboard.json read these
            for uid in range(len(meta.get('agents') or [])):
                m = metrics['miner_metrics'].get(uid) or {}
                board_rows.setdefault(uid, []).append({'timestamp': state.timestamp,
                                                       **{k: plain(m.get(k)) for k in BOARD_KEYS}})
            board_last = metrics
        miner = metrics['miner_metrics'].get(args.uid)
        if miner is None:
            continue
        last = (state.timestamp, metrics, miner)
        timeline.append({'timestamp': state.timestamp, **plain(miner)})
        minutes = (state.timestamp - simulation.grace_period) / 60e9
        print(f"t={state.timestamp / 1e9:>7.0f}s ({minutes:+6.1f} min after grace) | "
              f"making {miner.get('making_raw')} | skill {miner.get('skill_raw')} | "
              f"scored books {miner.get('num_scored_books')} | pnl {miner.get('pnl'):.2f} | "
              f"{frames} frames, {time.time() - started:.0f}s", flush=True)

    if last is None:
        sys.exit("No scoring boundary with metrics for this uid in the recording (is it long enough?).")
    sim_ts, metrics, miner = last
    window = f"{(sim_ts - simulation.grace_period) / 60e9:.0f} min"
    name = str(args.step) if args.step is not None else record_dir.name
    out = Path(args.out) if args.out else REPO / "agents" / "eval" / "scores" / f"step_{name}"
    out.mkdir(parents=True, exist_ok=True)
    final = {
        'run': record_dir.name,
        'step': args.step,
        'note': args.note,
        'uid': args.uid,
        'agent': (meta.get('agents') or [None])[args.uid] if args.uid < len(meta.get('agents') or []) else None,
        'simulation_timestamp': sim_ts,
        'window_after_grace': window,
        'frames': frames,
        'miner': plain(miner),
        'asset_classes': plain(class_summary(shadow, metrics, args.uid, simulation)),
        'per_book': {
            'volume': plain(metrics['daily_volumes'].get(args.uid, {})),
            'roundtrip_volume': plain(metrics['daily_roundtrip_volumes'].get(args.uid, {})),
            'realized_pnl': plain(metrics['realized_pnl_by_book'].get(args.uid, {})),
            'fees': plain(metrics['fee_sums_by_book'].get(args.uid, {})),
        },
        'kappa_values': plain(shadow.kappa_values.get(args.uid)),
        'debeta_detail': plain((getattr(shadow, 'debeta_detail', {}) or {}).get(args.uid)),
    }
    (out / "final.json").write_text(json.dumps(final, indent=2))
    (out / "timeline.json").write_text(json.dumps(timeline))
    if args.step is not None:
        append_scoreboard(args.scoreboard, args.step, args.note, record_dir.name, window, final['miner'])
    if args.all_uids and board_last is not None:
        board = []
        for uid, name in enumerate(meta.get('agents') or []):
            rows = board_rows.get(uid, [])
            averages = {}
            for key in AVERAGED_KEYS:
                values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
                averages[key] = sum(values) / len(values) if values else None
            board.append({'uid': uid, 'agent': name,
                          'final': plain(board_last['miner_metrics'].get(uid)),
                          'debeta_detail': plain((getattr(shadow, 'debeta_detail', {}) or {}).get(uid)),
                          'classes': plain(class_summary(shadow, board_last, uid, simulation)),
                          'per_book': {
                              'volume': plain(board_last['daily_volumes'].get(uid, {})),
                              'realized_pnl': plain(board_last['realized_pnl_by_book'].get(uid, {})),
                          },
                          'average': averages, 'boundaries': len(rows)})
        (out / "leaderboard.json").write_text(json.dumps({'run': record_dir.name, 'window_after_grace': window,
                                                          'simulation_timestamp': sim_ts, 'agents': board}, indent=2))
        # every agent's compact rows at every scoring boundary, for the dashboard's time charts
        (out / "timelines.json").write_text(json.dumps({'run': record_dir.name, 'grace_period': simulation.grace_period,
                                                        'keys': ['timestamp', *BOARD_KEYS],
                                                        'agents': {str(uid): [[r.get(k) for k in ('timestamp', *BOARD_KEYS)]
                                                                              for r in rows]
                                                                   for uid, rows in board_rows.items()}}))
        print(f"Saved {out / 'leaderboard.json'} and timelines.json ({len(board)} agents)")
    print(f"\nSaved {out / 'final.json'} and {out / 'timeline.json'}")
    for key, title, fmt in SCOREBOARD_COLUMNS:
        value = final['miner'].get(key)
        print(f"  {title:<16} {fmt.format(value) if isinstance(value, (int, float)) else '–'}")


if __name__ == "__main__":
    main()
