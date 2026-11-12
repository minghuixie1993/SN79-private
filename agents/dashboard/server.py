# SPDX-License-Identifier: MIT
"""
A local web dashboard for agents/network runs, laid out like the mainnet agents dashboard (taos.simulate.trading):
a runs list, a leaderboard per run and a page per agent with its score history, asset-class breakdown and books.

It reads what run_network.py and score_network.py write under agents/data/network/<run>/: network.json, each
validator's taosim.log (live progress) and score/ (leaderboard.json, timelines.json from score_run.py --all-uids).
A run that is still going, or was scored before timelines existed, can be scored from the page: "Score now" replays
the recording so far into <validator>/score_live/ (the final score/ is left to score_network.py), "Rescore" replays
the whole recording into score/ again. Both run at the lowest CPU priority.

Usage (from the repository root):
    python agents/dashboard/server.py                 # http://127.0.0.1:8050
    python agents/dashboard/server.py --port 8060 --host 0.0.0.0
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO / "agents" / "data" / "network"
STATIC = Path(__file__).resolve().parent / "static"
sys.path.insert(0, str(REPO / "agents" / "network"))
from run_network import sim_seconds  # noqa: E402

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, PlainTextResponse  # noqa: E402

GRACE_SECONDS = 600
LIVE_SECONDS = 90           # a simulator log written to this recently means the run is still going
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_jobs = {}                  # run name -> [(validator, Popen, kind, log path)]
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def run_dir(name):
    path = (RUNS_DIR / name).resolve()
    if path.parent != RUNS_DIR.resolve() or not (path / "network.json").exists():
        raise HTTPException(404, f"no run {name}")
    return path


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def scoring_jobs(name):
    """This server's scoring processes for a run that are still going."""
    return [(v, p, kind) for v, p, kind, _ in _jobs.get(name, []) if p.poll() is None]


def scoring_failures(name):
    """This server's last replays of a run that exited with an error, with the end of their log."""
    out = []
    for v, p, kind, log in _jobs.get(name, []):
        if p.poll() in (None, 0):
            continue
        try:
            tail = ANSI.sub("", "".join(deque(open(log, errors="replace"), maxlen=6))).strip()
        except FileNotFoundError:
            tail = ""
        out.append({"validator": v, "kind": kind, "code": p.returncode, "log": str(log.relative_to(REPO)), "tail": tail})
    return out


def progress(path, network):
    """Per validator: simulation seconds reached, the target and whether its simulator is still writing."""
    target = GRACE_SECONDS + int(float(network.get("minutes") or 0) * 60)
    out = []
    for v in network["validators"]:
        log = path / v["name"] / "taosim.log"
        try:
            age = time.time() - log.stat().st_mtime
        except FileNotFoundError:
            age = None
        out.append({"name": v["name"], "seed": v.get("seed"), "sim_s": sim_seconds(log), "target_s": target,
                    "grace_s": GRACE_SECONDS, "live": age is not None and age < LIVE_SECONDS})
    return out


def score_source(path, network):
    """Which scores to show: the final score/ when every validator has one, else score_live/ when any has."""
    for folder, kind in (("score", "final"), ("score_live", "live")):
        boards = {v["name"]: read_json(path / v["name"] / folder / "leaderboard.json") for v in network["validators"]}
        boards = {k: b for k, b in boards.items() if b}
        if boards and (kind == "live" or len(boards) == len(network["validators"])):
            return kind, folder, boards
    return None, None, {}


def agent_row(entry):
    final, avg = entry.get("final") or {}, entry.get("average") or {}
    classes = {}
    for cname, c in (entry.get("classes") or {}).items():
        d = c.get("debeta") or {}
        classes[cname] = {"making": d.get("making_raw"), "skill": d.get("skill_raw"), "skill_books": d.get("skill_books"),
                          "coverage_books": d.get("coverage_books"), "s3": d.get("making_s3_factor"),
                          "books": c.get("books"), "books_traded": c.get("books_traded"),
                          "books_positive": c.get("books_positive_realized_pnl"), "volume": c.get("volume"),
                          "present": d.get("present")}
    return {"uid": entry["uid"], "agent": entry["agent"], "trading": avg.get("trading_score"),
            "score": final.get("score"), "score_avg": avg.get("score"), "making": final.get("making_raw"),
            "captured": final.get("making_captured"), "realized": final.get("making_realized"),
            "making_share": final.get("making_share"), "skill": final.get("skill_raw"),
            "net_alpha": final.get("skill_net_alpha"), "skill_books": final.get("skill_books_kept"),
            "coverage_books": final.get("coverage_books"), "p11": final.get("p11_factor"), "s3": final.get("s3_factor"),
            "notional": final.get("notional"), "pnl": final.get("pnl"), "realized_pnl": final.get("total_realized_pnl"),
            "classes": classes}


