"""Real-time sound-card I/O and a live spectrogram (waterfall) analyzer.

Owns the audio device via sounddevice (PortAudio): it captures the microphone/line-in,
fans frames out to the streaming :class:`RealtimeReceiver` and to a
:class:`WaterfallAnalyzer`, and plays transmitted waveforms. The JANUS DSP itself stays
in the reference binaries — this module only moves samples around.
"""

from __future__ import annotations

import threading
from typing import Callable

import numpy as np

try:
    import sounddevice as sd
except OSError:  # PortAudio shared lib missing
    sd = None

from .engine import Detection, JanusEngine
from .receiver import RealtimeReceiver


def _require_sd():
    """Raise a clear error if sounddevice/PortAudio is unavailable."""
    if sd is None:
        raise RuntimeError(
            "sounddevice/PortAudio is unavailable. Install libportaudio2 "
            "(e.g. `apt-get install libportaudio2`) to use live audio. "
            "File/loopback decoding still works without it."
        )


def list_devices() -> dict:
    """Return available input/output devices and the current defaults."""
    if sd is None:
        return {"inputs": [], "outputs": [], "default_input": None, "default_output": None}
    devs = sd.query_devices()
    inputs, outputs = [], []
    for i, d in enumerate(devs):
        entry = {
            "index": i,
            "name": d["name"],
            "channels_in": d["max_input_channels"],
            "channels_out": d["max_output_channels"],
            "default_samplerate": int(d["default_samplerate"]),
        }
        if d["max_input_channels"] > 0:
            inputs.append(entry)
        if d["max_output_channels"] > 0:
            outputs.append(entry)
    di, do = sd.default.device
    return {"inputs": inputs, "outputs": outputs,
            "default_input": di, "default_output": do}


class WaterfallAnalyzer:
    """Sliding-window magnitude spectrogram restricted to a frequency band."""

    def __init__(self, fs: int, on_row: Callable[[list[float]], None],
                 fmin: float = 8000.0, fmax: float = 15000.0,
                 nfft: int = 2048, hop: int | None = None,
                 floor_db: float = -80.0, ceil_db: float = 0.0):
        self.fs = fs
        self.on_row = on_row
        self.nfft = nfft
        self.hop = hop or nfft // 2
        self.floor_db = floor_db
        self.ceil_db = ceil_db
        self._win = np.hanning(nfft).astype(np.float32)
        freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
        self._band = np.where((freqs >= fmin) & (freqs <= fmax))[0]
        self.fmin = float(freqs[self._band[0]]) if len(self._band) else fmin
        self.fmax = float(freqs[self._band[-1]]) if len(self._band) else fmax
        self._buf = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()

    @property
    def bins(self) -> int:
        """Number of frequency bins emitted per row."""
        return len(self._band)

    def config(self) -> dict:
        """The effective settings (snapped to FFT-bin frequencies)."""
        return {
            "fmin": round(self.fmin, 1),
            "fmax": round(self.fmax, 1),
            "nfft": self.nfft,
            "floor_db": self.floor_db,
            "ceil_db": self.ceil_db,
            "bins": self.bins,
            "fs": self.fs,
        }

    def feed(self, frames: np.ndarray) -> None:
        """Add samples and emit a normalised magnitude row per completed FFT window."""
        rows: list[np.ndarray] = []
        with self._lock:
            self._buf = np.concatenate([self._buf, np.asarray(frames, dtype=np.float32).ravel()])
            while self._buf.size >= self.nfft:
                seg = self._buf[: self.nfft]
                self._buf = self._buf[self.hop :]
                spec = np.fft.rfft(seg * self._win)
                mag = np.abs(spec[self._band]) / self.nfft
                db = 20.0 * np.log10(mag + 1e-12)
                norm = (db - self.floor_db) / (self.ceil_db - self.floor_db)
                rows.append(np.clip(norm, 0.0, 1.0))
        for row in rows:
            try:
                self.on_row([round(float(v), 4) for v in row])
            except Exception:
                pass


