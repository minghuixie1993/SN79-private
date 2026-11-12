# SPDX-License-Identifier: MIT
"""
Records every raw simulator state the local proxy receives, for offline scoring.

The file holds the msgpack bytes exactly as the simulator published them (before the multi-asset
flatten), so `agents/eval/score_run.py` can replay them through the validator's own accounting and
scoring code (`trade.update_trade_volumes`, `reward.score_uids`, `report.report_worker`), the same path
the scoring shadow uses. Frames are an 8-byte little-endian length followed by the payload, inside one
zstd stream. Writing happens on a background thread so a slow disk never delays a round.

Enable from the launcher config:
    "proxy": { ..., "record_states": true }               -> agents/data/records/<YYYYmmdd_HHMMSS>/
    "proxy": { ..., "record_states": "/some/directory" }  -> that directory
"""
import json
import os
import queue
import threading
import time
from pathlib import Path

import bittensor as bt
import zstandard

STATES_FILE = "states.msgpack.zst"
META_FILE = "meta.json"
WINDOW_LOG = 27


def read_frames(path):
    """Yields each recorded raw state, in order; tolerates a file cut short by a killed proxy."""
    with open(path, "rb") as fh:
        reader = zstandard.ZstdDecompressor(max_window_size=1 << WINDOW_LOG).stream_reader(fh)
        while True:
            header = reader.read(8)
            if len(header) < 8:
                return
            size = int.from_bytes(header, byteorder="little")
            payload = reader.read(size)
            if len(payload) < size:
                return
            yield payload


class StateRecorder:
    """Background writer for raw state frames."""

    def __init__(self, directory, meta: dict):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / META_FILE, "w") as fh:
            json.dump(meta, fh, indent=2)
        self._queue = queue.Queue(maxsize=256)
        self._frames = 0
        self._dropped = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name="StateRecorder")
        self._thread.start()
        bt.logging.info(f"Recording simulator states to {self.directory / STATES_FILE}")

    def put(self, raw: bytes) -> None:
        """Queues one frame; drops it (and counts the drop) rather than block the round."""
        try:
            self._queue.put_nowait(raw)
        except queue.Full:
            self._dropped += 1
            bt.logging.warning(f"State recorder queue full: frame dropped ({self._dropped} so far)")

    def _run(self):
        with open(self.directory / STATES_FILE, "wb") as fh:
            # A block flush per frame lets a replay read a run that is still going or was killed, and, unlike a
            # frame flush, keeps the compression window: consecutive states share most of their bytes.
            # A 128 MB window with long-distance matching reaches back across many ~7 MB states: about 9x
            # smaller than a plain level 3 stream (about 0.12 MB per state on the 128-book layout).
            params = zstandard.ZstdCompressionParameters.from_level(3, window_log=WINDOW_LOG, enable_ldm=True)
            writer = zstandard.ZstdCompressor(compression_params=params).stream_writer(fh)
            while True:
                raw = self._queue.get()
                writer.write(len(raw).to_bytes(8, byteorder="little"))
                writer.write(raw)
                writer.flush(zstandard.FLUSH_BLOCK)
                self._frames += 1
                if self._frames % 600 == 0:
                    bt.logging.info(f"State recorder: {self._frames} frames written, {self._dropped} dropped")


def make_recorder(setting, simulator_config_file: str, agent_names: list, timeout: float):
    """Builds a recorder from the launcher's `proxy.record_states` value, or returns None when it is off."""
    if not setting:
        return None
    if setting is True or str(setting).lower() in ("1", "true", "yes"):
        directory = Path(__file__).resolve().parents[1] / "data" / "records" / time.strftime("%Y%m%d_%H%M%S")
    else:
        directory = Path(os.path.expanduser(str(setting)))
    meta = {
        "simulation_xml": simulator_config_file,
        "agents": agent_names,
        "timeout": timeout,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return StateRecorder(directory, meta)
