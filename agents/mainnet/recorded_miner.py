# SPDX-License-Identifier: MIT
"""
Runs the stock miner (taos/im/neurons/miner.py, unchanged) with every validator state update recorded.

The miner attaches `self.forward` to its axon in BaseMinerNeuron.__init__. Before that runs, this gives the
instance a forward that hands the round to agents/mainnet/state_tap.py (references only; the writing happens on a
background thread) and then calls the miner's own forward, so replies are what they would be without it. No
tracked file is edited, so run_miner.sh's `git pull` stays clean.

It takes exactly the miner's arguments; recording settings come from the environment (see state_tap.py):
    MINER_RECORD_DIR, MINER_RECORD_VALIDATORS, MINER_RECORD_EVERY, MINER_RECORD_MAX_GB

Usually started by agents/mainnet/recorder_on.py, which restarts the pm2 `miner` process that run_miner.sh created
under this launcher with the same arguments. By hand (from taos/im/neurons, as run_miner.sh runs the miner):
    python ../../../agents/mainnet/recorded_miner.py --netuid 79 --wallet.name ... (the usual miner arguments)
"""
import os
import runpy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# miner.py re-executes itself once to set MALLOC_ARENA_MAX before glibc allocates; doing it here keeps that
# re-exec on this launcher, so the recording hook survives it.
if os.environ.get("MALLOC_ARENA_MAX") is None:
    os.environ["MALLOC_ARENA_MAX"] = "2"
    os.execvp(sys.executable, [sys.executable] + sys.argv)

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time  # noqa: E402

import bittensor as bt  # noqa: E402
from state_tap import StateTap  # noqa: E402
from taos.common.neurons import miner as base_miner  # noqa: E402
from taos.im.protocol import MarketSimulationStateUpdate  # noqa: E402

_original_init = base_miner.BaseMinerNeuron.__init__


def _recording_init(self, *args, **kwargs):
    tap = StateTap.from_env()
    inner = self.forward     # the subclass's own forward, bound

    async def forward(synapse: MarketSimulationStateUpdate) -> MarketSimulationStateUpdate:
        captured = None
        try:
            captured = tap.capture(synapse)
        except Exception as ex:
            bt.logging.warning(f"state tap: capture failed ({ex})")
        start = time.time()
        hotkey = getattr(getattr(synapse, "dendrite", None), "hotkey", None)
        try:
            result = await inner(synapse)
        except Exception as error:
            # the round that broke the agent is the one most worth keeping
            try:
                tap.observe(hotkey, time.time() - start)
                tap.finish(captured, None, time.time() - start, error=repr(error))
            except Exception as ex:
                bt.logging.warning(f"state tap: queue failed ({ex})")
            raise
        try:
            tap.observe(hotkey, time.time() - start)
            tap.finish(captured, synapse, time.time() - start)
        except Exception as ex:
            bt.logging.warning(f"state tap: queue failed ({ex})")
        return result

    # the axon reads the synapse type from the annotation, so the wrapper keeps the original's
    forward.__doc__ = inner.__doc__
    self.forward = forward
    self.state_tap = tap
    bt.logging.info(f"state tap: recording validator rounds to {tap.directory} "
                    f"(validators {tap.prefixes or 'all'}, every {tap.every}, limit {tap.max_bytes / 1e9:.0f} GB)")
    _original_init(self, *args, **kwargs)


base_miner.BaseMinerNeuron.__init__ = _recording_init

if __name__ == "__main__":
    miner = REPO / "taos" / "im" / "neurons" / "miner.py"
    sys.argv[0] = str(miner)
    runpy.run_path(str(miner), run_name="__main__")
