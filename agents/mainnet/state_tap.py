# SPDX-License-Identifier: MIT
"""
Records the state updates real validators send a miner, exactly as they arrive, with the agent's reply.

Each frame is one validator round: the synapse's compressed payload untouched (books and payload as the validator
encoded them), the validator's hotkey, its version and compression engine, the simulation timestamp, the wall time
the request arrived, how long the miner took to answer, and the reply the agent gave (instructions as JSON), or
the error when the miner failed on the round.
`decode_state` turns a frame back into a MarketSimulationStateUpdate through the protocol's own decompress, so a
recorded round is what the agent saw.

Writing never holds up a reply: the request thread only takes references and queues them; a background thread
serializes, compresses and writes. When the queue is full or the disk budget is spent, frames are dropped and
counted, never waited for. Files are zstd streams of length-prefixed msgpack frames (the layout of
agents/proxy/state_recorder.py), one per validator per hour:

    <dir>/<validator hotkey>/<YYYYmmdd_HH>.frames.zst

Settings come from the environment, so the miner's own argument parser never sees them:
    MINER_RECORD_DIR         where to write (default agents/data/mainnet_records)
    MINER_RECORD_VALIDATORS  "all" (default) or comma-separated hotkey prefixes of the validators to keep
    MINER_RECORD_EVERY       keep every n-th round per validator (default 1: all)
    MINER_RECORD_MAX_GB      stop writing once the directory holds this much (default 50)
"""
import os
import queue
import threading
import time
from pathlib import Path

import msgpack
import zstandard

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DIR = REPO / "agents" / "data" / "mainnet_records"
WINDOW_LOG = 27
STATS_FILE = "tap_stats.json"


