# januspy — Documentation

A practical, real-time **JANUS** underwater-acoustic modem with a web UI, a CLI, and a
Python API. januspy wraps the official **CMRE JANUS reference implementation** — it does
**not** reimplement the JANUS physical layer — and adds everything needed for day-to-day
use: real-time sound-card I/O, a streaming receiver, a live spectrogram, message
composition, and a browser dashboard.

- [1. What JANUS is](#1-what-janus-is)
- [2. Architecture](#2-architecture)
- [3. Installation & offline operation](#3-installation--offline-operation)
- [4. The web UI](#4-the-web-ui)
- [5. Spectrogram settings](#5-spectrogram-settings)
- [6. Command-line interface](#6-command-line-interface)
- [7. Python API](#7-python-api)
- [8. The air interface](#8-the-air-interface)
- [9. Configuration & environment](#9-configuration--environment)
- [10. Troubleshooting](#10-troubleshooting)
- [11. Development](#11-development)

---

## 1. What JANUS is

JANUS (NATO **STANAG 4748**, developed by CMRE) is the open, standard signalling method for
underwater acoustic communications. It is a **frequency-hopped binary FSK (FH-BFSK)**
physical layer designed to be robust in the harsh underwater channel and to let dissimilar
nodes establish first contact — an underwater *lingua franca*. A baseline packet is 64 bits
(version, flags, class/application identifiers, application data, CRC) and can carry an
optional cargo payload.

januspy uses the reference for all JANUS DSP — encoding, the rate-1/2 K=9 convolutional
code, interleaving, frequency hopping, BFSK modulation, preamble detection, Doppler
estimation, and Viterbi decoding — so transmitted signals are standard-compliant and
interoperable with other JANUS implementations.

## 2. Architecture

```
                          ┌──────────────────────── januspy ────────────────────────┐
  microphone / line-in ──▶│ sounddevice capture ─┬─▶ RealtimeReceiver ──▶ janus-rx  │──▶ decoded
                          │                       │      (paced raw f32 → stdin)      │    packets
                          │                       └─▶ WaterfallAnalyzer (FFT)         │──▶ waterfall
   compose message  ─────▶│ JanusEngine.render ──────▶ janus-tx ──▶ float32 samples ──┼─▶ speaker
                          └───────────────────────────────────────────────────────────┘
                                   browser  ◀── WebSocket / HTTP ──▶ FastAPI server
```

Modules (`januspy/januspy/`):

| Module | Responsibility |
|---|---|
| `engine.py` | Locates the reference; runs `janus-tx`/`janus-rx`; parses the reference's stderr dump into `Packet` / `RxState`. |
| `receiver.py` | `RealtimeReceiver` — a long-lived `janus-rx` process fed paced float32 frames over stdin; emits a decoded packet per detection. |
| `audio.py` | `AudioEngine` (sounddevice capture/playback) and `WaterfallAnalyzer` (band-limited FFT spectrogram). |
| `server.py` | FastAPI app: HTTP endpoints + a WebSocket hub bridging thread-land callbacks to browser clients. |
| `web/` | Single-page UI (HTML/CSS/JS) with bundled offline fonts. |
| `cli.py` | `januspy` command-line entry point. |
| `psets.py` | Parses the reference `parameter_sets.csv`. |

**Why a subprocess wrapper?** The reference `janus-rx` reads raw float32 passband samples
from `/dev/stdin` and loops forever (it never sees EOF), so it decodes packets continuously.
Its packet + state **dump is printed to stderr** (`%-15s: %-45s: value`), which januspy
parses. `janus-tx` renders a packet to samples that januspy plays.

## 3. Installation & offline operation

The reference is a git **submodule** (`github.com/janus-uw/reference`) inside the project at
`reference`. Clone recursively and run the installer — **the install is the only
step that needs internet**:

```bash
git clone --recursive <repo> januspy && cd januspy
./install.sh
```

`install.sh` (idempotent):
1. checks build deps (`cmake`, `gcc`/`make`, `libfftw3`, `libsndfile`; warns on `libportaudio2`),
2. initialises the reference submodule if you forgot `--recursive`,
3. builds the C reference (Release) into `third_party/reference/c/local-install`,
4. creates `.venv` and `pip install -e .`,
5. runs a software-loopback smoke test.

**Runtime is fully offline.** After installation nothing reaches the network: the modem
uses the local reference binaries and local Python, the web server binds to `127.0.0.1`,
and the UI bundles its own fonts and assets (no CDNs, no web fonts, no telemetry).

Options: `./install.sh --skip-reference`, `--venv PATH`. To install just the package into
an active venv: `pip install -e .` (uses `pyproject.toml`).

## 4. The web UI

```bash
januspy serve                 # http://127.0.0.1:8000
januspy serve --host 0.0.0.0 --port 9000
```

Layout:
- **Receiver** — input device, parameter set, Doppler toggle, Start/Stop. While running it
  decodes every packet on the channel.
- **Transmit** — a multi-line message composer (⌘/Ctrl+Enter to send), output device,
  *Show own TX on waterfall* and *Digital loopback* toggles, and an Advanced panel for
  class id / app type.
- **Spectrogram** — live spectrogram settings (see below).
- **Channel waterfall** — the scrolling spectrogram (the hero panel), with a frequency axis.
- **Received messages** — a readable feed; each entry shows the payload prominently with
  CRC status, class, SNR, Doppler speed, γ, timing, and raw bytes. **Click any message** to
  open a detail view with the full payload (scrollable, copyable) and every field —
  convenient for long messages.
- **Console** — detector/status log line feed.

The browser talks to the server over a single WebSocket. Server → client messages:
`status`, `devices`, `psets`, `waterfall_config`, `packet`, `tx`, `waterfall`, `event`.
Client → server commands: `start_rx`, `stop_rx`, `tx`, `set_waterfall`, `devices`.

**Digital loopback** feeds your transmitted samples straight into the receiver (no acoustic
path), so you can verify the full decode chain without a speaker/mic — handy on headless
machines or for self-test.

## 5. Spectrogram settings

The waterfall is a sliding-window FFT magnitude display restricted to a frequency band.
All settings are adjustable live from the **Spectrogram** panel (they apply immediately
while RX is running and persist across start/stop):

| Setting | Meaning | Notes |
|---|---|---|
| **Min / Max freq (Hz)** | The band shown on the waterfall. | Snapped to the nearest FFT bin; clamped to `[0, fs/2]`. Default 8000–15000 Hz (around the 11.5 kHz JANUS band). |
| **FFT size** | Window length (512–4096). | Larger = finer frequency resolution, coarser time resolution. Clamped to a power of two. The bin count is shown next to the control. |
| **Noise floor (dB)** | Magnitude mapped to the bottom of the colour scale. | Raise it to suppress background; lower to reveal weak signals. |
| **Headroom (dB)** | Magnitude mapped to the top of the colour scale. | Together with the floor this sets contrast/gain. |
| **Palette** | Colour map: Phosphor, Ice, Amber, Mono. | Client-side only (instant). |

Under the hood these map to `WaterfallAnalyzer(fmin, fmax, nfft, floor_db, ceil_db)`; the
server returns the *effective* configuration (with frequencies snapped to the bin grid) as
a `waterfall_config` message, and the UI reflects it back into the controls and the axis.

The current config is also available over HTTP: `GET /api/waterfall`.

## 6. Command-line interface

```bash
januspy serve                          # run the web UI
januspy rx [--device N] [--no-doppler] [--verbose]   # real-time decode to the console
januspy tx "message" [--out FILE.wav] [--play] [--device N] [--class-id N] [--app-type N]
januspy decode FILE.wav                # decode a recording (any rate/subtype soundfile reads)
januspy loopback "message"             # render + decode in software (no audio hardware)
januspy devices                        # list audio input/output devices
```

Common options: `--pset N` (parameter-set id, default 1), `--fs HZ` (sampling rate, default
48000), and the global `--ref PATH` (reference install prefix). `tx` with neither `--out`
nor `--play` just prints the encoded packet.

## 7. Python API

```python
import numpy as np
from januspy import JanusEngine

eng = JanusEngine()                                   # finds the built reference

# Transmit (encode) — returns mono float32 passband samples + parsed packet
samples, fs, pkt = eng.render(cargo="Hello JANUS")
print(pkt.bytes_hex, pkt.crc_ok, pkt.class_id)

# Receive (decode) from an in-memory array or a WAV
dets = eng.decode_array(samples, fs)                  # list[Detection]
det = dets[0]
print(det.packet.payload, det.packet.crc_ok, det.state.speed, det.state.gamma)
dets = eng.decode_wav("recording.wav")
```

Continuous, real-time decoding:

```python
from januspy import JanusEngine
from januspy.receiver import RealtimeReceiver

rx = RealtimeReceiver(JanusEngine(), on_packet=lambda d: print(d.packet.payload))
rx.start()
rx.feed(frames_float32)      # call repeatedly with captured audio
# ... rx.stop()
```

Full sound-card modem with waterfall callbacks:

```python
from januspy.audio import AudioEngine
ae = AudioEngine(on_packet=print, on_waterfall=lambda row: ...)
ae.start_rx(input_device=None)               # opens the mic, decodes + analyses
ae.configure_waterfall(fmin=9000, fmax=13000, nfft=1024, floor_db=-90, ceil_db=-10)
ae.transmit(cargo="ping", loopback=True)     # render + play (+ optional self-decode)
ae.stop_rx()
```

Key types: `Packet` (`payload`, `crc_ok`, `class_id`, `app_type`, `bytes_hex`, `fields`,
`cargo_ascii`, …) and `RxState` (`after`, `gamma`, `speed`, `rssi`, `snr`).

## 8. The air interface

- **Parameter sets** come from the reference `parameter_sets.csv`. The default, **id 1**, is
  the initial JANUS band: **11520 Hz** centre, **4160 Hz** bandwidth, **160 Hz** chip rate,
  13 frequencies, 32-chip preamble.
- **Sampling**: TX/RX use a **48 kHz passband** stream by default (`--fs`).
- **Transmit is half-duplex** (acoustic). To monitor your own transmission visually enable
  *Show own TX on waterfall*; to decode it without an acoustic path enable *Digital loopback*.
- **Cargo**: messages are carried as packet cargo (class 16 / app type 0 by default, the
  NATO reference plugin). Other class/app combinations select different field codecs.
- **Message length**: the default class-16/app-0 plugin encodes the cargo size in a 6-bit
  index, so it supports payloads up to **480 bytes** (the JANUS absolute maximum is 4096
  bytes, reachable only with a class/app that encodes size differently). The reference
  *silently drops* cargo larger than its size field can represent, so januspy detects this
  and raises `CargoTooLargeError` rather than transmitting an empty packet. Note JANUS is
  slow (~80 bps): 480 bytes is already ~50 s of airtime, so long messages take minutes.

### Packet validation

JANUS validates the **8-bit header CRC** over the 8-byte baseline packet — this is
recomputed and checked on receive (the "header crc ok" badge / `CRC Validity`). It covers
the version, flags, class/app identifiers, app-data and cargo *size*. The **optional cargo
has no CRC in the standard**; its only protection is the convolutional FEC. On a marginal
link (low SNR, or sample-clock skew drifting the long cargo out of chip alignment) you can
therefore get a *valid header with a garbled payload*.

To detect that, januspy offers an **opt-in application-layer CRC-32** over the payload:
`--verify` (CLI) or the *Validate payload with CRC-32* toggle (UI). The transmitter appends
a CRC-32 (as a text suffix, so it survives the reference's NUL-terminated cargo argument and
zero padding) and the receiver recomputes and compares it, reporting `payload_ok`
(`payload ✓` / `payload ✗ corrupt`). This is **non-standard** — it only interoperates with
another januspy node that also has `--verify` enabled — so it is off by default, and it
costs 8 bytes of the payload budget.

## 9. Configuration & environment

| Variable | Effect |
|---|---|
| `JANUSPY_REF` | Path to the reference install prefix (overrides the bundled submodule). |
| `--pset`, `--fs` | Parameter-set id and sampling rate (CLI), or `JanusConfig` fields. |

The reference is located in this order: `$JANUSPY_REF` → `third_party/reference/c/local-install`
→ legacy repo-root `reference/` → `janus-tx` on `PATH`. The reference's cargo plugins
(`share/janus/plugins`) are exposed to the subprocesses via `JANUS_PLUGINS` /
`LD_LIBRARY_PATH` automatically.

## 10. Troubleshooting

- **`JANUS reference binaries not found`** — build the reference: `./install.sh`, or set
  `$JANUSPY_REF`.
- **No audio devices / `PortAudio unavailable`** — install `libportaudio2`. File decode,
  software loopback, and the web UI still work without it.
- **RX runs but nothing decodes** — check input levels and that the signal is in the
  parameter-set band; widen the waterfall band / lower the noise floor to confirm energy is
  present; ensure the sampling rate matches the transmitter.
- **Verify the chain without hardware** — `januspy loopback "test"` (CLI) or the UI's
  *Digital loopback* toggle.
- **Decoding a recording fails** — januspy reads WAVs via `soundfile` (any subtype/rate) and
  feeds the raw path; make sure the file actually contains a JANUS passband signal.

## 11. Development

```bash
.venv/bin/python -m pytest januspy        # test suite (skips if the reference isn't built)
```

The tests drive the real reference binaries and cover the dump parser, render, software
loopback round-trip, WAV decode, and the streaming receiver. The DSP itself is the
reference's responsibility; januspy's tests verify the wrapper renders, decodes, parses,
and round-trips faithfully. See `README.md` for a quick start and `../CLAUDE.md` for
workspace-level notes.

**Credits:** all JANUS encoding/decoding/detection is performed by the **JANUS reference
implementation** of NATO STO **CMRE** (© 2008–2018 STO CMRE, GPL-3.0), vendored as the
`reference` submodule from
[github.com/janus-uw/reference](https://github.com/janus-uw/reference). JANUS is
standardised as NATO STANAG 4748.

**License:** Copyright © 2026 ErVuL. GPL-3.0-only (see `LICENSE`), matching the CMRE
reference it wraps.
