"""Thin wrapper around the CMRE JANUS reference binaries (janus-tx / janus-rx).

This module locates the built reference, renders packets to audio via ``janus-tx``,
decodes audio via ``janus-rx``, and parses the reference's stderr dump into structured
``Packet`` / ``RxState`` objects. The reference is the single source of truth for the
JANUS protocol; see third_party/reference for the original code.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import integrity

# --- Locating the reference --------------------------------------------------

# The reference submodule lives at third_party/reference; this file is
# januspy/januspy/engine.py, so parents[1] is the project dir. Candidate locations,
# most specific first.
_PROJECT_DIR = Path(__file__).resolve().parents[1]
_REF_CANDIDATES = (
    _PROJECT_DIR / "third_party" / "reference" / "c",  # submodule inside the package
    _PROJECT_DIR / "reference" / "c",                  # reference directly under the project dir
    _PROJECT_DIR.parent / "reference" / "c",           # reference beside the project dir
)

# A copy of pset id 1 shipped inside the package (package-data), used as the last-resort
# parameter-set table when neither the install tree nor the reference source has one
# (e.g. a wheel install driving binaries via $JANUSPY_REF).
_BUNDLED_PSET = Path(__file__).resolve().parent / "data" / "parameter_sets.csv"

# Max cargo bytes the default class-16 / app-0 plugin can encode (6-bit size index).
MAX_DEFAULT_CARGO = 480


class CargoTooLargeError(ValueError):
    """Raised when a message is too long to be encoded as JANUS cargo."""


@dataclass(frozen=True)
class JanusConfig:
    """Resolved locations + defaults for driving the reference binaries."""

    tx_bin: Path
    rx_bin: Path
    pset_file: Path
    plugins_dir: Path | None = None
    pset_id: int = 1
    #: Passband sampling rate handed to the reference (Hz).
    fs: int = 48000

    def env(self) -> dict:
        """Environment for subprocesses (so cargo plugins are found)."""
        e = dict(os.environ)
        if self.plugins_dir is not None:
            e["JANUS_PLUGINS"] = str(self.plugins_dir)
            # The reference also searches LD_LIBRARY_PATH for plugin .so files.
            e["LD_LIBRARY_PATH"] = os.pathsep.join(
                p for p in (str(self.plugins_dir), e.get("LD_LIBRARY_PATH", "")) if p
            )
        return e


def find_reference(install_dir: str | os.PathLike | None = None) -> JanusConfig:
    """Locate the built reference. Honours ``$JANUSPY_REF``, then the bundled submodule.

    Raises FileNotFoundError with a build hint if the binaries are missing.
    """
    explicit = install_dir or os.environ.get("JANUSPY_REF")
    roots = [Path(explicit)] if explicit else [c / "local-install" for c in _REF_CANDIDATES]
    root = roots[0]
    tx = rx = pset = None
    for r in roots:
        if (r / "bin" / "janus-tx").exists():
            root = r
            tx = r / "bin" / "janus-tx"
            rx = r / "bin" / "janus-rx"
            pset = r / "share" / "janus" / "etc" / "parameter_sets.csv"
            break
    if tx is None:  # fall back to PATH — need *both* binaries, not just janus-tx
        which_tx = shutil.which("janus-tx")
        which_rx = shutil.which("janus-rx")
        if which_tx and which_rx:
            tx = Path(which_tx)
            rx = Path(which_rx)
            root = tx.parent.parent
            pset = root / "share" / "janus" / "etc" / "parameter_sets.csv"
    if pset is None or not pset.exists():
        # source CSV if the install tree lacks one; the bundled copy is the last resort.
        for alt in [c / "etc" / "parameter_sets.csv" for c in _REF_CANDIDATES] + [_BUNDLED_PSET]:
            if alt.exists():
                pset = alt
                break
    if tx is None or not tx.exists() or rx is None or not rx.exists():
        raise FileNotFoundError(
            f"JANUS reference binaries not found (looked under {root}).\n"
            "Build them with ./install.sh, or manually:\n"
            "  cd third_party/reference/c && mkdir -p build local-install && cd build &&\n"
            "  cmake -DCMAKE_INSTALL_PREFIX=../local-install -DCMAKE_BUILD_TYPE=Release .. &&\n"
            "  make -j && make install\n"
            "Or set $JANUSPY_REF to the install prefix."
        )
    plugins = root / "share" / "janus" / "plugins"
    return JanusConfig(
        tx_bin=tx,
        rx_bin=rx,
        pset_file=pset,
        plugins_dir=plugins if plugins.exists() else None,
    )


# --- Parsed structures -------------------------------------------------------


@dataclass
class Packet:
    """A decoded (or encoded) JANUS packet, parsed from the reference dump."""

    bytes_hex: str = ""
    version: int | None = None
    mobility: int | None = None
    schedule: int | None = None
    tx_rx: int | None = None
    forward: int | None = None
    class_id: int | None = None
    class_id_name: str = ""
    app_type: int | None = None
    app_data: str = ""
    cargo_size: int = 0
    crc: int | None = None
    crc_ok: bool = False
    cargo_ascii: str = ""
    cargo_hex: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)
    #: app-layer CRC result when --verify is used: True/False, or None if not applicable.
    payload_ok: bool | None = None

    @property
    def payload(self) -> str:
        """Best-effort human-readable payload (plugin payload, else cargo ASCII)."""
        return self.fields.get("Payload") or self.cargo_ascii


@dataclass
class RxState:
    """Receiver state for a detection (timing, Doppler, signal quality)."""

    after: float | None = None
    gamma: float | None = None
    speed: float | None = None
    rssi: int | None = None
    snr: float | None = None
    cfreq: int | None = None
    bwidth: int | None = None
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class Detection:
    """A decoded packet together with its receiver state."""

    packet: Packet
    state: RxState


# --- Dump parsing ------------------------------------------------------------
# The reference prints with JANUS_DUMP -> fprintf(stderr, "%-15s: %-45s: <v>\n", ...).
# Group is the first colon-delimited cell, label the second, value the rest.

_INT = re.compile(r"-?\d+")


def _split_dump_line(line: str) -> tuple[str, str, str, int] | None:
    """Return (group, label, value, indent) or None.

    ``indent`` is the label's own leading-space count (the dump format adds one space
    after each colon, which we strip first); plugin sub-fields are indented by 2.
    """
    i = line.find(":")
    if i < 0:
        return None
    rest = line[i + 1 :]
    j = rest.find(":")
    if j < 0:
        return None
    group = line[:i].strip()
    if group not in ("State", "Packet", "Options"):
        return None
    label_field = rest[:j]
    if label_field.startswith(" "):  # drop the single format space
        label_field = label_field[1:]
    indent = len(label_field) - len(label_field.lstrip(" "))
    label = label_field.strip()
    value = rest[j + 1 :].strip()
    return group, label, value, indent


def _to_int(value: str) -> int | None:
    """Extract the first integer from a dump value, or None."""
    m = _INT.search(value)
    return int(m.group()) if m else None


def parse_dump(text: str) -> list[Detection]:
    """Parse one or more decoded-packet dumps from reference stderr output.

    The reference emits a ``State`` block followed by a ``Packet`` block per detection;
    a new ``State`` block (or "Parameter Set Id" after a packet) starts the next one.
    """
    detections: list[Detection] = []
    state: RxState | None = None
    packet: Packet | None = None
    in_fields = False

    def flush():
        """Append the in-progress detection (if any) and reset the accumulators."""
        nonlocal state, packet
        if packet is not None:
            detections.append(Detection(packet=packet, state=state or RxState()))
        state, packet = None, None

    for line in text.splitlines():
        parsed = _split_dump_line(line)
        if parsed is None:
            continue
        group, label, value, indent = parsed

        if group == "State" and label == "Parameter Set Id":
            flush()  # new detection begins
            state = RxState()
            in_fields = False
        if group == "Packet" and packet is None:
            packet = Packet()

        if group == "State" and state is not None:
            state.raw[label] = value
            if label == "After (s)":
                state.after = float(value)
            elif label == "Gamma":
                state.gamma = float(value)
            elif label == "Speed (m/s)":
                state.speed = _safe_float(value)
            elif label == "RSSI":
                state.rssi = _to_int(value)
            elif label == "SNR":
                state.snr = _safe_float(value)
            elif label == "Center Frequency (Hz)":
                state.cfreq = _to_int(value)
            elif label == "Available Bandwidth (Hz)":
                state.bwidth = _to_int(value)

        elif group == "Packet" and packet is not None:
            packet.raw[label] = value
            # Plugin app-fields are indented and follow "Application Data Fields".
            if in_fields and indent >= 2 and label not in _PACKET_LABELS:
                packet.fields[label] = value
                continue
            if label == "Application Data Fields":
                in_fields = True
            elif label == "Bytes (hex)":
                packet.bytes_hex = value
            elif label == "Version Number (4 bits)":
                packet.version = _to_int(value)
            elif label == "Mobility Flag (1 bit)":
                packet.mobility = _to_int(value)
            elif label == "Schedule Flag (1 bit)":
                packet.schedule = _to_int(value)
            elif label == "Tx/Rx Flag (1 bit)":
                packet.tx_rx = _to_int(value)
            elif label == "Forwarding Capability (1 bit)":
                packet.forward = _to_int(value)
            elif label == "Class User Identifier (8 bits)":
                packet.class_id = _to_int(value)
                m = re.search(r"\((.*)\)", value)
                packet.class_id_name = m.group(1) if m else ""
            elif label == "Application Type (6 bits)":
                packet.app_type = _to_int(value)
            elif label.startswith("Application Data ("):
                packet.app_data = value
            elif label == "Cargo Size":
                packet.cargo_size = _to_int(value) or 0
            elif label == "CRC (8 bits)":
                packet.crc = _to_int(value)
            elif label == "CRC Validity":
                packet.crc_ok = value.strip().startswith("1")
            elif label == "Cargo (ASCII)":
                packet.cargo_ascii = value.strip().strip('"')
            elif label == "Cargo (hex)":
                packet.cargo_hex = value.strip()

    flush()
    return detections


def verify_payload(det: Detection) -> Detection:
    """Check an app-layer CRC-32 suffix on a detection's cargo (in place).

    Sets ``packet.payload_ok`` (True/False/None) and, on success, replaces the displayed
    payload with the CRC-stripped text. Used when decoding with ``verify=True``.
    """
    p = det.packet
    if not p.cargo_hex:
        return det
    res = integrity.unframe(p.cargo_hex)
    if res is None:
        # Cargo present but too short to carry the CRC suffix: treat as corrupted.
        p.payload_ok = False
        return det
    payload, ok = res
    p.payload_ok = ok
    if ok:
        p.cargo_ascii = payload
        p.fields["Payload"] = payload
    return det


def _safe_float(value: str) -> float | None:
    """Parse a float, returning None for non-numeric or NaN/inf values."""
    try:
        f = float(value)
    except ValueError:
        return None
    if math.isnan(f) or math.isinf(f):  # reference prints "-nan" when uncomputable
        return None
    return f


# Packet labels that are structural, not plugin app-fields.
_PACKET_LABELS = {
    "Version Number (4 bits)",
    "Mobility Flag (1 bit)",
    "Schedule Flag (1 bit)",
    "Tx/Rx Flag (1 bit)",
    "Forwarding Capability (1 bit)",
    "Class User Identifier (8 bits)",
    "Application Type (6 bits)",
    "Application Data (26 bits)",
    "Application Data (34 bits)",
    "Cargo Size",
    "CRC (8 bits)",
    "Reservation/Repeat Flag (1 bit)",
    "Reservation Time (7 bits)",
    "Repeat Interval (7 bits)",
}


# --- The engine --------------------------------------------------------------


class JanusEngine:
    """Render and decode JANUS packets using the reference binaries."""

    def __init__(self, config: JanusConfig | None = None):
        self.config = config or find_reference()

    # ---- transmit ----

    def _tx_args(self, *, cargo=None, app_fields=None, class_id=None, app_type=None,
                 pset_id=None, fs=None, extra=None) -> list[str]:
        """Assemble the janus-tx command-line arguments."""
        cfg = self.config
        args = [
            str(cfg.tx_bin),
            "--pset-file", str(cfg.pset_file),
            "--pset-id", str(pset_id or cfg.pset_id),
            "--stream-fs", str(fs or cfg.fs),
            "--verbose", "1",
        ]
        if class_id is not None:
            args += ["--packet-class-id", str(class_id)]
        if app_type is not None:
            args += ["--packet-app-type", str(app_type)]
        if app_fields:
            args += ["--packet-app-fields", app_fields]
        if cargo is not None:
            if isinstance(cargo, bytes):
                cargo = cargo.decode("latin-1")
            args += ["--packet-cargo", cargo]
        if extra:
            args += list(extra)
        return args

    def render(self, *, cargo: str | bytes | None = None, app_fields: str | None = None,
               class_id: int | None = None, app_type: int | None = None,
               pset_id: int | None = None, fs: int | None = None,
               verify: bool = False,
               extra: list[str] | None = None) -> tuple[np.ndarray, int, Packet]:
        """Render a packet to a mono float32 passband waveform.

        Returns ``(samples, fs, packet)``. ``packet`` is parsed from the encoder dump.
        With ``verify`` the payload is wrapped with an app-layer CRC-32 (non-standard;
        decode with ``verify=True`` to check it).
        """
        cfg = self.config
        fs = fs or cfg.fs
        if verify and cargo:
            cargo = integrity.frame(cargo if isinstance(cargo, str) else cargo.decode("latin-1"))
        with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tf:
            raw_path = tf.name
        try:
            args = self._tx_args(cargo=cargo, app_fields=app_fields, class_id=class_id,
                                  app_type=app_type, pset_id=pset_id, fs=fs,
                                  extra=["--stream-driver", "raw",
                                         "--stream-format", "FLOAT",
                                         "--stream-passband", "1",
                                         "--stream-driver-args", raw_path] + (extra or []))
            proc = subprocess.run(args, capture_output=True, env=cfg.env())
            if proc.returncode != 0:
                raise RuntimeError(
                    f"janus-tx failed ({proc.returncode}): {proc.stderr.decode('utf-8','replace')}")
            samples = np.fromfile(raw_path, dtype="<f4")
            dets = parse_dump(proc.stderr.decode("utf-8", "replace"))
            packet = dets[0].packet if dets else Packet()
            # The reference silently drops cargo that the class/app's size field can't
            # represent (the default class 16 / app 0 plugin caps at 480 bytes). Catch it.
            if cargo:
                want = len(cargo if isinstance(cargo, (bytes, bytearray))
                           else str(cargo).encode("latin-1", "replace"))
                if packet.cargo_size < want:
                    raise CargoTooLargeError(
                        f"message of {want} bytes was not encoded as JANUS cargo "
                        f"(class {packet.class_id}/app {packet.app_type}); the default "
                        f"reference plugin supports up to {MAX_DEFAULT_CARGO} bytes."
                    )
            return samples, fs, packet
        finally:
            try:
                os.unlink(raw_path)
            except OSError:
                pass

    # ---- receive (offline) ----

    def decode_array(self, samples: np.ndarray, fs: int | None = None,
                     pset_id: int | None = None, doppler: bool = True,
                     verify: bool = False) -> list[Detection]:
        """Decode JANUS packets from an in-memory mono passband float array."""
        cfg = self.config
        fs = fs or cfg.fs
        data = np.ascontiguousarray(np.asarray(samples, dtype="<f4").ravel())
        args = [
            str(cfg.rx_bin),
            "--pset-file", str(cfg.pset_file),
            "--pset-id", str(pset_id or cfg.pset_id),
            "--stream-driver", "raw",
            "--stream-driver-args", "/dev/stdin",
            "--stream-fs", str(fs),
            "--stream-format", "FLOAT",
            "--stream-passband", "1",
            "--stream-channels", "1",
            "--doppler-correction", "1" if doppler else "0",
            "--verbose", "1",
        ]
        proc = subprocess.run(args, input=data.tobytes(), capture_output=True, env=cfg.env())
        dets = parse_dump(proc.stderr.decode("utf-8", "replace"))
        return [verify_payload(d) for d in dets] if verify else dets

    def decode_wav(self, path: str | os.PathLike, pset_id: int | None = None,
                   verify: bool = False) -> list[Detection]:
        """Decode a WAV file. Reads via soundfile then uses the raw passband path.

        Going through soundfile (rather than the reference's wav stream driver) accepts
        any subtype/rate soundfile can read; the first channel is used if multichannel.
        """
        import soundfile as sf

        data, fs = sf.read(str(path), dtype="float32", always_2d=True)
        mono = data[:, 0]
        return self.decode_array(mono, fs=int(fs), pset_id=pset_id, verify=verify)