class StateTap:
    """Queues validator rounds for a background writer; see the module docstring for the frame contents."""

    def __init__(self, directory=None, validators="all", every=1, max_gb=50.0, queue_size=512):
        self.directory = Path(directory or DEFAULT_DIR)
        self.directory.mkdir(parents=True, exist_ok=True)
        keep = (validators or "all").strip()
        self.prefixes = None if keep.lower() == "all" else [p.strip() for p in keep.split(",") if p.strip()]
        self.every = max(1, int(every))
        self.max_bytes = float(max_gb) * 1e9
        self.counts = {}            # rounds seen per validator, for sampling
        self.recent = {}            # validator -> deque of (arrival time, reply seconds), every round, kept or not
        self.stats = {"written": 0, "dropped_queue": 0, "dropped_disk": 0, "skipped": 0, "errors": 0}
        self._queue = queue.Queue(maxsize=queue_size)
        self._bytes = sum(p.stat().st_size for p in self.directory.rglob("*.frames.zst"))
        self._thread = threading.Thread(target=self._run, name="state-tap", daemon=True)
        self._thread.start()

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("MINER_RECORD_DIR") or None, os.environ.get("MINER_RECORD_VALIDATORS", "all"),
                   int(os.environ.get("MINER_RECORD_EVERY", "1")), float(os.environ.get("MINER_RECORD_MAX_GB", "50")))

    def wanted(self, hotkey):
        """True when this validator's round should be kept (validator filter, then every n-th round)."""
        if self.prefixes is not None and not any((hotkey or "").startswith(p) for p in self.prefixes):
            return False
        n = self.counts.get(hotkey, 0)
        self.counts[hotkey] = n + 1
        return n % self.every == 0

    def capture(self, synapse):
        """Takes what the round arrived with, before the miner decompresses it; None when it is not kept."""
        hotkey = getattr(getattr(synapse, "dendrite", None), "hotkey", None)
        if not synapse.compressed or not self.wanted(hotkey):
            self.stats["skipped"] += 1
            return None
        return {"validator": hotkey, "received": time.time(), "timestamp": synapse.timestamp,
                "version": synapse.version, "engine": synapse.compression_engine, "compressed": synapse.compressed}

    def observe(self, hotkey, seconds):
        """Every round's reply time, recorded or not, for agents/mainnet/miner_health.py."""
        from collections import deque
        self.recent.setdefault(hotkey or "unknown", deque(maxlen=300)).append((time.time(), seconds))

    def finish(self, captured, synapse, seconds, error=None):
        """Queues the round with the agent's reply (or, when the miner failed, the error); never blocks."""
        if captured is None:
            return
        captured["seconds"] = seconds
        captured["response"] = getattr(synapse, "response", None) if synapse is not None else None
        captured["error"] = error
        if self._bytes >= self.max_bytes:
            self.stats["dropped_disk"] += 1
            # _bytes was only ever counted up, so freeing space never resumed recording until a restart (25 Oct:
            # stopped 11:18 CET at --max-gb 15). Re-measure the folder now and then; a cleared folder resumes.
            if self.stats["dropped_disk"] % 200 == 0:
                try:
                    self._bytes = sum(p.stat().st_size for p in self.directory.rglob("*.frames.zst"))
                except OSError:
                    pass
            return
        try:
            self._queue.put_nowait(captured)
        except queue.Full:
            self.stats["dropped_queue"] += 1

    def _run(self):
        writers = {}            # (validator, hour) -> (file, zstd writer)
        params = zstandard.ZstdCompressionParameters.from_level(3, window_log=WINDOW_LOG)
        last_stats = 0.0
        while True:
            try:
                # wakes at least every 30 s, so the stats file stays current while no frame is kept
                frame = self._queue.get(timeout=30)
            except queue.Empty:
                frame = None
            try:
                if frame is None:
                    raise LookupError
                response = frame.pop("response")
                if response is not None and hasattr(response, "model_dump"):
                    response = response.model_dump(mode="json")
                frame["response"] = response
                body = msgpack.packb(frame, use_bin_type=True, default=str)
                hour = time.strftime("%Y%m%d_%H", time.gmtime(frame["received"]))
                key = (frame["validator"] or "unknown", hour)
                if key not in writers:
                    for old in [k for k in writers if k[0] == key[0]]:
                        writers.pop(old)[1].close()
                    folder = self.directory / key[0]
                    folder.mkdir(parents=True, exist_ok=True)
                    fh = open(folder / f"{hour}.frames.zst", "ab")
                    writers[key] = (fh, zstandard.ZstdCompressor(compression_params=params).stream_writer(fh))
                fh, writer = writers[key]
                before = fh.tell()
                writer.write(len(body).to_bytes(8, "little") + body)
                # a block flush per frame, then the file's own buffer, keeps a file readable to its last frame
                # while it is still being written
                writer.flush(zstandard.FLUSH_BLOCK)
                fh.flush()
                self._bytes += fh.tell() - before
                self.stats["written"] += 1
            except LookupError:
                pass
            except Exception:
                self.stats["errors"] += 1
            if time.time() - last_stats > 60:
                last_stats = time.time()
                try:
                    import json
                    replies = {}
                    for validator, rounds in list(self.recent.items()):
                        took = sorted(s for _, s in rounds)
                        if took:
                            replies[validator] = {"rounds": len(took), "p50": took[len(took) // 2],
                                                  "p90": took[int(0.9 * (len(took) - 1))], "max": took[-1],
                                                  "last_round": rounds[-1][0]}
                    (self.directory / STATS_FILE).write_text(json.dumps(
                        {**self.stats, "bytes": self._bytes, "max_bytes": self.max_bytes,
                         "validators": {k or "unknown": v for k, v in self.counts.items()},
                         "reply_seconds": replies, "at": time.time()}, indent=2))
                except OSError:
                    pass


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _zstd_frame_starts(data):
    """Offsets where a zstd frame starts in a recording file. A miner restart appends a new frame after the old
    process's unclosed one, so a file can hold several; the magic number can also occur by chance inside compressed
    data, so each candidate after the first must open as a frame header."""
    starts, at = [], data.find(ZSTD_MAGIC)
    while at >= 0:
        if not starts:
            starts.append(at)
        else:
            try:
                zstandard.ZstdDecompressor(max_window_size=1 << WINDOW_LOG).decompressobj().decompress(
                    data[at:at + (1 << 16)])
                starts.append(at)
            except zstandard.ZstdError:
                pass
        at = data.find(ZSTD_MAGIC, at + 1)
    return starts


def read_frames(path, chunk_size=1 << 20):
    """Yields each frame of one recording file as a dict; tolerates a file cut short or still being written, and a
    file that a restarted miner appended to (one zstd frame per process, the earlier ones never closed).

    An incremental decompressor rather than a stream reader: a file being written ends in an unclosed zstd frame,
    whose flushed blocks a stream reader holds back until the frame ends, so the newest rounds would be missing.
    Each zstd frame is decompressed on its own, up to where the next one starts: fed on into the next frame, the
    decompressor of an unclosed one fails, and every round recorded after the restart was lost."""
    import mmap
    with open(path, "rb") as fh:
        try:
            data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:          # an empty file
            return
        try:
            starts = _zstd_frame_starts(data)
            for begin, end in zip(starts, starts[1:] + [len(data)]):
                decompressor = zstandard.ZstdDecompressor(max_window_size=1 << WINDOW_LOG).decompressobj()
                buffer = bytearray()
                for offset in range(begin, end, chunk_size):
                    try:
                        buffer += decompressor.decompress(data[offset:min(end, offset + chunk_size)])
                    except zstandard.ZstdError:
                        break
                    start = 0
                    while len(buffer) - start >= 8:
                        size = int.from_bytes(buffer[start:start + 8], "little")
                        if len(buffer) - start - 8 < size:
                            break
                        yield msgpack.unpackb(bytes(buffer[start + 8:start + 8 + size]), raw=False, strict_map_key=False)
                        start += 8 + size
                    del buffer[:start]
                # what is left is the last record of a process that was stopped mid-write: incomplete, dropped
        finally:
            data.close()


def decode_state(frame, lazy=False):
    """The MarketSimulationStateUpdate the agent saw for this frame, decompressed by the protocol itself."""
    from taos.im.protocol import MarketSimulationStateUpdate
    synapse = MarketSimulationStateUpdate(timestamp=frame["timestamp"], version=frame.get("version"),
                                          compression_engine=frame.get("engine") or "lz4",
                                          compressed=frame["compressed"])
    return synapse.decompress(lazy=lazy)


def recordings(directory=None):
    """{validator hotkey: [recording files, oldest first]} under a recording directory."""
    root = Path(directory or DEFAULT_DIR)
    return {d.name: sorted(d.glob("*.frames.zst")) for d in sorted(root.iterdir()) if d.is_dir()} if root.exists() else {}