def combine(network, boards):
    """One row per agent: its per-validator rows, and the mean and median of their trading scores (the median
    stands in for the network's stake-weighted consensus, as in score_network.py)."""
    files = {a["uid"]: a["file"] for a in network["agents"]}
    per_validator = {name: {e["uid"]: agent_row(e) for e in b.get("agents", [])} for name, b in boards.items()}
    for rows in per_validator.values():
        for rank, row in enumerate(sorted(rows.values(), key=lambda r: -(r["trading"] or 0.0)), 1):
            row["rank"] = rank
    combined = []
    for uid, file in files.items():
        rows = {name: rows[uid] for name, rows in per_validator.items() if uid in rows}
        trading = [r["trading"] for r in rows.values() if isinstance(r["trading"], (int, float))]
        score = [r["score"] for r in rows.values() if isinstance(r["score"], (int, float))]

        def median_of(key):
            values = [r[key] for r in rows.values() if isinstance(r.get(key), (int, float))]
            return statistics.median(values) if values else None
        combined.append({"uid": uid, "agent": file, "mine": Path(file).name.startswith("MyAgent"),
                         "median": statistics.median(trading) if trading else None,
                         "mean": statistics.mean(trading) if trading else None,
                         "score": statistics.mean(score) if score else None,
                         **{k: median_of(k) for k in ("making", "skill", "skill_books", "notional", "pnl",
                                                      "making_share", "s3", "p11", "net_alpha")},
                         "validators": rows})
    combined.sort(key=lambda r: -(r["median"] or 0.0))
    for rank, row in enumerate(combined, 1):
        row["rank"] = rank
    return combined


@app.get("/api/runs")
def runs():
    out = []
    for path in sorted(RUNS_DIR.glob("*/network.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        network = read_json(path)
        if not network:
            continue
        prog = progress(path.parent, network)
        kind, _, boards = score_source(path.parent, network)
        leader = combine(network, boards)[0] if boards else None
        out.append({"name": path.parent.name, "created": path.stat().st_mtime, "minutes": network.get("minutes"),
                    "layout": Path(network.get("simulation_xml", "")).name, "agents": len(network["agents"]),
                    "validators": prog, "running": any(v["live"] for v in prog), "scores": kind,
                    "scoring": bool(scoring_jobs(path.parent.name)),
                    "scoring_failed": bool(scoring_failures(path.parent.name)),
                    "leader": leader and {"agent": leader["agent"], "median": leader["median"]}})
    return out


@app.get("/api/runs/{name}")
def run(name: str):
    path = run_dir(name)
    network = read_json(path / "network.json")
    kind, folder, boards = score_source(path, network)
    any_board = next(iter(boards.values()), None) or {}
    classes = []
    for entry in any_board.get("agents", [])[:1]:
        classes = [{"name": c, "books": v.get("books")} for c, v in (entry.get("classes") or {}).items()]
    return {"name": name, "minutes": network.get("minutes"), "layout": Path(network.get("simulation_xml", "")).name,
            "agents": network["agents"], "validators": progress(path, network),
            "scores": {"source": kind, "folder": folder,
                       "window": any_board.get("window_after_grace"), "simulation_timestamp": any_board.get("simulation_timestamp"),
                       "has_timelines": any((path / v / folder / "timelines.json").exists() for v in boards) if folder else False,
                       "classes": classes, "combined": combine(network, boards) if boards else []},
            "scoring": [{"validator": v, "kind": k} for v, _, k in scoring_jobs(name)],
            "scoring_failed": scoring_failures(name)}


@app.get("/api/runs/{name}/timelines")
def timelines(name: str):
    path = run_dir(name)
    network = read_json(path / "network.json")
    _, folder, _ = score_source(path, network)
    if not folder:
        return {}
    return {v["name"]: read_json(path / v["name"] / folder / "timelines.json") for v in network["validators"]}


@app.get("/api/runs/{name}/agents/{uid}")
def agent(name: str, uid: int):
    path = run_dir(name)
    network = read_json(path / "network.json")
    _, folder, boards = score_source(path, network)
    entries = {v: next((e for e in b.get("agents", []) if e["uid"] == uid), None) for v, b in boards.items()}
    info = next((a for a in network["agents"] if a["uid"] == uid), None)
    if info is None:
        raise HTTPException(404, f"no uid {uid}")
    return {"agent": info, "validators": {v: e and {"row": agent_row(e), "per_book": e.get("per_book"),
                                                    "debeta_detail": e.get("debeta_detail")}
                                          for v, e in entries.items()}}


@app.get("/api/runs/{name}/agents/{uid}/log", response_class=PlainTextResponse)
def agent_log(name: str, uid: int, lines: int = 120):
    path = run_dir(name)
    logs = sorted((path / "agents").glob(f"{uid}_*.log"))
    if not logs:
        return ""
    with open(logs[0], errors="replace") as fh:
        return ANSI.sub("", "".join(deque(fh, maxlen=max(1, min(lines, 2000)))))


@app.post("/api/runs/{name}/score")
def score(name: str, mode: str = "live"):
    """Replays each validator's recording: mode "live" (so far, into score_live/) or "final" (into score/)."""
    path = run_dir(name)
    if scoring_jobs(name):
        raise HTTPException(409, "already scoring this run")
    network = read_json(path / "network.json")
    folder = "score_live" if mode == "live" else "score"
    jobs = []
    for v in network["validators"]:
        record = path / v["name"] / "record"
        if not (record / "states.msgpack.zst").exists():
            continue
        argv = ["nice", "-n", "19", sys.executable, str(REPO / "agents" / "eval" / "score_run.py"), str(record),
                "--all-uids", "--out", str(path / v["name"] / folder)]
        if mode != "live" and network.get("minutes"):
            argv += ["--minutes", str(network["minutes"])]
        log = path / v["name"] / f"{folder}.log"
        with open(log, "w") as fh:
            proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, cwd=REPO)
        jobs.append((v["name"], proc, mode, log))
    _jobs[name] = jobs
    return {"started": [v for v, _, _, _ in jobs]}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8050)
    args = parser.parse_args()
    import uvicorn
    print(f"Dashboard on http://{args.host}:{args.port}  (runs from {RUNS_DIR})", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
