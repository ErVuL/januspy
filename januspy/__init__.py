"""januspy — a practical real-time JANUS modem that wraps the CMRE reference.

The JANUS physical layer (encode/decode/detect) is performed entirely by the official
CMRE C reference implementation (the ``janus-tx`` / ``janus-rx`` programs built from
``januspy/reference/c``). januspy adds the parts needed for practical use: real-time
sound-card I/O, a streaming receiver, message composition, a live web UI, and a CLI.

Nothing in here reimplements the JANUS DSP — it orchestrates the reference binaries.
"""

from .engine import (
    CargoTooLargeError,
    Detection,
    JanusConfig,
    JanusEngine,
    MAX_DEFAULT_CARGO,
    Packet,
    RxState,
    find_reference,
    verify_payload,
)

__all__ = [
    "JanusEngine",
    "JanusConfig",
    "Packet",
    "RxState",
    "Detection",
    "find_reference",
    "verify_payload",
    "CargoTooLargeError",
    "MAX_DEFAULT_CARGO",
    "__version__",
]

__version__ = "0.1.0"
__author__ = "ErVuL"
__license__ = "GPL-3.0-only"
