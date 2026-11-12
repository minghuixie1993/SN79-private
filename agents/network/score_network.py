# SPDX-License-Identifier: MIT
"""
Scores a run_network.py run: every validator's recording is replayed through the validator's own scoring
(agents/eval/score_run.py --all-uids, the validators in parallel), then one leaderboard per validator and a
combined one are written to <run dir>/leaderboard.md and printed.

Per validator an agent is ranked by its de-beta trading score averaged over the run's scoring boundaries (every
5 s of simulation), which is steadier than the last boundary alone; the final `score` (the validator's smoothed
weight input) is shown beside it. Across validators the combined board takes the mean and the median of those
averages, the median standing in for consensus: on a network the weights a miner receives are a stake-weighted
consensus of every validator's, so one validator's outlier score does not carry it.

Usage:
    python agents/network/score_network.py agents/data/network/<run> [--minutes 60] [--rescore]
"""
import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def fmt(value, spec="{:.3f}"):
    return spec.format(value) if isinstance(value, (int, float)) else "–"


def validator_board(board):
    """Rows for one validator, best first."""
    rows = []
    for entry in board["agents"]:
        final, avg = entry.get("final") or {}, entry.get("average") or {}
        rows.append({
            "uid": entry["uid"], "agent": entry["agent"],
            "trading": avg.get("trading_score"), "score": final.get("score"),
            "making": final.get("making_raw"), "captured": final.get("making_captured"),
            "realized": final.get("making_realized"), "skill": final.get("skill_raw"),
            "skill_books": final.get("skill_books_kept"), "notional": final.get("notional"), "pnl": final.get("pnl"),
        })
    rows.sort(key=lambda r: -(r["trading"] or 0.0))
    for rank, r in enumerate(rows, 1):
        r["rank"] = rank
    return rows


def table(rows, columns):
    head = "| " + " | ".join(title for title, _, _ in columns) + " |"
    sep = "|" + "---|" * len(columns)
    body = ["| " + " | ".join(fmt(r.get(key), spec) if spec else str(r.get(key, "–")) for _, key, spec in columns) + " |"
            for r in rows]
    return "\n".join([head, sep, *body])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir")
    parser.add_argument("--minutes", type=float, default=None)
    parser.add_argument("--rescore", action="store_true", help="Replay again even where leaderboard.json exists.")
    args = parser.parse_args()
    run_dir = Path(args.run_dir).resolve()
    network = json.loads((run_dir / "network.json").read_text())
    minutes = args.minutes if args.minutes is not None else network.get("minutes")
    names = {a["uid"]: f"{a['file']}" for a in network["agents"]}

    jobs = []
    for v in network["validators"]:
        out = run_dir / v["name"] / "score"
        if (out / "leaderboard.json").exists() and not args.rescore:
            continue
        argv = [sys.executable, str(REPO / "agents" / "eval" / "score_run.py"), str(run_dir / v["name"] / "record"),
                "--all-uids", "--out", str(out)]
        if minutes:
            argv += ["--minutes", str(minutes)]
        log = open(run_dir / v["name"] / "score.log", "w")
        print(f"Scoring {v['name']}...", flush=True)
        jobs.append((v["name"], subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)))
    for name, proc in jobs:
        if proc.wait() != 0:
            print(f"Scoring {name} failed; see {run_dir / name / 'score.log'}", flush=True)

    columns = [("Rank", "rank", None), ("uid", "uid", None), ("Agent", "agent", None),
               ("Avg trading score", "trading", "{:.3f}"), ("Final score", "score", "{:.3f}"),
               ("Making", "making", "{:.4g}"), ("Captured", "captured", "{:.4g}"), ("Realized", "realized", "{:.4g}"),
               ("Skill", "skill", "{:.3f}"), ("Skill books", "skill_books", "{:.0f}"),
               ("Notional", "notional", "{:.3g}"), ("PnL", "pnl", "{:.1f}")]
    parts = [f"# Network leaderboard: {run_dir.name}\n",
             f"{len(network['agents'])} agents, {len(network['validators'])} validators, "
             f"{minutes} simulation minutes after grace. Scored with the validator's own code (score_run.py).\n"]
    per_validator = {}
    for v in network["validators"]:
        path = run_dir / v["name"] / "score" / "leaderboard.json"
        if not path.exists():
            parts.append(f"## {v['name']} (seed {v.get('seed')})\n\nNot scored.\n")
            continue
        rows = validator_board(json.loads(path.read_text()))
        for r in rows:
            r["agent"] = names.get(r["uid"], r["agent"])
        per_validator[v["name"]] = {r["uid"]: r for r in rows}
        parts.append(f"## {v['name']} (seed {v.get('seed')})\n\n{table(rows, columns)}\n")

    if per_validator:
        combined = []
        for uid, name in names.items():
            scores = [b[uid]["trading"] for b in per_validator.values() if uid in b and b[uid]["trading"] is not None]
            ranks = [b[uid]["rank"] for b in per_validator.values() if uid in b]
            row = {"uid": uid, "agent": name,
                   "mean": statistics.mean(scores) if scores else None,
                   "median": statistics.median(scores) if scores else None,
                   "mean_rank": statistics.mean(ranks) if ranks else None}
            for vname, b in per_validator.items():
                row[vname] = b.get(uid, {}).get("trading")
            combined.append(row)
        combined.sort(key=lambda r: (-(r["median"] or 0.0), r["mean_rank"] or 1e9))
        for rank, r in enumerate(combined, 1):
            r["rank"] = rank
        ccols = [("Rank", "rank", None), ("uid", "uid", None), ("Agent", "agent", None),
                 ("Median trading", "median", "{:.3f}"), ("Mean trading", "mean", "{:.3f}"),
                 ("Mean rank", "mean_rank", "{:.1f}")] + [(n, n, "{:.3f}") for n in per_validator]
        parts.insert(2, "## Combined (all validators)\n\nRanked by the median over validators of each agent's average "
                        f"de-beta trading score.\n\n{table(combined, ccols)}\n")
        (run_dir / "leaderboard.json").write_text(json.dumps({"combined": combined,
                                                              "validators": {k: list(v.values()) for k, v in per_validator.items()}},
                                                             indent=2))
    text = "\n".join(parts)
    (run_dir / "leaderboard.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
