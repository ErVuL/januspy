"""Optional application-layer payload integrity for januspy.

JANUS validates only the 8-bit *header* CRC; the optional cargo has no checksum in the
standard (the FEC is its only protection). This module adds an opt-in CRC-32 over the
payload so corruption can be detected — at the cost of being non-standard (it only
interoperates with another januspy node that also has ``--verify`` enabled).

Scheme (text-safe so it survives the reference's NUL-terminated ``--packet-cargo`` and its
zero padding): the cargo is ``<payload-utf8-bytes><crc32 as 8 lowercase hex chars>``.
"""

from __future__ import annotations

import zlib

_CRC_HEX = 8  # CRC-32 rendered as 8 hex characters


def frame(text: str) -> str:
    """Append the payload CRC-32 (8 hex chars) to a message for transmission."""
    crc = zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF
    return text + format(crc, "08x")


def unframe(cargo_hex: str) -> tuple[str, bool] | None:
    """Verify and strip the CRC from received cargo bytes (given as the dump's hex).

    Returns ``(payload, ok)``; ``ok`` is False on a CRC mismatch *or* a suffix that isn't
    a valid CRC (both mean the payload was corrupted in transit). Returns ``None`` only
    when the cargo is too short to carry our 8-char suffix at all (e.g. an empty/truncated
    cargo), which the caller treats as "not validated".
    """
    raw = bytes.fromhex(cargo_hex.replace(" ", "")) if cargo_hex else b""
    raw = raw.rstrip(b"\x00")  # drop the reference's zero padding
    if len(raw) < _CRC_HEX:
        return None
    body, crc_ascii = raw[:-_CRC_HEX], raw[-_CRC_HEX:]
    try:
        want = int(crc_ascii.decode("ascii"), 16)
    except (ValueError, UnicodeDecodeError):
        # A non-hex suffix means the payload was corrupted: report it as a failure.
        return raw.decode("utf-8", "replace"), False
    ok = (zlib.crc32(body) & 0xFFFFFFFF) == want
    return body.decode("utf-8", "replace"), ok
