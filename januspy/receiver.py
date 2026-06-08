"""Continuous real-time JANUS receiver.

Runs ``janus-rx`` as a long-lived subprocess reading raw float32 passband samples from
its stdin (the reference's ``raw`` stream driver on ``/dev/stdin``). Audio frames are
fed in via :meth:`RealtimeReceiver.feed`; decoded packets are delivered through an
``on_packet`` callback. The reference never sees EOF, so it detects packets continuously.
"""

from __future__ import annotations

import queue
import select
import subprocess
import threading
import time
from typing import Callable

import numpy as np

from .engine import Detection, JanusEngine, parse_dump, verify_payload

# Idle gap after which an accumulated dump block is considered complete and emitted.
_FLUSH_IDLE_S = 0.20


class RealtimeReceiver:
    """Stream audio into janus-rx and emit decoded packets via callbacks."""

    def __init__(
        self,
        engine: JanusEngine,
        on_packet: Callable[[Detection], None],
        *,
        pset_id: int | None = None,
        fs: int | None = None,
        doppler: bool = True,
        verify: bool = False,
        on_event: Callable[[str], None] | None = None,
    ):
        self.engine = engine
        self.on_packet = on_packet
        self.on_event = on_event
        self.pset_id = pset_id or engine.config.pset_id
        self.fs = fs or engine.config.fs
        self.doppler = doppler
        self.verify = verify  # live-toggleable app-layer payload CRC check

        self._proc: subprocess.Popen | None = None
        self._tx_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=256)
        self._threads: list[threading.Thread] = []
        self._running = False

    # ---- lifecycle ----

    def start(self) -> None:
        """Spawn the janus-rx process and the writer/reader threads."""
        if self._running:
            return
        cfg = self.engine.config
        args = [
            str(cfg.rx_bin),
            "--pset-file", str(cfg.pset_file),
            "--pset-id", str(self.pset_id),
            "--stream-driver", "raw",
            "--stream-driver-args", "/dev/stdin",
            "--stream-fs", str(self.fs),
            "--stream-format", "FLOAT",
            "--stream-passband", "1",
            "--stream-channels", "1",
            "--doppler-correction", "1" if self.doppler else "0",
            "--verbose", "1",
        ]
        self._proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, bufsize=0, env=cfg.env(),
        )
        self._running = True
        self._threads = [
            threading.Thread(target=self._writer_loop, name="janus-rx-writer", daemon=True),
            threading.Thread(target=self._reader_loop, name="janus-rx-reader", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        """Close stdin and terminate the janus-rx process."""
        self._running = False
        try:
            self._tx_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except OSError:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                self._proc.kill()
            self._proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    # ---- feeding audio ----

    def feed(self, frames: np.ndarray) -> None:
        """Queue mono float32 samples for decoding (drops if backed up)."""
        if not self._running:
            return
        data = np.ascontiguousarray(np.asarray(frames, dtype="<f4").ravel())
        try:
            self._tx_queue.put_nowait(data.tobytes())
        except queue.Full:
            pass  # decoder fell behind; better to drop than to stall audio

    # ---- internals ----

    def _writer_loop(self) -> None:
        """Drain queued audio frames into the janus-rx stdin pipe."""
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        while self._running:
            try:
                chunk = self._tx_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if chunk is None:
                break
            try:
                proc.stdin.write(chunk)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                break

    def _reader_loop(self) -> None:
        """Read janus-rx stderr, group it into dumps, and emit detections."""
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        fd = proc.stderr
        block: list[str] = []
        buf = b""
        last_line = time.monotonic()

        def flush_block():
            """Parse the buffered dump lines into detections and dispatch them."""
            if not block:
                return
            text = "\n".join(block)
            block.clear()
            for det in parse_dump(text):
                if self.verify:
                    verify_payload(det)
                try:
                    self.on_packet(det)
                except Exception:  # never let a UI callback kill the reader
                    pass

        while self._running:
            ready, _, _ = select.select([fd], [], [], 0.1)
            now = time.monotonic()
            if ready:
                chunk = fd.read1(65536) if hasattr(fd, "read1") else fd.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    last_line = now
                    if "Triggering detection" in line or "Busy channel" in line:
                        if self.on_event:
                            self.on_event(line.strip())
                        continue
                    if ":" in line and any(line.startswith(g) for g in ("State", "Packet", "Options")):
                        # A new detection's State block starts a fresh emit boundary.
                        if line.startswith("State") and "Parameter Set Id" in line and block:
                            flush_block()
                        block.append(line)
            elif block and (now - last_line) > _FLUSH_IDLE_S:
                flush_block()

        flush_block()
