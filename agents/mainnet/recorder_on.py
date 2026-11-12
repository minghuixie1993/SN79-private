# SPDX-License-Identifier: MIT
"""
Switches the pm2 `miner` process that run_miner.sh started to agents/mainnet/recorded_miner.py, with the same
arguments, working directory and interpreter, so every validator round is recorded (see state_tap.py); --off
switches it back to the stock miner.py.

run_miner.sh stays the way to set the miner up (wallet, agent, params, git pull). Run this right after it; run it
again after any later run_miner.sh, which starts the stock miner.py again.

Usage (from the repository root):
    python agents/mainnet/recorder_on.py                                 # record every validator, all rounds
    python agents/mainnet/recorder_on.py --validators 5EWwdZB7,5F4tQyWr --every 2 --max-gb 30
    python agents/mainnet/recorder_on.py --off
    python agents/mainnet/recorder_on.py --dry-run                       # print the pm2 commands only
    python agents/mainnet/recorder_on.py --agent MyAgent10_23_5Gpt --agent-params "event_lookback_minutes=1 shield_post_s=2"
                                                                         # switch the agent (and its params) too
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = Path(__file__).resolve().parent / "recorded_miner.py"
STOCK = REPO / "taos" / "im" / "neurons" / "miner.py"


def pm2_home():
    """Makes sure pm2 runs from a home this account owns: ~/.pm2 here belongs to root (pm2 was once run with sudo),
    so without PM2_HOME every pm2 call fails. ~/.bashrc exports PM2_HOME=~/.pm2-trustvs; this repeats it for shells
    that did not read it."""
    if not os.environ.get("PM2_HOME"):
        default = Path.home() / ".pm2"
        sock = default / "rpc.sock"
        if sock.exists() and sock.stat().st_uid != os.getuid():
            os.environ["PM2_HOME"] = str(Path.home() / ".pm2-trustvs")
    return os.environ.get("PM2_HOME") or str(Path.home() / ".pm2")


def pm2_process(name):
    """The pm2 process description for `name`, or None."""
    out = subprocess.run(["pm2", "jlist"], capture_output=True, text=True).stdout
    start = out.find("[")
    for proc in json.loads(out[start:]) if start >= 0 else []:
        if proc.get("name") == name:
            return proc
    return None


def with_agent(miner_args, name, params):
    """`miner_args` with --agent.name set to `name` (when given) and --agent.params' values replaced by `params`
    (when not None): its values run up to the next option."""
    out = list(miner_args)
    if name:
        if "--agent.name" in out:
            out[out.index("--agent.name") + 1] = name
        else:
            out += ["--agent.name", name]
    if params is not None:
        if "--agent.params" in out:
            start = out.index("--agent.params")
            end = next((i for i in range(start + 1, len(out)) if out[i].startswith("--")), len(out))
            del out[start:end]
        values = params.split()
        if values:
            out += ["--agent.params", *values]
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="miner", help="pm2 process name (run_miner.sh uses 'miner').")
    parser.add_argument("--dir", default=str(REPO / "agents" / "data" / "mainnet_records"))
    parser.add_argument("--validators", default="all", help="'all' or comma-separated validator hotkey prefixes.")
    parser.add_argument("--every", type=int, default=1, help="Keep every n-th round per validator.")
    parser.add_argument("--max-gb", type=float, default=50.0, help="Stop writing at this many GB in --dir.")
    parser.add_argument("--off", action="store_true", help="Switch back to the stock miner.py.")
    parser.add_argument("--agent", default="", help="Also switch --agent.name to this agent (same --agent.path).")
    parser.add_argument("--agent-params", default=None,
                        help="Also replace --agent.params with these space-separated key=value pairs ('' for none).")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    home = pm2_home()
    proc = pm2_process(args.name)
    if proc is None:
        sys.exit(f"No pm2 process named '{args.name}' in pm2 home {home}. Start the miner with run_miner.sh first "
                 f"(from a shell with the same PM2_HOME).")
    env = proc["pm2_env"]
    script = Path(env.get("pm_exec_path", ""))
    target = STOCK if args.off else LAUNCHER
    if script.resolve() == target.resolve():
        print(f"'{args.name}' already runs {target.name}; restarting it with the settings below.")
    miner_args = with_agent(list(env.get("args") or []), args.agent, args.agent_params)
    cwd = env.get("pm_cwd") or str(STOCK.parent)
    interpreter = env.get("exec_interpreter") or "python"
    record_env = {} if args.off else {"MINER_RECORD_DIR": args.dir, "MINER_RECORD_VALIDATORS": args.validators,
                                      "MINER_RECORD_EVERY": str(args.every), "MINER_RECORD_MAX_GB": str(args.max_gb)}
    start = ["pm2", "start", str(target), "--name", args.name, "--interpreter", interpreter, "--cwd", cwd,
             "--update-env", "--", *miner_args]
    print(f"process '{args.name}': {script.name} -> {target.name}")
    print(f"  cwd {cwd}\n  args {' '.join(miner_args)}")
    if record_env:
        print("  recording: " + ", ".join(f"{k}={v}" for k, v in record_env.items()))
    if args.dry_run:
        print("  would run: pm2 delete " + args.name + "\n  would run: " + " ".join(start))
        return
    subprocess.run(["pm2", "delete", args.name], check=False)
    # pm2 passes the calling environment to the process it starts. run_miner.sh exported settings (S3, GenTRX, ...)
    # into the original process's environment, so that is carried over, and the recording settings ride on it.
    original = {k: str(v) for k, v in (env.get("env") or {}).items() if isinstance(v, (str, int, float))}
    environ = {k: v for k, v in {**os.environ, **original}.items() if not k.startswith("MINER_RECORD_")}
    subprocess.run(start, check=True, env={**environ, **record_env})
    subprocess.run(["pm2", "save"], check=False)
    # the tap's start-up log line comes before the miner configures logging, so it is not shown; check the stats file
    print(f"Done. Check it records: python agents/mainnet/miner_health.py  (reply times appear within a minute)")


if __name__ == "__main__":
    main()
