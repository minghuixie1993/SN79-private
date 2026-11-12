# SPDX-License-Identifier: MIT
"""
Is the live miner healthy enough for a local validation to run beside it? One line of JSON, and the exit status:
0 healthy, 1 not (a validation should wait, or scale down / stop), 2 no live miner found.

Checks:
  - the miner process (taos/im/neurons/miner.py or agents/mainnet/recorded_miner.py) is running; its RSS and CPU,
  - the host's available memory against --min-free-mb (no swap here: running out lets the kernel kill the miner),
  - the miner's reply times over its last rounds, per validator, from the recorder's tap_stats.json (written every
    30-60 s by agents/mainnet/state_tap.py): p90 against --max-p90. The validator turns reply time into order delay
    (10 ms up to 1 s) and drops replies past its timeout, so slow replies cost score directly. Without the recorder
    there are no reply times; then only the process and memory are checked.

Usage (from the repository root):
    python agents/mainnet/miner_health.py                         # one check
    python agents/mainnet/miner_health.py --min-free-mb 6000 --max-p90 0.5
    until python agents/mainnet/miner_health.py >/dev/null; do sleep 60; done && <start the validation>
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from state_tap import DEFAULT_DIR, STATS_FILE  # noqa: E402

MINER_SCRIPTS = ("taos/im/neurons/miner.py", "neurons/miner.py", "recorded_miner.py", "miner.py --netuid")


def miner_processes():
    """(pid, rss MB, cpu %, command) of the processes running the miner."""
    out = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        if not cmd or "miner_health" in cmd or not any(s in cmd for s in MINER_SCRIPTS) or "--netuid" not in cmd:
            continue
        try:
            rss = int(next(l.split()[1] for l in (proc / "status").read_text().splitlines() if l.startswith("VmRSS"))) / 1024
        except (OSError, StopIteration):
            rss = None
        out.append({"pid": int(proc.name), "rss_mb": round(rss) if rss else None, "cmd": cmd[:160]})
    return out


def available_mb():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable"):
            return int(line.split()[1]) // 1024
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-free-mb", type=int, default=6000, help="Least available memory to call it healthy.")
    parser.add_argument("--max-p90", type=float, default=1.0, help="Highest p90 reply time (s) over recent rounds.")
    parser.add_argument("--stale-s", type=float, default=600, help="Ignore reply stats older than this.")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="The recorder's directory.")
    parser.add_argument("--record-cap-gb", type=float, default=30.0,
                        help="The recorder's --max-gb (start_new_uid.sh runs it with 30): warn at 90%% of it.")
    args = parser.parse_args()

    report = {"at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "problems": []}
    procs = miner_processes()
    report["miner"] = procs
    report["available_mb"] = free = available_mb()
    if free is not None and free < args.min_free_mb:
        report["problems"].append(f"available memory {free} MB < {args.min_free_mb} MB")
    stats_file = Path(args.dir) / STATS_FILE
    replies = {}
    try:
        stats = json.loads(stats_file.read_text())
        if time.time() - stats.get("at", 0) <= args.stale_s:
            replies = stats.get("reply_seconds") or {}
            report["recorder"] = {k: stats.get(k) for k in ("written", "dropped_queue", "dropped_disk", "errors")}
    except (OSError, ValueError):
        pass
    report["reply_seconds"] = {v[:8]: {k: round(x, 3) if isinstance(x, float) and k != "last_round" else x
                                       for k, x in r.items() if k != "last_round"} for v, r in replies.items()}
    slow = {v[:8]: r["p90"] for v, r in replies.items() if r.get("p90", 0) > args.max_p90}
    if slow:
        report["problems"].append(f"reply p90 over {args.max_p90} s: {slow}")
    if not replies:
        report["note"] = "no recent reply times (recorder off, or no rounds yet); only process and memory checked"
    # The recorder stops writing at its --max-gb folder cap without touching the miner (28 Oct: dropped_disk 166 before
    # anyone noticed). Warn, not fail: the miner itself is fine, but replays and the switchback read need the frames.
    warnings = []
    if (report.get("recorder") or {}).get("dropped_disk"):
        warnings.append(f"recorder dropped frames at its folder cap (dropped_disk {report['recorder']['dropped_disk']}, cumulative since the miner start; check that written grows)")
    try:
        size_gb = sum(p.stat().st_size for p in Path(args.dir).rglob("*.frames.zst")) / 1e9
        report["record_gb"] = round(size_gb, 1)
        if size_gb >= 0.9 * args.record_cap_gb:
            warnings.append(f"recordings {size_gb:.1f} GB >= 90% of the {args.record_cap_gb:g} GB recorder cap")
    except OSError:
        pass
    if warnings:
        report["warnings"] = warnings
    report["healthy"] = bool(procs) and not report["problems"]
    print(json.dumps(report))
    sys.exit(2 if not procs else (0 if report["healthy"] else 1))


if __name__ == "__main__":
    main()
