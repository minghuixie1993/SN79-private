# SPDX-License-Identifier: MIT
"""
A local network: several validators, each running its own simulation, and one set of agents serving all of them,
the way a miner serves every validator that queries it.

Each validator gets its own copy of the simulation layout (128 books by default, the run_validator.sh layout) with
its own `seed`, its own proxy (agents/proxy/proxy.py, with its own hotkey, port and state recording) and its own
simulator; they talk over IPC names suffixed per validator (TAOSIM_IPC_SUFFIX). The agents are started once, by
agent_server.py, which hands them one state at a time as the miner does.

Usage (from the repository root):
    python agents/network/run_network.py agents/network/network.example.json             # run, then stop
    python agents/network/run_network.py agents/network/network.example.json --score     # and score every validator
    python agents/network/run_network.py <config> --minutes 5                            # a short check

Output: agents/data/network/<name>_<timestamp>/
    network.json                the agents (uid, file, params, port) and validators of the run
    agents/<uid>_<file>.log     each agent's log
    <validator>/config/         that validator's simulation XMLs (seed and port set)
    <validator>/launcher_config.json, proxy.log, taosim.log, record/ (states for score_run.py), sim/ (simulator logs)
Score it later with agents/network/score_network.py <run dir>.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO / "agents"
PROXY_DIR = AGENTS_DIR / "proxy"
TIME_MARK = b"TIME : "


def discover_agents():
    """Every agent file in agents/ that runs as a strategy process (has a top-level launch(...) call)."""
    found = []
    for path in sorted(AGENTS_DIR.glob("*.py")):
        if any(line.startswith("    launch(") or line.startswith("launch(") for line in path.read_text().splitlines()):
            found.append(path.stem)
    return found


def default_params(file):
    """The agent's launch parameters from bin/agent_params.sh, the map run_miner.sh uses ({} when it has none)."""
    out = subprocess.run(["bash", "-c", f'source "{REPO}/bin/agent_params.sh"; agent_params "{file}"'],
                         capture_output=True, text=True).stdout.split()
    return dict(item.split("=", 1) for item in out if "=" in item)


def agent_specs(cfg):
    """[(file, params)] in the order the proxies will number them."""
    common = cfg.get("common_params", {})
    overrides = cfg.get("agent_params", {})
    if cfg.get("agents", "all") == "all":
        entries = [{"file": f} for f in discover_agents() if f not in set(cfg.get("exclude", []))]
    else:
        entries = cfg["agents"]
    specs = []
    for entry in entries:
        params = {**default_params(entry["file"]), **common, **overrides.get(entry["file"], {}), **entry.get("params", {})}
        specs.append((entry["file"], params))
    # The proxy numbers agents file by file in first-appearance order (its config groups them by file), so the
    # agent ids follow that order too.
    order = list(dict.fromkeys(f for f, _ in specs))
    return [s for f in order for s in specs if s[0] == f]


def write_validator_xml(source_xml, out_dir, seed, port):
    """Copies the layout (root and the background files it names) into out_dir with the run's seed and port."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tree = ET.parse(source_xml)
    root = tree.getroot()
    root.set("port", str(port))
    if root.tag == "MultiAssetSimulation":
        root.set("seed", str(seed))
        for bg in root.iter("Background"):
            src = (Path(source_xml).parent / bg.get("path")).resolve()
            (out_dir / src.name).write_text(src.read_text())
            bg.set("path", src.name)
    else:
        root.set("rngSeed", str(seed))
    target = out_dir / Path(source_xml).name
    tree.write(target)
    return target


def available_mb():
    """Memory the kernel could hand out now without swapping (MemAvailable), in MB."""
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 1 << 30


def sim_seconds(log_path):
    """The simulation time the simulator last printed to its log, in seconds (0 before the first)."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            tail = fh.read()
    except FileNotFoundError:
        return 0
    at = tail.rfind(TIME_MARK)
    if at < 0:
        return 0
    h, m, s = tail[at + len(TIME_MARK):at + len(TIME_MARK) + 8].decode(errors="replace").split(":")[:3]
    return int(h) * 3600 + int(m) * 60 + int(float(s))


