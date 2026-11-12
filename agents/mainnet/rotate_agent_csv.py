# SPDX-License-Identifier: MIT
"""
Moves a miner agent's order / cancellation / trade CSV histories aside (gzipped into an archive folder), so the next
miner start does not parse them.

Why: on its first state from each validator, FinanceAgent.load_event_history (taos/im/agents/__init__.py) reads the
WHOLE orders.csv and cancellations.csv of the running simulation into event objects before trimming them to
event_lookback_minutes. They grow every round (23 MB after ~8 h on 23 October), so the first reply after a restart grew
from ~5 s to 26.6 s, past the validator's 3 s timeout, and requests queued meanwhile were refused ("Nonce is too old").
The agent appends with open(path, "a") on every write, so moving the files while the miner runs is safe: the next
write starts a fresh file. The MyAgent10_23_* agents never read event_history.

Usage (from the repository root):
    python agents/mainnet/rotate_agent_csv.py                 # every uid folder, files over 1 MB
    python agents/mainnet/rotate_agent_csv.py --uid 105 --min-mb 0 --dry-run
"""
import argparse
import gzip
import shutil
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NAMES = ("orders.csv", "cancellations.csv", "trades.csv")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # The default was 105, the uid before the 24 October re-registration as uid 8: every restart since then rotated
    # nothing and uid 8's orders.csv reached 26 MB. "all" covers whatever uid the miner runs under now.
    parser.add_argument("--uid", default="all",
                        help="The agent's data folder under agents/data/, or 'all' (every numeric folder).")
    parser.add_argument("--min-mb", type=float, default=1.0, help="Only move files at least this large.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    data = REPO / "agents" / "data"
    roots = (sorted(p for p in data.iterdir() if p.is_dir() and p.name.isdigit()) if args.uid == "all"
             else [data / str(args.uid)])
    stamp = time.strftime("%Y%m%d_%H%M%S")
    moved = 0
    # only the miner's own <hotkey>/<simulation>/ files, not local replay output kept beside them
    paths = [(root, p) for root in roots for name in NAMES for p in root.glob(f"*/*/{name}")
             if len(p.parent.parent.name) >= 40]
    for root, path in sorted(paths):
        archive = root / "csv_archive"
        size = path.stat().st_size
        if size < args.min_mb * 1e6:
            continue
        target = archive / path.parent.parent.name[:8] / path.parent.name / f"{path.stem}_{stamp}.csv.gz"
        print(f"{path.relative_to(REPO)} ({size / 1e6:.1f} MB) -> {target.relative_to(REPO)}")
        if args.dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f"{path.stem}.rotating_{stamp}.csv")
        path.rename(staged)                     # the miner's next append creates a fresh file
        with open(staged, "rb") as src, gzip.open(target, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        staged.unlink()
        moved += 1
    print(f"{'would move' if args.dry_run else 'moved'} {moved} file(s)")


if __name__ == "__main__":
    main()
