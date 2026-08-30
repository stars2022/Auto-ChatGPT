#!/usr/bin/env python3
"""Generate the small, dependency-free Auto Codex icon set.

The PNG writer is intentionally stdlib-only so CI can regenerate Linux/Windows
assets without installing Pillow.  macOS ``iconutil`` is used when available
to assemble the PNGs into an .icns bundle.
"""

from __future__ import annotations

import struct
import subprocess
import tempfile
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png(size: int) -> bytes:
    # Opaque deep-blue tile with two crisp white diagonal strokes matching the
    # in-app brand mark. The square is deliberately left unrounded; macOS and
    # Windows apply their native icon mask when presenting it.
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            t = (x + y) / max(1, 2 * size - 2)
            r = int(19 + 28 * t)
            g = int(86 + 82 * t)
            b = int(180 + 48 * t)
            # Two diagonal 10%-wide strokes.
            stroke_a = abs((y - (0.68 * size - 0.4 * x)) / size) < 0.035 and 0.18 * size < x < 0.62 * size
            stroke_b = abs((y - (0.47 * size - 0.4 * x)) / size) < 0.035 and 0.52 * size < x < 0.84 * size
            if stroke_a or stroke_b:
                r, g, b = 242, 249, 255
            row.extend((r, g, b, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico(images: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = bytearray()
    offset = 6 + 16 * len(images)
    payload = bytearray()
    for size, data in images:
        dimension = 0 if size >= 256 else size
        entries.extend(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset))
        payload.extend(data)
        offset += len(data)
    return header + bytes(entries) + bytes(payload)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    sizes = [16, 32, 64, 128, 256, 512, 1024]
    images: list[tuple[int, bytes]] = []
    for size in sizes:
        data = png(size)
        (ASSETS / f"icon-{size}.png").write_bytes(data)
        images.append((size, data))
    (ASSETS / "icon.png").write_bytes(dict(images)[512])
    (ASSETS / "icon.ico").write_bytes(ico([(size, data) for size, data in images if size in (16, 32, 64, 128, 256)]))
    if subprocess.call(["sh", "-c", "command -v iconutil >/dev/null 2>&1"]) == 0:
        with tempfile.TemporaryDirectory(prefix="autocodex-iconset-") as temp:
            iconset = Path(temp) / "AutoCodex.iconset"
            iconset.mkdir()
            for logical, physical in ((16, 16), (32, 32), (128, 128), (256, 256), (512, 512)):
                (iconset / f"icon_{logical}x{logical}.png").write_bytes((ASSETS / f"icon-{physical}.png").read_bytes())
            for logical, physical in ((16, 32), (32, 64), (128, 256), (256, 512), (512, 1024)):
                (iconset / f"icon_{logical}x{logical}@2x.png").write_bytes((ASSETS / f"icon-{physical}.png").read_bytes())
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS / "icon.icns")], check=True)


if __name__ == "__main__":
    main()
