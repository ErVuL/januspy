"""januspy command-line interface.

Subcommands:
  serve      run the web UI
  tx         render a packet to WAV and/or play it
  rx         real-time decode from the microphone to the console
  decode     decode a WAV file
  loopback   render + decode in software (no audio hardware)
  devices    list audio devices
"""

from __future__ import annotations

import argparse
import sys
import time

from .engine import Detection, JanusEngine, find_reference


def _fmt(det: Detection) -> str:
    """Format a decoded detection as a one-line console summary."""
    p, s = det.packet, det.state
    crc = "OK " if p.crc_ok else "BAD"
    after = f"{s.after:.3f}s" if s.after is not None else "—"
    gamma = f"{s.gamma:.5f}" if s.gamma is not None else "—"
    speed = f"{s.speed:+.2f}m/s" if (s.speed is not None) else "—"
    vmark = "" if p.payload_ok is None else (" payload=VERIFIED" if p.payload_ok else " payload=CORRUPT")
    return (f"[{crc}] class {p.class_id} ({p.class_id_name}) app {p.app_type}  "
            f"payload={p.payload!r}{vmark}  t={after} γ={gamma} v={speed}  bytes={p.bytes_hex.replace(' ','')}")


def _engine(args) -> JanusEngine:
    """Build a JanusEngine, honouring the global --ref option."""
    return JanusEngine(find_reference(getattr(args, "ref", None)))


def cmd_devices(args) -> int:
    """List the available audio input and output devices."""
    from .audio import list_devices
    d = list_devices()
    if not d["inputs"] and not d["outputs"]:
        print("No audio devices (PortAudio unavailable).")
        return 0
    print("Inputs:")
    for x in d["inputs"]:
        star = " *" if x["index"] == d["default_input"] else "  "
        print(f" {star}{x['index']:>3}: {x['name']}  ({x['channels_in']} ch, {x['default_samplerate']} Hz)")
    print("Outputs:")
    for x in d["outputs"]:
        star = " *" if x["index"] == d["default_output"] else "  "
        print(f" {star}{x['index']:>3}: {x['name']}  ({x['channels_out']} ch, {x['default_samplerate']} Hz)")
    return 0


def cmd_tx(args) -> int:
    """Render a packet and optionally write a WAV and/or play it."""
    eng = _engine(args)
    samples, fs, pkt = eng.render(
        cargo=args.message, class_id=args.class_id, app_type=args.app_type,
        app_fields=args.app_fields, pset_id=args.pset, fs=args.fs, verify=args.verify)
    print(f"encoded: bytes={pkt.bytes_hex.replace(' ','')} class={pkt.class_id} "
          f"app={pkt.app_type} crc_ok={pkt.crc_ok} dur={len(samples)/fs:.2f}s")
    if args.out:
        import soundfile as sf
        sf.write(args.out, samples, fs)
        print(f"wrote {args.out} ({fs} Hz, {len(samples)} samples)")
    if args.play:
        try:
            import sounddevice as sd
            sd.play(samples, samplerate=fs, device=args.device, blocking=True)
            print("played.")
        except Exception as exc:
            print(f"playback failed: {exc}", file=sys.stderr)
            return 1
    if not args.out and not args.play:
        print("(nothing to do: pass --out FILE and/or --play)")
    return 0


def cmd_decode(args) -> int:
    """Decode JANUS packets from a WAV file."""
    eng = _engine(args)
    dets = eng.decode_wav(args.wav, pset_id=args.pset, verify=args.verify)
    if not dets:
        print("no packets decoded.")
        return 1
    for d in dets:
        print(_fmt(d))
    return 0


def cmd_loopback(args) -> int:
    """Render a packet and decode it in software (no audio hardware)."""
    import numpy as np
    eng = _engine(args)
    samples, fs, _ = eng.render(cargo=args.message, pset_id=args.pset, fs=args.fs, verify=args.verify)
    sig = np.concatenate([np.zeros(fs // 2, "<f4"), samples, np.zeros(fs, "<f4")])
    dets = eng.decode_array(sig, fs, pset_id=args.pset, verify=args.verify)
    if not dets:
        print("loopback FAILED: no packet decoded.")
        return 1
    for d in dets:
        print(_fmt(d))
    return 0


def cmd_rx(args) -> int:
    """Decode packets from the microphone in real time until interrupted."""
    from .audio import AudioEngine
    eng = _engine(args)
    print("Listening… (Ctrl-C to stop)")
    ae = AudioEngine(eng, on_packet=lambda d: print(_fmt(d)),
                     on_event=(print if args.verbose else (lambda e: None)))
    ae.set_verify(args.verify)
    try:
        ae.start_rx(input_device=args.device, fs=args.fs, pset_id=args.pset,
                    doppler=not args.no_doppler)
    except Exception as exc:
        print(f"failed to start receiver: {exc}", file=sys.stderr)
        return 1
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        ae.stop_rx()
    return 0


def cmd_serve(args) -> int:
    """Run the web UI server."""
    from .server import run
    eng = _engine(args)
    print(f"januspy web UI → http://{args.host}:{args.port}")
    print(f"reference: {eng.config.tx_bin}")
    run(host=args.host, port=args.port, engine=eng)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the januspy argument parser."""
    p = argparse.ArgumentParser(prog="januspy", description="Real-time JANUS modem (wraps the CMRE reference).")
    p.add_argument("--ref", help="path to the reference install prefix (else $JANUSPY_REF / default)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        """Add options shared by the tx/rx/decode/loopback subcommands."""
        sp.add_argument("--pset", type=int, default=None, help="parameter set id (default 1)")
        sp.add_argument("--fs", type=int, default=None, help="sampling rate Hz (default 48000)")
        sp.add_argument("--verify", action="store_true",
                        help="add/check an app-layer CRC-32 over the payload (non-standard; "
                             "both ends must use --verify)")

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("tx", help="render a packet to WAV and/or play it")
    s.add_argument("message", help="cargo message text")
    s.add_argument("--out", help="write a WAV file")
    s.add_argument("--play", action="store_true", help="play through the speaker")
    s.add_argument("--device", type=int, default=None, help="output device index")
    s.add_argument("--class-id", type=int, default=None, dest="class_id")
    s.add_argument("--app-type", type=int, default=None, dest="app_type")
    s.add_argument("--app-fields", default=None, dest="app_fields")
    add_common(s)
    s.set_defaults(func=cmd_tx)

    s = sub.add_parser("rx", help="real-time decode from the microphone")
    s.add_argument("--device", type=int, default=None, help="input device index")
    s.add_argument("--no-doppler", action="store_true")
    s.add_argument("--verbose", action="store_true", help="show detector events")
    add_common(s)
    s.set_defaults(func=cmd_rx)

    s = sub.add_parser("decode", help="decode a WAV file")
    s.add_argument("wav")
    add_common(s)
    s.set_defaults(func=cmd_decode)

    s = sub.add_parser("loopback", help="render + decode in software (no hardware)")
    s.add_argument("message")
    add_common(s)
    s.set_defaults(func=cmd_loopback)

    s = sub.add_parser("devices", help="list audio devices")
    s.set_defaults(func=cmd_devices)

    return p


def main(argv=None) -> int:
    """CLI entry point: parse arguments and dispatch to the chosen command."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
