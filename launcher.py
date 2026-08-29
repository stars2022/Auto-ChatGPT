#!/usr/bin/env python3
"""Cross-platform background launcher for the local Auto Codex service."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("AUTOCODEX_DATA_DIR", Path.home() / ".autocodex")).expanduser()
PID_FILE = STATE_DIR / "autocodex.pid"
LOG_FILE = STATE_DIR / "autocodex.log"


def pid() -> int | None:
    try:
        value = int(PID_FILE.read_text().strip())
        os.kill(value, 0)
        return value
    except (OSError, ValueError):
        return None


def start() -> int:
    current = pid()
    if current:
        print(f"already running (pid {current})")
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = LOG_FILE.open("a", encoding="utf-8")
    env = {**os.environ, "AUTOCODEX_OPEN_BROWSER": "0"}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    child = subprocess.Popen([sys.executable, str(ROOT / "app.py")], cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, creationflags=creationflags)
    PID_FILE.write_text(str(child.pid), encoding="utf-8")
    print(f"started pid {child.pid}; http://127.0.0.1:{env.get('AUTOCODEX_PORT', '8765')}")
    return 0


def stop() -> int:
    current = pid()
    if not current:
        PID_FILE.unlink(missing_ok=True)
        print("not running")
        return 0
    try:
        os.kill(current, signal.SIGTERM)
    except OSError:
        pass
    PID_FILE.unlink(missing_ok=True)
    print(f"stopped pid {current}")
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "start"
    if command == "start": return start()
    if command == "stop": return stop()
    if command == "restart": stop(); time.sleep(0.2); return start()
    if command == "status":
        current = pid(); print(f"running (pid {current})" if current else "not running"); return 0
    print("usage: launcher.py [start|stop|restart|status]", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