class Network:
    def __init__(self, cfg, run_dir):
        self.cfg = cfg
        self.run_dir = run_dir
        self.procs = []   # (name, Popen)

    def spawn(self, name, argv, log, env=None, cwd=None):
        fh = open(log, "ab")
        proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, env=env, cwd=cwd, start_new_session=True)
        self.procs.append((name, proc))
        return proc

    def stop(self):
        for name, proc in reversed(self.procs):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.time() + 30
        for name, proc in self.procs:
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        self.procs = []

    def dead(self):
        return [name for name, proc in self.procs if proc.poll() is not None]


def wait_ready(ports, procs, timeout):
    """Ports whose agent answers within `timeout` seconds; an agent whose process exits first is not ready."""
    pending, ready = dict(zip(ports, procs)), []
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        for port, proc in list(pending.items()):
            if proc.poll() is not None:
                del pending[port]
                continue
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=2)
                ready.append(port)
                del pending[port]
            except Exception:
                pass
        time.sleep(2)
    return ready


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config")
    parser.add_argument("--minutes", type=float, default=None, help="Simulation minutes after the grace period.")
    parser.add_argument("--score", action="store_true", help="Score every validator when the run ends.")
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    minutes = args.minutes if args.minutes is not None else float(cfg.get("minutes", 60))
    py = sys.executable
    source_xml = (REPO / cfg.get("simulation_xml", "simulate/trading/run/config/multiasset_simulation_0.xml")).resolve()
    taosim = str((REPO / cfg.get("taosim_bin", "simulate/trading/build/src/cpp/taosim")).resolve())
    grace = int(ET.parse(source_xml).getroot().get("gracePeriod")) // 10**9
    target = grace + int(minutes * 60) + 30

    run_dir = AGENTS_DIR / "data" / "network" / f"{cfg.get('name', 'network')}_{datetime.now():%Y%m%d_%H%M%S}"
    (run_dir / "agents").mkdir(parents=True)
    net = Network(cfg, run_dir)
    signal.signal(signal.SIGTERM, lambda *_: (net.stop(), sys.exit(143)))

    # Agents: started once; any that does not come up is dropped and the rest restarted, so uids stay contiguous.
    specs = agent_specs(cfg)
    agent_port = int(cfg.get("agent_port", 9100))
    for attempt in range(2):
        procs, ports = [], []
        for uid, (file, params) in enumerate(specs):
            argv = [py, str(AGENTS_DIR / "network" / "agent_server.py"), str(AGENTS_DIR / f"{file}.py"),
                    "--port", str(agent_port + uid), "--agent_id", str(uid),
                    "--params", *[f"{k}={v}" for k, v in params.items()], f"data_dir={run_dir / 'agents' / 'data'}"]
            # `file` may be a path under agents/ (e.g. "Agent_10_23/MyAgent10_23_Comment"); its log is named after the file.
            procs.append(net.spawn(f"agent {uid} {file}", argv, run_dir / "agents" / f"{uid}_{Path(file).name}.log",
                                   cwd=run_dir / "agents"))
            ports.append(agent_port + uid)
        print(f"Starting {len(specs)} agents...", flush=True)
        ready = set(wait_ready(ports, procs, float(cfg.get("agent_start_timeout", 240))))
        failed = [specs[p - agent_port][0] for p in ports if p not in ready]
        if not failed:
            break
        print(f"Agents that did not start: {failed} (logs in {run_dir / 'agents' / 'first_start'}); "
              "continuing without them.", flush=True)
        net.stop()
        # The restart renumbers the agents; keep the first attempt's logs apart so names stay unambiguous.
        (run_dir / "agents" / "first_start").mkdir(exist_ok=True)
        for log in (run_dir / "agents").glob("*.log"):
            log.rename(run_dir / "agents" / "first_start" / log.name)
        specs = [s for p, s in zip(ports, specs) if p in ready]
        if not specs:
            sys.exit("No agent started.")
    else:
        net.stop()
        sys.exit("Agents failed to start twice.")

    agents_cfg = {"path": str(AGENTS_DIR), "start_port": agent_port}
    for file, params in specs:
        agents_cfg.setdefault(file, []).append({"params": params, "count": 1})
    validators = cfg["validators"]
    proxy_port = int(cfg.get("proxy_port", 8100))
    (run_dir / "network.json").write_text(json.dumps({
        "config": cfg, "minutes": minutes, "simulation_xml": str(source_xml),
        "agents": [{"uid": uid, "file": f, "params": p, "port": agent_port + uid} for uid, (f, p) in enumerate(specs)],
        "validators": [{**v, "port": proxy_port + i} for i, v in enumerate(validators)],
    }, indent=2))

    for i, v in enumerate(validators):
        vdir = run_dir / v["name"]
        xml = write_validator_xml(source_xml, vdir / "config", v.get("seed", 1000 + i), proxy_port + i)
        launcher = {"proxy": {"port": proxy_port + i, "simulation_xml": str(xml), "timeout": cfg.get("timeout", 5),
                              "record_states": str(vdir / "record"), "hotkey": v["name"],
                              "latency_s": float(cfg.get("latency_s", 0.0))},
                    "agents": agents_cfg}
        (vdir / "launcher_config.json").write_text(json.dumps(launcher, indent=2))
        env = {**os.environ, "TAOSIM_IPC_SUFFIX": f"_{v['name']}"}
        net.spawn(f"proxy {v['name']}", [py, "proxy.py", "--config", str(vdir / "launcher_config.json")],
                  vdir / "proxy.log", env=env, cwd=PROXY_DIR)
    time.sleep(float(cfg.get("simulator_delay", 8)))
    for i, v in enumerate(validators):
        vdir = run_dir / v["name"]
        env = {**os.environ, "TAOSIM_IPC_SUFFIX": f"_{v['name']}"}
        net.spawn(f"taosim {v['name']}", [taosim, "-f", str(vdir / "config" / source_xml.name), "-d", str(vdir / "sim")],
                  vdir / "taosim.log", env=env, cwd=vdir)

    print(f"Run {run_dir}: {len(specs)} agents, {len(validators)} validators, until simulation time {target}s", flush=True)
    # The host has no swap: once memory runs out the kernel kills processes, desktop sessions included, and
    # everything else crawls. The run stops itself well before that, and when the simulations stop advancing.
    min_available = int(cfg.get("min_available_mb", 3000))
    stall_limit = float(cfg.get("stall_minutes", 10)) * 60
    started = last_progress = time.time()
    last_times = None
    try:
        while True:
            time.sleep(5)
            dead = net.dead()
            if dead:
                print(f"\nStopped early: {dead} exited (see the logs in {run_dir}).", flush=True)
                net.stop()
                sys.exit(1)
            available = available_mb()
            if available < min_available:
                print(f"\nStopped early: {available} MB of memory left (limit {min_available} MB).", flush=True)
                net.stop()
                sys.exit(1)
            times = {v["name"]: sim_seconds(run_dir / v["name"] / "taosim.log") for v in validators}
            if times != last_times:
                last_times, last_progress = times, time.time()
            elif time.time() - last_progress > stall_limit:
                print(f"\nStopped early: no simulation advanced for {stall_limit / 60:.0f} min.", flush=True)
                net.stop()
                sys.exit(1)
            elapsed = (time.time() - started) / 60
            print(f"\r  {elapsed:6.1f} min | " + " | ".join(f"{n} {t}s" for n, t in times.items())
                  + f" / {target}s | {available} MB free", end="", flush=True)
            if min(times.values()) >= target:
                break
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
        net.stop()
        sys.exit(130)
    print("\nAll validators reached the target; stopping.", flush=True)
    net.stop()
    if args.score:
        subprocess.run([py, str(AGENTS_DIR / "network" / "score_network.py"), str(run_dir), "--minutes", str(minutes)])


if __name__ == "__main__":
    main()
