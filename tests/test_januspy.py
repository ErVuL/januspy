"""Tests for the januspy reference wrapper.

These exercise the real CMRE binaries, so they're skipped if the reference isn't
built. The DSP correctness itself is the reference's responsibility — here we verify
the wrapper renders, decodes, parses, and round-trips faithfully.
"""

from __future__ import annotations

import numpy as np
import pytest

from januspy import integrity
from januspy.engine import JanusEngine, parse_dump
from januspy.psets import load_psets

try:
    _ENGINE = JanusEngine()
    _HAVE_REF = True
except FileNotFoundError:
    _ENGINE = None
    _HAVE_REF = False

needs_ref = pytest.mark.skipif(not _HAVE_REF, reason="JANUS reference not built")


def test_parse_dump_unit():
    text = "\n".join([
        "State          : Parameter Set Id                             : 1",
        "State          : Center Frequency (Hz)                        : 11520",
        "State          : After (s)                                    : 0.031250",
        "State          : Gamma                                        : 1.000000",
        "State          : Speed (m/s)                                  : 0.000226",
        "Packet         : Bytes (hex)                                  : | 32| 10| 00| 00| 00| 00| 05| 4E|",
        "Packet         :   Class User Identifier (8 bits)             : 16 (NATO JANUS reference Implementation)",
        "Packet         :   Application Type (6 bits)                  : 0",
        "Packet         : CRC Validity                                 : 1",
        "Packet         : Application Data Fields                      : ",
        "Packet         :   Payload Size                               :  16",
        "Packet         :   Payload                                    : hi there",
        'Packet         : Cargo (ASCII)                                : "hi there"',
    ])
    dets = parse_dump(text)
    assert len(dets) == 1
    d = dets[0]
    assert d.packet.crc_ok is True
    assert d.packet.class_id == 16
    assert d.packet.class_id_name.startswith("NATO")
    assert d.packet.app_type == 0
    assert d.packet.cargo_ascii == "hi there"
    assert d.packet.fields["Payload"] == "hi there"
    assert d.packet.payload == "hi there"
    assert abs(d.state.after - 0.03125) < 1e-6
    assert abs(d.state.gamma - 1.0) < 1e-9


def test_parse_handles_nan_speed():
    text = "\n".join([
        "State          : Parameter Set Id                             : 1",
        "State          : Speed (m/s)                                  : -nan",
        "Packet         : CRC Validity                                 : 0",
    ])
    d = parse_dump(text)[0]
    assert d.state.speed is None
    assert d.packet.crc_ok is False


def test_integrity_frame_unframe():
    framed = integrity.frame("hello world")
    hexs = " ".join(f"{b:02X}" for b in framed.encode())
    assert integrity.unframe(hexs) == ("hello world", True)
    # padding (the reference zero-pads cargo) is tolerated
    assert integrity.unframe(hexs + " 00 00 00") == ("hello world", True)
    # a tampered byte is detected
    bad = bytearray(framed.encode())
    bad[0] ^= 0x01
    payload, ok = integrity.unframe(" ".join(f"{b:02X}" for b in bad))
    assert ok is False
    # Heavy corruption whose CRC suffix isn't valid hex must be reported as a
    # failure (False), not as "not framed" (None).
    garbage = "68 65 6C 6C 6F A7 19 FF 2C 81 44 9E"
    res = integrity.unframe(garbage)
    assert res is not None and res[1] is False


def test_load_psets_default():
    assert _HAVE_REF, "reference required for pset csv location"
    psets = load_psets(_ENGINE.config.pset_file)
    assert any(p["id"] == 1 and p["cfreq"] == 11520 for p in psets)


@needs_ref
def test_render_returns_samples_and_packet():
    samples, fs, pkt = _ENGINE.render(cargo="unit render")
    assert fs == 48000
    assert samples.dtype == np.float32
    assert len(samples) > fs  # at least ~1s
    assert pkt.crc_ok is True
    assert pkt.class_id == 16


@needs_ref
def test_software_loopback_roundtrip():
    msg = "round trip 123"
    samples, fs, _ = _ENGINE.render(cargo=msg)
    sig = np.concatenate([np.zeros(fs // 2, "<f4"), samples, np.zeros(fs, "<f4")])
    dets = _ENGINE.decode_array(sig, fs)
    assert len(dets) == 1
    assert dets[0].packet.crc_ok
    assert dets[0].packet.payload == msg


@needs_ref
def test_verify_roundtrip():
    msg = "integrity check ✓ 123"
    samples, fs, _ = _ENGINE.render(cargo=msg, verify=True)
    sig = np.concatenate([np.zeros(fs // 2, "<f4"), samples, np.zeros(fs, "<f4")])
    dets = _ENGINE.decode_array(sig, fs, verify=True)
    assert dets and dets[0].packet.payload == msg
    assert dets[0].packet.payload_ok is True


@needs_ref
def test_decode_wav(tmp_path):
    import soundfile as sf

    msg = "via wav file"
    samples, fs, _ = _ENGINE.render(cargo=msg)
    path = tmp_path / "p.wav"
    sf.write(path, samples, fs)
    dets = _ENGINE.decode_wav(path)
    assert dets and dets[0].packet.payload == msg


@needs_ref
def test_realtime_receiver_streaming():
    from januspy.receiver import RealtimeReceiver

    got = []
    rx = RealtimeReceiver(_ENGINE, on_packet=got.append)
    rx.start()
    try:
        for msg in ("stream a", "stream b"):
            s, fs, _ = _ENGINE.render(cargo=msg)
            sig = np.concatenate([np.zeros(fs // 4, "<f4"), s, np.zeros(fs // 2, "<f4")])
            block = fs // 20
            import time
            for i in range(0, len(sig), block):
                rx.feed(sig[i : i + block])
                time.sleep(0.04)
            time.sleep(0.4)
        import time
        time.sleep(1.0)
    finally:
        rx.stop()
    payloads = [d.packet.payload for d in got if d.packet.crc_ok]
    assert "stream a" in payloads and "stream b" in payloads
