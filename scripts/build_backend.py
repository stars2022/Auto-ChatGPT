#!/usr/bin/env python3
"""Build the dependency-free Python service as a native PyInstaller binary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = {"darwin": "darwin", "win32": "win32", "linux": "linux"}.get(sys.platform, sys.platform)
DIST = ROOT / "build" / "backend-dist" / PLATFORM
WORK = ROOT / "build" / "backend-work" / PLATFORM
SPEC = ROOT / "build" / "backend-spec" / PLATFORM


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    SPEC.mkdir(parents=True, exist_ok=True)
    executable = "autocodex-backend.exe" if PLATFORM == "win32" else "autocodex-backend"
    output = DIST / executable
    if output.exists():
        output.unlink()
    separator = os.pathsep
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "autocodex-backend",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
        "--specpath",
        str(SPEC),
        "--add-data",
        f"{ROOT / 'static'}{separator}static",
        "--collect-data",
        "certifi",
        str(ROOT / "app.py"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    if not output.exists():
        raise SystemExit(f"PyInstaller did not produce {output}")
    if PLATFORM != "win32":
        output.chmod(0o755)
    print(f"built {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
