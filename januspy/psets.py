"""Read the JANUS parameter-set table (the reference's parameter_sets.csv)."""

from __future__ import annotations

from pathlib import Path


def load_psets(csv_path: str | Path) -> list[dict]:
    """Parse the parameter-set CSV into ``[{id, cfreq, bandwidth, name}, ...]``.

    Format (per the reference): ``Id, Center Frequency (Hz), Bandwidth (Hz), Name``,
    ``#``-comment lines ignored.
    """
    out: list[dict] = []
    for line in Path(csv_path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",", 3)]
        if len(parts) < 4:
            continue
        try:
            out.append({
                "id": int(parts[0]),
                "cfreq": int(parts[1]),
                "bandwidth": int(parts[2]),
                "name": parts[3],
            })
        except ValueError:
            continue
    return out
