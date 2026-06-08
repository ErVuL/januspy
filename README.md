# januspy

[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Linux-FCC624?logo=linux&logoColor=black)](#requirements)
[![License: GPL v3](https://img.shields.io/badge/license-GPLv3-blue.svg)](LICENSE)

A practical, real-time **JANUS** underwater-acoustic modem with a web UI.

JANUS (NATO STANAG 4748) is an open FH-BFSK signalling standard for underwater comms.
januspy does **not** reimplement the JANUS physical layer — it wraps the official CMRE
reference implementation (the `janus-tx` / `janus-rx` programs) and adds everything needed
for practical use:

- **Real-time receive** — listens continuously on the sound card and decodes every packet.
- **Transmit** — compose a message, render it with the reference encoder, play it.
- **Live web UI** — waterfall spectrogram, decoded-message log, device + parameter-set pickers.
- **CLI** — `tx`, `rx`, `decode`, `loopback`, `devices`, `serve`.

The JANUS DSP (encoding, convolutional code, interleaving, frequency hopping, BFSK,
preamble detection, Doppler, Viterbi decoding) is performed entirely by the reference
binaries, so the on-air signal is standard-compliant and interoperable.

📖 **Full documentation:** [`DOCUMENTATION.md`](DOCUMENTATION.md) — architecture, web UI,
spectrogram settings, CLI, Python API, the air interface, configuration, and troubleshooting.

## How it works

```
            ┌─────────────┐   raw f32    ┌────────────────────┐
 mic  ──────▶ sounddevice ─────────────▶ │ janus-rx (stdin)   │── stderr dump ──▶ parsed
            │  capture    │   (paced)    │  reference decoder │                   packets
            └──────┬──────┘              └────────────────────┘
                   │ same frames
                   ▼
            ┌─────────────┐  FFT band     waterfall rows ──▶ web UI (WebSocket)
            │ waterfall   │──────────────────────────────────────────────────▶
            └─────────────┘

 compose ──▶ janus-tx (reference encoder) ──▶ float32 samples ──▶ sounddevice playback
```

`janus-rx` runs as a long-lived process reading raw float32 passband samples from its
stdin, so it never sees EOF and decodes packets continuously. Its packet/state dump (on
stderr) is parsed into `Packet` / `RxState` objects.

## Requirements

- **Linux** — the real-time receiver streams via `/dev/stdin` and live audio uses
  ALSA/PortAudio.
- The CMRE reference (vendored as a git submodule at `third_party/reference`, built by
  `install.sh` into `third_party/reference/c/local-install/bin/`). Set `$JANUSPY_REF` to override.
- A C toolchain to build the reference: `cmake`, `gcc`/`make`, `libfftw3`, `libsndfile`.
- Python ≥ 3.12, and for live audio: PortAudio (`libportaudio2`).

## Install

The reference is a git **submodule** (the official
[janus-uw/reference](https://github.com/janus-uw/reference)). Clone recursively, then run
the installer — this is the only step that needs internet:

```bash
git clone --recursive https://github.com/ErVuL/januspy.git januspy && cd januspy
./install.sh
.venv/bin/pip install -e .          # runtime only
# or: .venv/bin/pip install -e ".[dev]"   # + pytest, to run the test suite
```

`install.sh` checks system deps, fetches/builds the C reference, and creates `.venv`. It
**does not** install the januspy Python package — that is a separate step you run yourself
(the `pip install -e` above), so you can choose whether to include the `[dev]` test extras.
Re-running `./install.sh` after the package is installed also probes live audio and runs a
smoke test. **At runtime januspy is fully offline** — no network is used (local reference
binaries + local Python; the web UI bundles its own assets).

Already cloned without `--recursive`? `git submodule update --init --recursive` (or just
re-run `./install.sh`, which does it for you).

## Usage

```bash
# Web UI (open http://127.0.0.1:8000)
januspy serve

# Real-time decode from the default microphone
januspy rx

# Transmit a message: render to WAV and/or play it
januspy tx "Hello JANUS" --out packet.wav --play

# Decode a recording
januspy decode packet.wav

# Software round-trip (no audio hardware) — handy smoke test
januspy loopback "does it work?"

# List audio devices
januspy devices
```

### Python API

```python
import numpy as np
from januspy import JanusEngine

eng = JanusEngine()
samples, fs, pkt = eng.render(cargo="Hello JANUS")     # encode via janus-tx
dets = eng.decode_array(samples, fs)                   # decode via janus-rx
print(dets[0].packet.payload, dets[0].packet.crc_ok)
```

For continuous decoding use `januspy.receiver.RealtimeReceiver`, and for full sound-card
I/O + waterfall use `januspy.audio.AudioEngine`.

## Notes on the air interface

- Default parameter set is **id 1** — the initial JANUS band: 11520 Hz centre, 4160 Hz
  bandwidth, 160 Hz chip rate. Parameter sets come from the reference `parameter_sets.csv`.
- TX/RX use a 48 kHz passband stream by default.
- Transmit is half-duplex (acoustic). Use the UI's **digital loopback** toggle to decode
  your own transmission without an acoustic path.

## Credits

januspy wraps the **JANUS reference implementation** by NATO STO **CMRE** (Centre for
Maritime Research and Experimentation) — Copyright © 2008–2018 STO CMRE, licensed under
GPL-3.0. The reference source (v3.0.5) is vendored as the `third_party/reference` submodule
from [github.com/janus-uw/reference](https://github.com/janus-uw/reference); all JANUS
encoding/decoding/detection is performed by that code. See `third_party/reference` and its
`COPYING` for the original work.

JANUS is standardised as **NATO STANAG 4748**. More at <https://www.januswiki.org>.

## License

Copyright © 2026 **ErVuL**. januspy is free software, licensed under the **GNU General
Public License v3.0** — see [`LICENSE`](LICENSE). This matches the GPL-3.0 license of the
CMRE JANUS reference implementation it wraps.