class AudioEngine:
    """Full-duplex-ish modem: capture -> decode, and render -> playback."""

    def __init__(self, engine: JanusEngine | None = None, *,
                 on_packet: Callable[[Detection], None] | None = None,
                 on_waterfall: Callable[[list[float]], None] | None = None,
                 on_event: Callable[[str], None] | None = None,
                 on_tx: Callable[[dict], None] | None = None):
        self.engine = engine or JanusEngine()
        self.on_packet = on_packet or (lambda d: None)
        self.on_waterfall = on_waterfall or (lambda row: None)
        self.on_event = on_event or (lambda e: None)
        self.on_tx = on_tx or (lambda info: None)

        self.fs = self.engine.config.fs
        self.pset_id = self.engine.config.pset_id
        self.verify = False  # opt-in app-layer payload CRC (TX wraps, RX checks)
        # User-adjustable spectrogram settings (persist across start/stop).
        self.waterfall_settings: dict = {
            "fmin": 8000.0, "fmax": 15000.0, "nfft": 2048,
            "floor_db": -80.0, "ceil_db": 0.0,
        }
        self._receiver: RealtimeReceiver | None = None
        self._waterfall: WaterfallAnalyzer | None = None
        self._in_stream = None
        self._running = False
        self._lock = threading.Lock()
        self._play_lock = threading.Lock()  # serialise output so overlapping TX don't collide

    @property
    def running(self) -> bool:
        """True while the receiver/input stream is active."""
        return self._running

    def _new_waterfall(self) -> WaterfallAnalyzer:
        """Create a WaterfallAnalyzer with the current settings."""
        return WaterfallAnalyzer(self.fs, on_row=self.on_waterfall, **self.waterfall_settings)

    def configure_waterfall(self, **settings) -> dict:
        """Update spectrogram settings; applies live if RX is running.

        Accepts fmin, fmax (Hz), nfft (power of two), floor_db, ceil_db. Returns the
        effective config (frequencies snapped to FFT bins).
        """
        allowed = {"fmin", "fmax", "nfft", "floor_db", "ceil_db"}
        for key, value in settings.items():
            if key not in allowed or value is None:
                continue
            if key == "nfft":
                # clamp to a sane power of two
                n = int(value)
                n = max(256, min(8192, 1 << (n - 1).bit_length()))
                self.waterfall_settings["nfft"] = n
            else:
                self.waterfall_settings[key] = float(value)
        # sanity: keep the band ordered and within Nyquist
        nyq = self.fs / 2.0
        s = self.waterfall_settings
        s["fmin"] = max(0.0, min(s["fmin"], nyq - 1))
        s["fmax"] = max(s["fmin"] + 1, min(s["fmax"], nyq))
        if s["ceil_db"] <= s["floor_db"]:
            s["ceil_db"] = s["floor_db"] + 1.0
        with self._lock:
            if self._running:
                self._waterfall = self._new_waterfall()
        return self.waterfall_config()

    def waterfall_config(self) -> dict:
        """The effective spectrogram settings (from the live analyzer, or the next one)."""
        if self._waterfall is not None:
            return self._waterfall.config()
        return self._new_waterfall().config()

    # ---- receive ----

    def set_verify(self, verify: bool) -> bool:
        """Enable/disable app-layer payload CRC (applies to TX and live RX)."""
        self.verify = bool(verify)
        if self._receiver is not None:
            self._receiver.verify = self.verify
        return self.verify

    def start_rx(self, *, input_device=None, fs: int | None = None,
                 pset_id: int | None = None, doppler: bool = True) -> None:
        """Open the input device and begin continuous decoding + waterfall."""
        _require_sd()
        with self._lock:
            if self._running:
                return
            self.fs = fs or self.fs
            self.pset_id = pset_id or self.pset_id
            self._receiver = RealtimeReceiver(
                self.engine, on_packet=self.on_packet, pset_id=self.pset_id,
                fs=self.fs, doppler=doppler, verify=self.verify, on_event=self.on_event)
            self._receiver.start()
            self._waterfall = self._new_waterfall()

            def callback(indata, frames, time_info, status):
                """Fan captured frames out to the receiver and the waterfall."""
                x = indata[:, 0].copy()
                if self._receiver:
                    self._receiver.feed(x)
                if self._waterfall:
                    self._waterfall.feed(x)

            self._in_stream = sd.InputStream(
                samplerate=self.fs, channels=1, dtype="float32",
                device=input_device, blocksize=0, callback=callback)
            self._in_stream.start()
            self._running = True

    def stop_rx(self) -> None:
        """Stop the input stream and the receiver."""
        with self._lock:
            self._running = False
            if self._in_stream is not None:
                try:
                    self._in_stream.stop()
                    self._in_stream.close()
                except Exception:
                    pass
                self._in_stream = None
            if self._receiver is not None:
                self._receiver.stop()
                self._receiver = None
            self._waterfall = None

    # ---- transmit ----

    def transmit(self, *, cargo: str | None = None, app_fields: str | None = None,
                 class_id: int | None = None, app_type: int | None = None,
                 output_device=None, monitor: bool = True,
                 loopback: bool = False) -> dict:
        """Render a packet and play it. Returns the encoded packet summary.

        ``monitor`` also routes the transmitted samples into the waterfall so the
        operator sees their own transmission. ``loopback`` additionally feeds the
        samples into the active receiver (digital self-test, no acoustic path needed).
        """
        samples, fs, packet = self.engine.render(
            cargo=cargo, app_fields=app_fields, class_id=class_id,
            app_type=app_type, pset_id=self.pset_id, fs=self.fs, verify=self.verify)
        info = {
            "payload": cargo if (self.verify and cargo) else (packet.payload or (cargo or "")),
            "verified": bool(self.verify and cargo),
            "class_id": packet.class_id,
            "app_type": packet.app_type,
            "bytes_hex": packet.bytes_hex,
            "duration": round(len(samples) / fs, 3),
            "crc_ok": packet.crc_ok,
        }
        self.on_tx(info)

        def _play():
            """Render-and-play worker run on a background thread."""
            if monitor and self._waterfall is not None:
                self._waterfall.feed(samples)
            if loopback and self._receiver is not None:
                # Split into frame-sized writes so the streaming detector ingests it the
                # way it would live capture frames (the reference reads from the pipe as
                # fast as it can, so this is shaping, not real-time pacing).
                block = max(1, fs // 20)
                for i in range(0, len(samples), block):
                    self._receiver.feed(samples[i : i + block])
            if sd is not None:
                try:
                    # One transmission owns the output device at a time.
                    with self._play_lock:
                        sd.play(samples, samplerate=fs, device=output_device, blocking=True)
                except Exception as exc:  # no output device; monitor/loopback still ran
                    self.on_event(f"playback unavailable: {exc}")

        threading.Thread(target=_play, name="janus-tx-play", daemon=True).start()
        return info

    def close(self) -> None:
        """Release all audio resources."""
        self.stop_rx()
