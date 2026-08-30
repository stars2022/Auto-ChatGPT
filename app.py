#!/usr/bin/env python3
"""Auto Codex Companion - a dependency-free local helper for Codex Desktop.

The app deliberately reads only local metadata and never returns auth.json or
other credential contents. It can enqueue a continuation through the official
`codex queue` CLI when a schedule fires.
"""

from __future__ import annotations

import json
import os
import plistlib
import queue
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


APP_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
STATE_DIR = Path(os.environ.get("AUTOCODEX_DATA_DIR", Path.home() / ".autocodex")).expanduser()
STATE_FILE = STATE_DIR / "state.json"
HOST = os.environ.get("AUTOCODEX_HOST", "127.0.0.1")
PORT = int(os.environ.get("AUTOCODEX_PORT", "8765"))
POLL_SECONDS = max(5, int(os.environ.get("AUTOCODEX_POLL_SECONDS", "15")))
STATE_LOCK = threading.RLock()


def https_context() -> ssl.SSLContext:
    """Return a verified TLS context that also works inside PyInstaller bundles.

    Frozen Python builds do not reliably inherit Homebrew/system OpenSSL paths.
    Prefer certifi's bundled Mozilla roots when present, then fall back to the
    platform trust store. Never disable certificate verification.
    """
    cafile = os.environ.get("SSL_CERT_FILE")
    try:
        import certifi  # type: ignore
        cafile = cafile or certifi.where()
    except ImportError:
        pass
    candidates = [cafile, "/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/opt/homebrew/etc/openssl@3/cert.pem", "/opt/homebrew/etc/ca-certificates/cert.pem"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            try:
                return ssl.create_default_context(cafile=candidate)
            except OSError:
                continue
    return ssl.create_default_context()


HTTPS_CONTEXT = https_context()


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int | float | None) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def atomic_write_json(path: Path, value: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def db_rows(path: Path, query: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(query, args).fetchall()]
        conn.close()
        return rows
    except (sqlite3.Error, OSError):
        return []


def file_info(path: Path, label: str, sensitive: bool = False) -> dict[str, Any]:
    try:
        stat = path.stat()
        return {
            "label": label,
            "path": str(path),
            "exists": True,
            "sensitive": sensitive,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
        }
    except OSError:
        return {"label": label, "path": str(path), "exists": False, "sensitive": sensitive}


SENSITIVE_KEY = re.compile(r"token|secret|password|credential|cookie|auth|api[_-]?key|private", re.I)


def parse_config_summary(path: Path) -> list[dict[str, Any]]:
    """Parse TOML enough for a safe key inventory; values are redacted by default."""
    if not path.exists():
        return []
    section = "(root)"
    result: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            result.append({"section": section, "key": None, "value": None})
            continue
        if "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        full_key = f"{section}.{key}" if section != "(root)" else key
        sensitive = bool(SENSITIVE_KEY.search(full_key))
        if sensitive:
            safe_value = "••••••"
        elif raw.startswith(('"', "'")):
            safe_value = raw.strip('"\'')
            if len(safe_value) > 96:
                safe_value = safe_value[:93] + "…"
        else:
            safe_value = raw if len(raw) <= 96 else raw[:93] + "…"
        result.append({"section": section, "key": key, "value": safe_value, "sensitive": sensitive})
    return result


def codex_provider_base_url() -> str:
    """Best-effort extraction of the active custom provider base_url."""
    path = CODEX_HOME / "config.toml"
    if not path.exists():
        return ""
    section = ""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("["):
                section = s
            if section == "[model_providers.custom]" and s.startswith("base_url") and "=" in s:
                return s.split("=", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return ""


def codex_provider_api_key() -> str:
    """Read a custom provider bearer token for the third-party usage probe.

    The value is only used in-memory for the outbound request and is never
    returned by an API response or written to the event log.
    """
    path = CODEX_HOME / "config.toml"
    if not path.exists():
        return ""
    section = ""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("["):
                section = s
            if section == "[model_providers.custom]" and "=" in s:
                key, raw = s.split("=", 1)
                if key.strip() in {"experimental_bearer_token", "api_key", "bearer_token"}:
                    return raw.strip().strip('"\'')
    except OSError:
        pass
    return ""


def effective_usage_config(config: dict[str, Any] | None) -> dict[str, Any]:
    config = dict(config or {})
    # Third-party provider usage is allowed to auto-follow the same base URL and key
    # that Codex CLI uses. This never treats it as an official subscription.
    if not config.get("base_url") and codex_provider_base_url():
        config["base_url"] = codex_provider_base_url()
        config["auto_from_codex"] = True
    if not config.get("api_key"):
        token, kind = local_auth_token()
        if token and kind == "api_key":
            config["api_key"] = token
            config["auto_from_codex"] = True
    if not config.get("api_key") and codex_provider_api_key():
        config["api_key"] = codex_provider_api_key()
        config["auto_from_codex"] = True
    if config.get("auto_from_codex"):
        config["enabled"] = True
    if config.get("base_url") and config.get("api_key") and "enabled" not in config:
        config["enabled"] = True
    return config


def _saved_codex_cli_path() -> str:
    state = read_json(STATE_FILE, {})
    if isinstance(state, dict) and isinstance(state.get("settings"), dict):
        return str(state["settings"].get("codex_cli_path") or "").strip()
    return ""


def _expand_cli_candidate(value: str | None) -> list[Path]:
    if not value:
        return []
    candidate = Path(value).expanduser()
    if candidate.is_dir():
        names = ["codex.exe", "codex.cmd", "codex.bat", "codex"] if os.name == "nt" else ["codex"]
        return [candidate / name for name in names]
    return [candidate]


def codex_command_info(configured_path: str | None = None) -> dict[str, Any]:
    """Resolve Codex CLI with platform defaults and an optional file or directory."""
    manual = configured_path if configured_path is not None else (os.environ.get("CODEX_CLI_PATH") or _saved_codex_cli_path())
    candidates: list[tuple[str, Path]] = []
    candidates.extend(("manual", item) for item in _expand_cli_candidate(manual))
    discovered = shutil.which("codex")
    if discovered:
        candidates.append(("path", Path(discovered)))
    if sys.platform == "darwin":
        candidates.extend([
            ("chatgpt_bundle", Path("/Applications/ChatGPT.app/Contents/Resources/codex")),
            ("user_app_bundle", Path.home() / "Applications/ChatGPT.app/Contents/Resources/codex"),
            ("homebrew", Path("/opt/homebrew/bin/codex")),
            ("homebrew", Path("/usr/local/bin/codex")),
        ])
    elif os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        roaming = Path(os.environ.get("APPDATA", ""))
        candidates.extend([
            ("npm", roaming / "npm/codex.cmd"),
            ("chatgpt_bundle", local / "Programs/ChatGPT/resources/codex.exe"),
            ("program_files", Path(os.environ.get("PROGRAMFILES", "")) / "ChatGPT/resources/codex.exe"),
        ])
    else:
        candidates.extend([
            ("local", Path.home() / ".local/bin/codex"),
            ("system", Path("/usr/local/bin/codex")),
            ("system", Path("/usr/bin/codex")),
        ])
    seen: set[str] = set()
    for source, candidate in candidates:
        text = str(candidate)
        if not text or text in seen:
            continue
        seen.add(text)
        executable = candidate.exists() and (os.name == "nt" or os.access(candidate, os.X_OK))
        if executable:
            return {"found": True, "path": text, "source": source, "platform": sys.platform}
    return {"found": False, "path": None, "source": "not_found", "platform": sys.platform, "manual_path": manual or ""}


def codex_command(configured_path: str | None = None) -> str | None:
    return codex_command_info(configured_path).get("path")


def codex_version(configured_path: str | None = None) -> str | None:
    command = codex_command(configured_path)
    if not command:
        return None
    try:
        completed = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=3)
        value = (completed.stdout or completed.stderr).strip()
        return value or None
    except (OSError, subprocess.SubprocessError):
        return None


def _wait_json_response(lines: queue.Queue[dict[str, Any]], request_id: int, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = lines.get(timeout=max(0.05, deadline - time.monotonic()))
        except queue.Empty:
            return None
        if message.get("id") == request_id:
            return message
    return None


def codex_cli_status_probe(configured_path: str | None = None) -> dict[str, Any]:
    """Read the machine-readable rate-limit snapshot behind CLI `/status`.

    `/status` itself is a TUI slash command. The app-server request is the CLI's
    stable machine-readable equivalent and works without scraping terminal ANSI.
    """
    checked = iso(now_ms())
    info = codex_command_info(configured_path)
    command = info.get("path")
    if not command:
        return {"status": "cli_not_found", "checked_at": checked, "cli": info}
    process: subprocess.Popen[str] | None = None
    lines: queue.Queue[dict[str, Any]] = queue.Queue()
    try:
        process = subprocess.Popen(
            [str(command), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        def read_stdout() -> None:
            assert process is not None and process.stdout is not None
            for line in process.stdout:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        lines.put(value)
                except ValueError:
                    continue

        threading.Thread(target=read_stdout, name="codex-status-reader", daemon=True).start()
        assert process.stdin is not None

        def send(message: dict[str, Any]) -> None:
            assert process is not None and process.stdin is not None
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()

        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "auto-codex-companion", "title": "Auto Codex Companion", "version": "0.2.5"}, "capabilities": {}}})
        initialized = _wait_json_response(lines, 1, 6)
        if not initialized or initialized.get("error"):
            raise RuntimeError(str((initialized or {}).get("error") or "Codex app-server 初始化超时"))
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        response = _wait_json_response(lines, 2, 8)
        if not response:
            raise RuntimeError("Codex CLI 未返回额度状态")
        if response.get("error"):
            raise RuntimeError(str(response["error"]))
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        rate_limits = result.get("rateLimits") if isinstance(result.get("rateLimits"), dict) else {}
        windows = []
        for key in ("primary", "secondary"):
            window = rate_limits.get(key)
            if not isinstance(window, dict) or window.get("usedPercent") is None:
                continue
            minutes = window.get("windowDurationMins")
            if minutes == 300:
                name = "5_hour"
            elif minutes == 10_080:
                name = "7_day"
            elif isinstance(minutes, (int, float)) and minutes >= 1_440:
                name = f"{int(minutes // 1_440)}_day"
            else:
                name = f"{int((minutes or 0) // 60)}_hour"
            reset = window.get("resetsAt")
            windows.append({"name": name, "used_percent": window.get("usedPercent"), "reset_at": iso(float(reset) * 1000) if isinstance(reset, (int, float)) else reset})
        return {
            "status": "ok", "auth_kind": "cli", "credential_source": "codex_cli",
            "endpoint": "account/rateLimits/read", "checked_at": checked, "windows": windows,
            "plan_type": rate_limits.get("planType"), "credits": _safe_response_shape(rate_limits.get("credits") or {}),
            "reset_credits": _safe_response_shape(result.get("rateLimitResetCredits") or {}),
            "cli": {**info, "version": codex_version(configured_path)},
        }
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        return {"status": "cli_error", "checked_at": checked, "detail": str(exc)[:400], "cli": info}
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


def _keychain_auth() -> dict[str, Any] | None:
    """Read the Codex Auth Keychain item only for local use; never expose its value."""
    if sys.platform != "darwin":
        return None
    try:
        completed = subprocess.run(["security", "find-generic-password", "-s", "Codex Auth", "-w"], capture_output=True, text=True, timeout=3)
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        value = json.loads(completed.stdout.strip())
        return value if isinstance(value, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _auth_sources() -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    raw = read_json(CODEX_HOME / "auth.json", {})
    if isinstance(raw, dict):
        sources.append(raw)
    keychain = _keychain_auth()
    if keychain:
        sources.append(keychain)
    return sources


def local_auth_info() -> dict[str, Any]:
    """Return credential type metadata, never the credential itself."""
    present = (CODEX_HOME / "auth.json").exists()
    for index, raw in enumerate(_auth_sources()):
        tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw
        if raw.get("auth_mode") == "chatgpt" and isinstance(tokens, dict) and tokens.get("access_token"):
            return {"present": True, "kind": "oauth", "field": "tokens.access_token", "account_id_configured": bool(tokens.get("account_id")), "source": "auth.json" if index == 0 else "keychain"}
        if raw.get("OPENAI_API_KEY"):
            present = True
    if present:
        return {"present": True, "kind": "api_key", "field": "OPENAI_API_KEY"}
    if codex_provider_api_key():
        return {"present": True, "kind": "api_key", "field": "model_providers.custom.experimental_bearer_token", "source": "config.toml"}
    return {"present": False, "kind": "none"}


def local_auth_token() -> tuple[str | None, str]:
    for raw in _auth_sources():
        tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw
        if raw.get("auth_mode") == "chatgpt" and isinstance(tokens, dict) and tokens.get("access_token"):
            return str(tokens["access_token"]), "oauth"
        if raw.get("OPENAI_API_KEY"):
            return str(raw["OPENAI_API_KEY"]), "api_key"
    provider_key = codex_provider_api_key()
    if provider_key:
        return provider_key, "api_key"
    return None, "none"


def local_oauth_credentials() -> tuple[str | None, str | None, str | None]:
    for index, raw in enumerate(_auth_sources()):
        tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
        if raw.get("auth_mode") == "chatgpt" and tokens.get("access_token"):
            return str(tokens["access_token"]), str(tokens.get("account_id")) if tokens.get("account_id") else None, "auth.json" if index == 0 else "keychain"
    return None, None, None


def _safe_response_shape(value: Any, depth: int = 0) -> Any:
    """Keep useful usage fields while removing IDs, account data and credentials."""
    if depth > 4:
        return "<nested>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_l = str(key).lower()
            if SENSITIVE_KEY.search(key_l) or key_l.endswith("_id") or key_l in {"id", "email", "username"}:
                continue
            if isinstance(item, (dict, list)):
                out[str(key)] = _safe_response_shape(item, depth + 1)
            elif isinstance(item, (str, int, float, bool)) or item is None:
                out[str(key)] = item
        return out
    if isinstance(value, list):
        return [_safe_response_shape(item, depth + 1) for item in value[:20]]
    return value


def official_usage_probe() -> dict[str, Any]:
    """Probe the first-party usage endpoint with the locally configured CLI credential.

    OAuth sessions use ChatGPT's Codex endpoint. API-key sessions are deliberately
    rejected here because an API key is a third-party/API billing credential, not a
    ChatGPT subscription credential.
    """
    token, account_id, source = local_oauth_credentials()
    if not token:
        auth = local_auth_info()
        return {"status": "oauth_not_configured", "auth_kind": auth.get("kind"), "checked_at": iso(now_ms()), "detail": "当前 auth.json 是 API key 模式；官方订阅额度只接受 auth_mode=chatgpt 的 OAuth。"}
    url = "https://chatgpt.com/backend-api/wham/usage"
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": "codex-cli"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    request = urllib.request.Request(url, headers=headers, method="GET")
    checked = iso(now_ms())
    try:
        with urllib.request.urlopen(request, timeout=15, context=HTTPS_CONTEXT) as response:
            raw = response.read(4_000_000)
            data = json.loads(raw.decode("utf-8"))
            # Project the two official rate-limit windows into a compact, safe shape.
            windows = []
            rate_limit = data.get("rate_limit") if isinstance(data, dict) else None
            if isinstance(rate_limit, dict):
                for window in (rate_limit.get("primary_window"), rate_limit.get("secondary_window")):
                    if isinstance(window, dict) and window.get("used_percent") is not None:
                        seconds = window.get("limit_window_seconds")
                        if seconds == 18_000: tier = "5_hour"
                        elif seconds == 604_800: tier = "7_day"
                        elif seconds == 2_592_000: tier = "30_day"
                        elif isinstance(seconds, (int, float)) and seconds >= 86_400: tier = f"{int(seconds // 86_400)}_day"
                        elif isinstance(seconds, (int, float)): tier = f"{int(seconds // 3_600)}_hour"
                        else: tier = "unknown"
                        reset = window.get("reset_at")
                        windows.append({"name": tier, "used_percent": window.get("used_percent"), "reset_at": iso(float(reset) * 1000) if isinstance(reset, (int, float)) else reset})
            return {"status": "ok", "auth_kind": "oauth", "credential_source": source, "endpoint": url, "http_status": response.status, "checked_at": checked, "windows": windows, "data": _safe_response_shape(data)}
    except urllib.error.HTTPError as exc:
        # Preserve status and a short server message; never echo Authorization headers.
        body = exc.read(1200).decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
            detail: Any = _safe_response_shape(parsed)
        except ValueError:
            detail = body[:400]
        return {"status": "http_error", "auth_kind": "oauth", "credential_source": source, "endpoint": url, "http_status": exc.code, "checked_at": checked, "detail": detail}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError) as exc:
        return {"status": "error", "auth_kind": "oauth", "credential_source": source, "endpoint": url, "checked_at": checked, "detail": str(getattr(exc, "reason", None) or exc)[:400]}


def current_official_usage_probe(configured_path: str | None = None) -> dict[str, Any]:
    """Prefer the Codex CLI status callback, then fall back to local OAuth."""
    cli_result = codex_cli_status_probe(configured_path)
    if cli_result.get("status") == "ok":
        return cli_result
    oauth_result = official_usage_probe()
    oauth_result["cli_fallback"] = {
        "status": cli_result.get("status"),
        "detail": cli_result.get("detail"),
        "cli": cli_result.get("cli"),
    }
    return oauth_result


def read_app_state() -> dict[str, Any]:
    with STATE_LOCK:
        value = read_json(STATE_FILE, {})
    if not isinstance(value, dict):
        return default_app_state()
    defaults = default_app_state()
    for key, default in defaults.items():
        if key not in value:
            value[key] = default
        elif isinstance(default, dict) and isinstance(value.get(key), dict):
            for nested_key, nested_default in default.items():
                value[key].setdefault(nested_key, nested_default)
    return value


def write_app_state(value: dict[str, Any]) -> None:
    with STATE_LOCK:
        atomic_write_json(STATE_FILE, value)


def default_app_state() -> dict[str, Any]:
    return {
        "schedules": [], "last_goal_statuses": {}, "events": [], "last_scan_at": None,
        "monitor": {
            "scheduler_started_at": None, "last_tick_at": None, "tick_count": 0,
            "status_checked_at": None, "status_check_count": 0,
            "official_checked_at": None, "official_next_check_at": None,
            "official_check_count": 0, "official_last_status": "not_checked",
        },
        "usage_config": {"enabled": False, "base_url": "", "path": "/v1/usage", "unit": "USD", "poll_minutes": 5},
        "usage_probe": {"status": "not_configured"},
        "official_usage": {"status": "not_checked"},
        "settings": {
            "poll_seconds": POLL_SECONDS,
            "official_poll_minutes": 5,
            "codex_cli_path": "",
            "notifications": True,
            # Closing the window normally keeps the local scheduler alive and
            # hides the UI in the tray/menu bar. Users can opt into a full
            # application quit from Settings when they prefer conventional
            # desktop-window behavior.
            "close_behavior": "tray",
            # 0 means retry forever (with exponential backoff).
            "default_network_retries": 0,
            "default_backoff_seconds": 30,
        },
    }


def get_goal_rows() -> list[dict[str, Any]]:
    goal_path = CODEX_HOME / "goals_1.sqlite"
    rows = db_rows(
        goal_path,
        """SELECT thread_id, goal_id, objective, status, token_budget,
                  tokens_used, time_used_seconds, created_at_ms, updated_at_ms
           FROM thread_goals ORDER BY updated_at_ms DESC LIMIT 100""",
    )
    # Goals and thread metadata live in separate SQLite databases. SQLite
    # cannot join them unless one file is explicitly ATTACHed, so merge the
    # two read-only result sets in Python instead of silently losing goals.
    thread_rows = db_rows(
        CODEX_HOME / "state_5.sqlite",
        """SELECT id, title, cwd, model, reasoning_effort, archived,
                  updated_at_ms AS thread_updated_at_ms FROM threads""",
    )
    thread_by_id = {str(row.get("id")): row for row in thread_rows}
    for row in rows:
        row.update(thread_by_id.get(str(row.get("thread_id")), {}))
        row["updated_at"] = iso(row.get("updated_at_ms"))
        row["thread_updated_at"] = iso(row.get("thread_updated_at_ms"))
        row.pop("updated_at_ms", None)
        row.pop("thread_updated_at_ms", None)
    return rows


def get_thread_rows() -> list[dict[str, Any]]:
    """Return user tasks, nesting Codex-spawned workers under their parent."""
    path = CODEX_HOME / "state_5.sqlite"
    rows = db_rows(
        path,
        """SELECT id, title, cwd, model, reasoning_effort, updated_at_ms,
                  archived, tokens_used, source, git_branch, agent_nickname,
                  agent_role, agent_path
           FROM threads ORDER BY updated_at_ms DESC LIMIT 100""",
    )
    parent_by_child = {
        str(edge.get("child_thread_id")): str(edge.get("parent_thread_id"))
        for edge in db_rows(path, "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges")
        if edge.get("child_thread_id") and edge.get("parent_thread_id")
    }
    goal_by_thread = {r["thread_id"]: r for r in get_goal_rows()}
    for row in rows:
        goal = goal_by_thread.get(row["id"], {})
        row["goal_status"] = goal.get("status")
        row["objective"] = goal.get("objective")
        row["token_budget"] = goal.get("token_budget")
        row["time_used_seconds"] = goal.get("time_used_seconds")
        row["updated_at"] = iso(row.pop("updated_at_ms", None))
        parent_id = parent_by_child.get(str(row["id"]))
        source = row.get("source")
        is_subagent_source = False
        if isinstance(source, str) and source.startswith("{"):
            try:
                subagent_source = json.loads(source).get("subagent") or {}
                is_subagent_source = bool(subagent_source)
                spawn = subagent_source.get("thread_spawn") or {}
            except (TypeError, ValueError):
                spawn = {}
            parent_id = parent_id or spawn.get("parent_thread_id")
            row["agent_nickname"] = row.get("agent_nickname") or spawn.get("agent_nickname")
            row["agent_role"] = row.get("agent_role") or spawn.get("agent_role")
            row["agent_path"] = row.get("agent_path") or spawn.get("agent_path")
            row["agent_depth"] = spawn.get("depth")
        row["is_subagent"] = bool(parent_id or is_subagent_source)
        row["parent_thread_id"] = parent_id
        row["subagents"] = []

    by_id = {str(row["id"]): row for row in rows}
    top_level: list[dict[str, Any]] = []
    for row in rows:
        parent = by_id.get(str(row.get("parent_thread_id") or ""))
        if row.get("is_subagent"):
            if parent is not None:
                parent["subagents"].append(row)
            continue
        top_level.append(row)
    for row in rows:
        row["subagents"].sort(key=lambda item: parse_when(str(item.get("updated_at") or "")) or 0, reverse=True)
        row["subagent_count"] = len(row["subagents"])
    return top_level


def get_project_rows(threads: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Group recent threads by working directory for a readable task view."""
    groups: dict[str, dict[str, Any]] = {}
    for thread in threads if threads is not None else get_thread_rows():
        cwd = str(thread.get("cwd") or "未指定工作区")
        group = groups.setdefault(cwd, {"id": cwd, "name": Path(cwd).name or cwd, "cwd": cwd, "threads": [], "tokens_used": 0, "active": 0, "limited": 0})
        group["threads"].append(thread)
        group["tokens_used"] += max(0, int(thread.get("tokens_used") or 0)) + sum(
            max(0, int(agent.get("tokens_used") or 0)) for agent in thread.get("subagents", [])
        )
        if thread.get("goal_status") == "active":
            group["active"] += 1
        if thread.get("goal_status") == "usage_limited":
            group["limited"] += 1
    projects = list(groups.values())
    projects.sort(key=lambda item: max((parse_when(str(t.get("updated_at") or "")) or 0) for t in item["threads"]), reverse=True)
    for project in projects:
        project["thread_count"] = len(project["threads"])
    return projects


def inventory() -> dict[str, Any]:
    files = [
        file_info(CODEX_HOME / "config.toml", "Codex 主配置"),
        file_info(CODEX_HOME / "auth.json", "登录凭据（仅检测存在性）", True),
        file_info(CODEX_HOME / ".codex-global-state.json", "全局状态"),
        file_info(CODEX_HOME / "state_5.sqlite", "线程数据库"),
        file_info(CODEX_HOME / "goals_1.sqlite", "目标/配额状态数据库"),
        file_info(CODEX_HOME / "queue_1.sqlite", "队列数据库"),
        file_info(CODEX_HOME / "sqlite/codex-dev.db", "桌面自动化数据库"),
        file_info(CODEX_HOME / "process_manager/chat_processes.json", "进程管理快照"),
        file_info(CODEX_HOME / "session_index.jsonl", "会话索引"),
        file_info(Path.home() / "Library/Preferences/com.openai.codex.plist", "ChatGPT/Codex 偏好"),
        file_info(Path.home() / "Library/Preferences/com.openai.chat.plist", "ChatGPT 偏好"),
    ]
    config = parse_config_summary(CODEX_HOME / "config.toml")
    cli = codex_command_info()
    cli["version"] = codex_version()
    return {
        "codex_home": str(CODEX_HOME),
        "files": files,
        "config": config,
        "codex_cli": cli.get("path"),
        "codex_version": cli.get("version"),
        "codex_cli_info": cli,
        "auth": local_auth_info(),
        "chatgpt_app_running": any("ChatGPT.app/Contents/MacOS/ChatGPT" in line for line in _process_lines()),
        "limitations": [
            "优先通过 Codex CLI app-server 的 account/rateLimits/read 读取 `/status` 对应额度；失败时回退到本机 OAuth 探针。",
            "auth.json 只报告存在性和文件元数据，不读取或展示内容。",
            "继续任务通过 codex queue 写入线程队列；不会模拟点击或修改 ChatGPT 数据库。",
            "官方额度探针仅在 auth_mode=chatgpt 的 OAuth 下请求 ChatGPT wham/usage；API key 模式不会冒充官方订阅。",
        ],
    }


def _process_lines() -> list[str]:
    try:
        return subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True, timeout=2).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def quota_snapshot() -> dict[str, Any]:
    rows = get_goal_rows()
    counts: dict[str, int] = {}
    for row in rows:
        status = row.get("status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return {
        "counts": counts,
        "usage_limited": counts.get("usage_limited", 0),
        "tracked_goals": len(rows),
        "checked_at": iso(now_ms()),
        "source": str(CODEX_HOME / "goals_1.sqlite"),
    }


def token_snapshot() -> dict[str, Any]:
    """Aggregate token counters exposed by the local thread database."""
    rows = get_thread_rows()
    total = sum(max(0, int(row.get("tokens_used") or 0)) for row in rows)
    active = sum(max(0, int(row.get("tokens_used") or 0)) for row in rows if row.get("goal_status") == "active")
    limited = sum(max(0, int(row.get("tokens_used") or 0)) for row in rows if row.get("goal_status") == "usage_limited")
    return {
        "total_tokens": total,
        "active_tokens": active,
        "limited_tokens": limited,
        "tracked_threads": len(rows),
        "checked_at": iso(now_ms()),
        "source": str(CODEX_HOME / "state_5.sqlite"),
    }


def safe_usage_config(config: dict[str, Any] | None) -> dict[str, Any]:
    config = effective_usage_config(config)
    return {
        "enabled": bool(config.get("enabled")),
        "base_url": str(config.get("base_url") or ""),
        "path": str(config.get("path") or "/v1/usage"),
        "unit": str(config.get("unit") or "USD"),
        "poll_minutes": max(1, int(config.get("poll_minutes") or 5)),
        "api_key_configured": bool(config.get("api_key")),
        "auto_from_codex": bool(config.get("auto_from_codex")),
    }


def usage_probe(app: dict[str, Any], force: bool = False) -> dict[str, Any]:
    """Call the user-supplied usage endpoint, with the extractor semantics shown by the user.

    We intentionally do not execute arbitrary JavaScript. The equivalent field fallback is
    deterministic and the key is never returned to the browser or written to logs.
    """
    config = effective_usage_config(app.get("usage_config"))
    if not config.get("enabled") or not config.get("base_url") or not config.get("api_key"):
        return {"status": "not_configured", "checked_at": iso(now_ms())}
    previous = app.get("usage_probe") or {}
    last_ms = parse_when(str(previous.get("checked_at") or "")) if previous.get("checked_at") else None
    poll_ms = max(1, int(config.get("poll_minutes") or 5)) * 60_000
    if not force and last_ms and now_ms() - last_ms < poll_ms:
        return previous
    base = str(config.get("base_url")).rstrip("/")
    path = str(config.get("path") or "/v1/usage")
    url = base + (path if path.startswith("/") else "/" + path)
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + str(config.get("api_key")), "Accept": "application/json"}, method="GET")
    checked = iso(now_ms())
    try:
        with urllib.request.urlopen(request, timeout=10, context=HTTPS_CONTEXT) as response:
            raw = response.read(2_000_000)
            parsed = json.loads(raw.decode("utf-8"))
        remaining = parsed.get("remaining")
        if remaining is None and isinstance(parsed.get("quota"), dict):
            remaining = parsed["quota"].get("remaining")
        if remaining is None:
            remaining = parsed.get("balance")
        unit = parsed.get("unit")
        if unit is None and isinstance(parsed.get("quota"), dict):
            unit = parsed["quota"].get("unit")
        unit = unit or config.get("unit") or "USD"
        is_active = parsed.get("is_active")
        if is_active is None:
            is_active = parsed.get("isValid", True)
        numeric = None
        try:
            numeric = float(remaining) if remaining is not None else None
        except (TypeError, ValueError):
            pass
        result = {"status": "ok", "is_active": bool(is_active), "remaining": remaining, "remaining_numeric": numeric, "unit": unit, "checked_at": checked, "http_status": 200, "transport": "direct_http"}
        if previous.get("status") == "ok":
            result["last_good"] = {k: previous.get(k) for k in ("remaining", "remaining_numeric", "unit", "is_active", "checked_at")}
        elif previous.get("last_good"):
            result["last_good"] = previous["last_good"]
        return result
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError) as exc:
        detail = getattr(exc, "reason", None) or str(exc)
        last_good = previous.get("last_good") or ({k: previous.get(k) for k in ("remaining", "remaining_numeric", "unit", "is_active", "checked_at")} if previous.get("status") == "ok" else None)
        return {"status": "error", "detail": str(detail)[:240], "checked_at": checked, "last_good": last_good, "transport": "direct_http"}


def notification(title: str, message: str) -> None:
    # Notifications are best-effort and never affect scheduling.
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["osascript", "-e", f'display notification {json.dumps(message)} with title {json.dumps(title)}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def enqueue(thread_id: str, message: str) -> tuple[bool, str]:
    command = codex_command()
    if not command:
        return False, "未找到 codex CLI"
    if not thread_id or not message.strip():
        return False, "thread_id 和 message 不能为空"
    try:
        completed = subprocess.run(
            [command, "queue", "--thread", thread_id, "--message", message],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if completed.returncode == 0:
            return True, (completed.stdout or "已加入队列").strip()[:500]
        return False, (completed.stderr or completed.stdout or "codex queue 失败").strip()[:500]
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def restore_goal(thread_id: str, message: str = "", execution_mode: str = "auto") -> tuple[bool, str, str]:
    """Restore a thread and continue it using native goal control when possible.

    The CLI owns the session database, so restoration is deliberately performed
    through ``codex unarchive`` instead of editing SQLite directly. Goal-backed
    sessions receive the official ``/goal resume`` command; ordinary sessions
    receive the configured continuation message.
    """
    thread_id = str(thread_id or "").strip()
    message = str(message or "").strip()
    execution_mode = str(execution_mode or "auto").strip().lower()
    if execution_mode not in {"auto", "goal", "message"}:
        return False, "execution_mode 必须是 auto、goal 或 message", execution_mode
    if not thread_id:
        return False, "thread_id 不能为空", execution_mode
    row = next((item for item in get_thread_rows() if item.get("id") == thread_id), None)
    if row is None:
        return False, "未找到这个线程，可能已不在最近线程列表中", execution_mode
    if row.get("archived"):
        command = codex_command()
        if not command:
            return False, "未找到 codex CLI", execution_mode
        try:
            completed = subprocess.run(
                [command, "unarchive", thread_id],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if completed.returncode != 0:
                return False, (completed.stderr or completed.stdout or "恢复线程失败").strip()[:500], execution_mode
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc), execution_mode
    goal = next((item for item in get_goal_rows() if item.get("thread_id") == thread_id), None)
    resolved_mode = "goal" if execution_mode == "goal" or (execution_mode == "auto" and goal and goal.get("status") != "complete") else "message"
    if resolved_mode == "goal":
        if goal is None:
            return False, "这个会话没有可恢复的目标", resolved_mode
        ok, detail = enqueue(thread_id, "/goal resume")
        return ok, detail, resolved_mode
    if not message:
        return False, "普通会话需要填写继续消息", resolved_mode
    ok, detail = enqueue(thread_id, message)
    return ok, detail, resolved_mode


def classify_enqueue_error(detail: str) -> str:
    text = (detail or "").lower()
    if any(token in text for token in ("quota", "usage limit", "rate limit", "rate_limit", "insufficient", "exhausted", "429")):
        return "quota"
    if any(token in text for token in ("timeout", "timed out", "network", "connection", "temporar", "503", "502", "504", "dns")):
        return "network"
    if any(token in text for token in ("unauthorized", "forbidden", "401", "403", "auth", "login")):
        return "auth"
    return "other"


def budget_guard(schedule: dict[str, Any]) -> tuple[bool, str | None]:
    """Enforce per-schedule token and estimated-price ceilings before queueing."""
    thread_id = str(schedule.get("thread_id") or "")
    row = next((r for r in get_thread_rows() if r.get("id") == thread_id), None)
    if row is None:
        return True, None
    used_tokens = int(row.get("tokens_used") or 0)
    token_budget = schedule.get("token_budget")
    if token_budget not in (None, ""):
        try:
            if used_tokens >= int(token_budget):
                return False, f"token 预算已用尽（{used_tokens:,} / {int(token_budget):,}）"
        except (TypeError, ValueError):
            pass
    price_budget = schedule.get("price_budget_usd")
    price_per_1k = schedule.get("price_per_1k_tokens")
    if price_budget not in (None, "") and price_per_1k not in (None, ""):
        try:
            estimated = used_tokens / 1000 * float(price_per_1k)
            if estimated >= float(price_budget):
                return False, f"估算价格预算已用尽（${estimated:.4f} / ${float(price_budget):.4f}）"
        except (TypeError, ValueError):
            pass
    return True, None


def parse_when(value: str) -> int | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def schedule_due(schedule: dict[str, Any], current_ms: int, recovered_threads: set[str]) -> bool:
    if not schedule.get("enabled", True):
        return False
    recovery_targets = {str(schedule.get("thread_id") or ""), "__official__", "__usage_probe__"}
    matching_recovery = bool(recovered_threads & recovery_targets)
    next_attempt = int(schedule.get("next_attempt_at") or 0)
    if schedule.get("waiting_for_quota"):
        return matching_recovery
    if schedule.get("retry_pending"):
        return bool(next_attempt and current_ms >= next_attempt)
    kind = schedule.get("kind")
    if kind == "quota_recovered":
        return matching_recovery
    if kind == "interval":
        interval = max(1, int(schedule.get("interval_minutes", 60))) * 60_000
        baseline = int(schedule.get("last_check_at") or schedule.get("created_at") or current_ms)
        return current_ms - baseline >= interval
    if kind == "at_time":
        at = parse_when(str(schedule.get("run_at") or ""))
        return bool(at is not None and current_ms >= at and not schedule.get("last_run_at"))
    return False


class Scheduler:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.thread = threading.Thread(target=self.run, name="autocodex-scheduler", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # pragma: no cover - safety net for daemon thread
                app = read_app_state() or default_app_state()
                app.setdefault("events", []).append({"at": iso(now_ms()), "kind": "error", "message": str(exc)})
                app["events"] = app["events"][-100:]
                write_app_state(app)
            settings = read_app_state().get("settings") or {}
            wait_seconds = max(5, int(settings.get("poll_seconds") or POLL_SECONDS))
            self.stop_event.wait(wait_seconds)

    def tick(self) -> None:
        # Keep a scheduler tick atomic with API writes so a stale request
        # snapshot cannot erase a completed quota/status check.
        with STATE_LOCK, self.lock:
            app = read_app_state() or default_app_state()
            settings = app.get("settings") or {}
            current_ms = now_ms()
            monitor = app.setdefault("monitor", {})
            if not monitor.get("scheduler_started_at"):
                monitor["scheduler_started_at"] = iso(current_ms)
            monitor["last_tick_at"] = iso(current_ms)
            monitor["tick_count"] = int(monitor.get("tick_count") or 0) + 1
            before = app.get("last_goal_statuses") or {}
            current_rows = get_goal_rows()
            monitor["status_checked_at"] = iso(current_ms)
            monitor["status_check_count"] = int(monitor.get("status_check_count") or 0) + 1
            current = {r["thread_id"]: r.get("status") for r in current_rows}
            recovered = {tid for tid, old in before.items() if old == "usage_limited" and current.get(tid) not in (None, "usage_limited")}
            old_probe = app.get("usage_probe") or {}
            new_probe = usage_probe(app)
            app["usage_probe"] = new_probe
            old_exhausted = old_probe.get("status") == "ok" and (old_probe.get("is_active") is False or (old_probe.get("remaining_numeric") is not None and old_probe.get("remaining_numeric") <= 0))
            now_available = new_probe.get("status") == "ok" and new_probe.get("is_active") is True and (new_probe.get("remaining_numeric") is None or new_probe.get("remaining_numeric") > 0)
            if old_exhausted and now_available:
                app.setdefault("events", []).append({"at": iso(now_ms()), "kind": "usage_recovered", "message": "额度探针报告额度已恢复"})
                recovered.add("__usage_probe__")
            # Official ChatGPT subscription quota is OAuth-only and is polled at a
            # conservative five-minute cadence. The endpoint is internal to the
            # desktop product; failures are retained as status, never as secrets.
            old_official = app.get("official_usage") or {}
            official_checked = parse_when(str(old_official.get("checked_at") or "")) if old_official.get("checked_at") else None
            official_poll_ms = max(1, int(settings.get("official_poll_minutes") or 5)) * 60_000
            if official_checked is None or current_ms - official_checked >= official_poll_ms:
                new_official = current_official_usage_probe()
                if new_official.get("status") != "ok" and old_official.get("status") == "ok":
                    new_official["last_good"] = old_official
                app["official_usage"] = new_official
                old_windows = old_official.get("windows") or []
                new_windows = new_official.get("windows") or []
                old_full = bool(old_windows) and all(float(w.get("used_percent", 0)) >= 100 for w in old_windows)
                now_open = bool(new_windows) and any(float(w.get("used_percent", 100)) < 100 for w in new_windows)
                if old_full and now_open:
                    app.setdefault("events", []).append({"at": iso(now_ms()), "kind": "official_usage_recovered", "message": "官方订阅额度窗口已恢复"})
                    recovered.add("__official__")
                checked_ms = parse_when(str(new_official.get("checked_at") or "")) or current_ms
                monitor["official_checked_at"] = iso(checked_ms)
                monitor["official_check_count"] = int(monitor.get("official_check_count") or 0) + 1
                monitor["official_last_status"] = str(new_official.get("status") or "unknown")
                app.setdefault("events", []).append({
                    "at": iso(checked_ms), "kind": "automatic_usage_check", "message": "后台自动检查官方额度",
                    "ok": new_official.get("status") == "ok", "status": new_official.get("status"),
                    "source": new_official.get("credential_source"),
                })
                official_checked = checked_ms
            monitor["official_next_check_at"] = iso((official_checked or current_ms) + official_poll_ms)
            app["last_goal_statuses"] = current
            app["last_scan_at"] = current_ms
            if recovered:
                app.setdefault("events", []).append({
                    "at": iso(now_ms()), "kind": "quota_recovered", "threads": sorted(recovered),
                    "message": f"检测到 {len(recovered)} 个线程离开 usage_limited 状态",
                })
            for schedule in app.get("schedules", []):
                if schedule.get("kind") == "interval" and current.get(str(schedule.get("thread_id") or "")) == "active":
                    # Re-arm the watchdog only after the queued continuation is
                    # observed running. This prevents a slow status update from
                    # causing the same stopped target to be queued every interval.
                    schedule["awaiting_active"] = False
                if schedule.get("waiting_for_quota") and recovered & {str(schedule.get("thread_id") or ""), "__official__", "__usage_probe__"}:
                    schedule["waiting_for_quota"] = False
                    schedule["retry_pending"] = True
                    schedule["next_attempt_at"] = current_ms
                if not schedule_due(schedule, current_ms, recovered):
                    continue
                if schedule.get("kind") == "interval":
                    # An interval schedule is a watchdog, not a repeating message.
                    # Each due interval observes the target first and only queues a
                    # continuation when the target is no longer running.
                    target_status = current.get(str(schedule.get("thread_id") or ""))
                    schedule["last_check_at"] = current_ms
                    schedule["last_observed_status"] = target_status or "stopped"
                    if target_status == "active":
                        schedule["waiting_for_quota"] = False
                        schedule["retry_pending"] = False
                        schedule["next_attempt_at"] = None
                        schedule["blocked_reason"] = None
                        schedule["consecutive_failures"] = 0
                        continue
                    if schedule.get("awaiting_active"):
                        continue
                    if target_status == "usage_limited" and schedule.get("retry_on_quota", True):
                        schedule["waiting_for_quota"] = True
                        schedule["retry_pending"] = False
                        schedule["next_attempt_at"] = None
                        schedule["blocked_reason"] = "等待额度恢复"
                        continue
                allowed, budget_reason = budget_guard(schedule)
                if not allowed:
                    schedule["enabled"] = False
                    schedule["blocked_reason"] = budget_reason
                    app.setdefault("events", []).append({"at": iso(current_ms), "kind": "budget_blocked", "schedule_id": schedule.get("id"), "message": f"{schedule.get('name', '计划')}：{budget_reason}"})
                    continue
                ok, detail, execution_mode = restore_goal(
                    str(schedule.get("thread_id") or ""),
                    str(schedule.get("message") or "继续之前的任务"),
                    str(schedule.get("execution_mode") or "auto"),
                )
                attempt_at = now_ms()
                schedule["last_attempt_at"] = attempt_at
                schedule["last_result"] = {"ok": ok, "detail": detail, "at": iso(attempt_at), "execution_mode": execution_mode}
                schedule["attempt_count"] = int(schedule.get("attempt_count") or 0) + 1
                schedule["retry_pending"] = False
                schedule["next_attempt_at"] = None
                schedule["blocked_reason"] = None
                error_kind = None if ok else classify_enqueue_error(detail)
                if ok:
                    schedule["last_run_at"] = attempt_at
                    schedule["run_count"] = int(schedule.get("run_count") or 0) + 1
                    schedule["consecutive_failures"] = 0
                    schedule["waiting_for_quota"] = False
                    if schedule.get("kind") == "interval":
                        schedule["awaiting_active"] = True
                    if schedule.get("kind") == "at_time":
                        schedule["enabled"] = False
                        schedule["completed_at"] = attempt_at
                elif error_kind == "quota" and schedule.get("retry_on_quota", True):
                    schedule["waiting_for_quota"] = True
                    schedule["consecutive_failures"] = int(schedule.get("consecutive_failures") or 0) + 1
                elif error_kind == "network" and schedule.get("retry_on_network", True):
                    failures = int(schedule.get("consecutive_failures") or 0) + 1
                    configured_attempts = schedule.get("max_attempts")
                    if configured_attempts in (None, ""):
                        configured_attempts = settings.get("default_network_retries", 0)
                    try:
                        max_attempts = max(0, int(configured_attempts))
                    except (TypeError, ValueError):
                        max_attempts = 0
                    # max_attempts=0 is intentionally unlimited. A retry remains
                    # pending until it succeeds or the user pauses the schedule.
                    if max_attempts == 0 or failures <= max_attempts:
                        backoff = max(1, int(schedule.get("backoff_seconds") or settings.get("default_backoff_seconds") or 30))
                        # Keep retry timing bounded while allowing an unlimited
                        # number of attempts. This avoids integer/timestamp overflow
                        # after a long offline period.
                        delay_ms = min(backoff * (2 ** min(failures - 1, 12)) * 1000, 6 * 60 * 60 * 1000)
                        schedule["next_attempt_at"] = attempt_at + delay_ms
                        schedule["retry_pending"] = True
                        schedule["blocked_reason"] = f"网络失败，等待第 {failures} 次重试"
                    else:
                        schedule["enabled"] = False
                        schedule["blocked_reason"] = f"网络重试已用尽：{detail}"
                    schedule["consecutive_failures"] = failures
                else:
                    schedule["consecutive_failures"] = int(schedule.get("consecutive_failures") or 0) + 1
                    schedule["blocked_reason"] = f"{error_kind or 'unknown'}：{detail}"
                    schedule["enabled"] = False
                app.setdefault("events", []).append({
                    "at": iso(attempt_at), "kind": "schedule_run", "schedule_id": schedule.get("id"),
                    "message": f"{schedule.get('name', '未命名')}：{'目标恢复已加入队列' if ok and execution_mode == 'goal' else '消息已加入队列' if ok else f'执行失败（{error_kind}）'}", "detail": detail,
                    "execution_mode": execution_mode,
                })
                if settings.get("notifications", True):
                    notification("Auto Codex Companion", f"{schedule.get('name', '计划')}：{'已加入队列' if ok else f'执行失败（{error_kind}）'}")
            app["events"] = app.get("events", [])[-100:]
            write_app_state(app)


def payload(handler: BaseHTTPRequestHandler) -> bytes:
    length = int(handler.headers.get("Content-Length", "0"))
    return handler.rfile.read(min(length, 1_000_000)) if length else b"{}"


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoCodex/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[autocodex] " + (fmt % args) + "\n")

    def send_json(self, value: Any, status: int = 200) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/overview":
            app = read_app_state() or default_app_state()
            threads = get_thread_rows()
            self.send_json({"inventory": inventory(), "quota": quota_snapshot(), "tokens": token_snapshot(), "official_usage": app.get("official_usage") or {"status": "not_checked"}, "usage_config": safe_usage_config(app.get("usage_config")), "usage_probe": app.get("usage_probe") or {"status": "not_checked"}, "settings": app.get("settings") or default_app_state()["settings"], "monitor": app.get("monitor") or default_app_state()["monitor"], "threads": threads, "projects": get_project_rows(threads), "schedules": app.get("schedules", []), "events": list(reversed(app.get("events", [])[-30:]))})
            return
        if parsed.path == "/api/threads":
            self.send_json({"threads": get_thread_rows()})
            return
        if parsed.path == "/api/projects":
            self.send_json({"projects": get_project_rows()})
            return
        if parsed.path == "/api/inventory":
            self.send_json(inventory())
            return
        if parsed.path == "/api/events":
            app = read_app_state() or default_app_state()
            self.send_json({"events": list(reversed(app.get("events", [])[-100:]))})
            return
        if parsed.path == "/api/settings":
            app = read_app_state() or default_app_state()
            self.send_json({"settings": app.get("settings") or default_app_state()["settings"]})
            return
        if parsed.path == "/":
            self.serve_file(APP_DIR / "static/index.html", "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/static/"):
            name = parsed.path.removeprefix("/static/")
            allowed = {"app.js": "application/javascript; charset=utf-8", "styles.css": "text/css; charset=utf-8", "overrides.css": "text/css; charset=utf-8"}
            if name in allowed:
                self.serve_file(APP_DIR / "static" / name, allowed[name])
                return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def serve_file(self, path: Path, content_type: str) -> None:
        try:
            raw = path.read_bytes()
        except OSError:
            self.send_json({"error": "file not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        # Treat each read/modify/write request as one state transaction. The
        # lock is re-entrant because the state helpers also protect disk I/O.
        with STATE_LOCK:
            self._do_POST_locked()

    def _do_POST_locked(self) -> None:
        parsed = urlparse(self.path)
        try:
            data = json.loads(payload(self).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.send_json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
            return
        app = read_app_state() or default_app_state()
        if parsed.path == "/api/schedules":
            kind = str(data.get("kind") or "")
            if kind not in {"interval", "at_time", "quota_recovered"}:
                self.send_json({"error": "kind 必须是 interval、at_time 或 quota_recovered"}, HTTPStatus.BAD_REQUEST); return
            thread_id = str(data.get("thread_id") or "").strip()
            message = str(data.get("message") or "").strip()
            execution_mode = str(data.get("execution_mode") or "auto").strip().lower()
            if execution_mode not in {"auto", "goal", "message"}:
                self.send_json({"error": "继续方式必须是 auto、goal 或 message"}, HTTPStatus.BAD_REQUEST); return
            if not thread_id or (execution_mode == "message" and not message):
                self.send_json({"error": "目标会话不能为空；消息模式必须填写继续消息"}, HTTPStatus.BAD_REQUEST); return
            if not any(str(row.get("id") or "") == thread_id for row in get_thread_rows()):
                self.send_json({"error": "目标会话不存在或已不在最近会话列表"}, HTTPStatus.BAD_REQUEST); return
            if execution_mode == "goal" and not any(str(row.get("thread_id") or "") == thread_id for row in get_goal_rows()):
                self.send_json({"error": "选择的会话没有目标，不能使用继续目标模式"}, HTTPStatus.BAD_REQUEST); return
            try:
                interval_minutes = max(1, int(data.get("interval_minutes") or 60)) if kind == "interval" else None
                max_attempts = max(0, int(data.get("max_attempts") or 0))
                backoff_seconds = max(1, int(data.get("backoff_seconds") or 30))
            except (TypeError, ValueError):
                self.send_json({"error": "间隔、重试次数和退避时间必须是有效整数"}, HTTPStatus.BAD_REQUEST); return
            run_at = str(data.get("run_at") or "").strip() if kind == "at_time" else None
            if kind == "at_time":
                run_at_ms = parse_when(run_at)
                if run_at_ms is None:
                    self.send_json({"error": "指定时间格式无效"}, HTTPStatus.BAD_REQUEST); return
                if run_at_ms < now_ms():
                    self.send_json({"error": "指定时间必须晚于当前时间"}, HTTPStatus.BAD_REQUEST); return
            try:
                token_budget = int(data["token_budget"]) if str(data.get("token_budget") or "").strip() else None
                price_budget = float(data["price_budget_usd"]) if str(data.get("price_budget_usd") or "").strip() else None
                price_per_1k = float(data["price_per_1k_tokens"]) if str(data.get("price_per_1k_tokens") or "").strip() else None
            except (TypeError, ValueError):
                self.send_json({"error": "预算必须是有效数字"}, HTTPStatus.BAD_REQUEST); return
            if token_budget is not None and token_budget <= 0:
                self.send_json({"error": "Token 预算必须大于 0"}, HTTPStatus.BAD_REQUEST); return
            if (price_budget is None) != (price_per_1k is None):
                self.send_json({"error": "价格预算和每 1K tokens 单价必须同时填写"}, HTTPStatus.BAD_REQUEST); return
            if price_budget is not None and (price_budget <= 0 or price_per_1k is None or price_per_1k <= 0):
                self.send_json({"error": "价格预算和单价必须大于 0"}, HTTPStatus.BAD_REQUEST); return
            item = {
                "id": str(uuid.uuid4()), "name": str(data.get("name") or "未命名计划"), "kind": kind,
                "thread_id": thread_id, "message": message, "execution_mode": execution_mode, "enabled": bool(data.get("enabled", True)),
                "interval_minutes": interval_minutes, "run_at": run_at,
                "token_budget": token_budget, "price_budget_usd": price_budget, "price_per_1k_tokens": price_per_1k,
                "retry_on_network": bool(data.get("retry_on_network", True)), "retry_on_quota": bool(data.get("retry_on_quota", True)),
                "max_attempts": max_attempts, "backoff_seconds": backoff_seconds,
                "created_at": now_ms(), "last_run_at": None, "last_check_at": None, "last_observed_status": None,
                "last_attempt_at": None, "run_count": 0, "last_result": None,
                "attempt_count": 0, "consecutive_failures": 0, "waiting_for_quota": False, "retry_pending": False,
                "next_attempt_at": None, "blocked_reason": None, "completed_at": None, "awaiting_active": False,
            }
            app.setdefault("schedules", []).append(item); write_app_state(app); self.send_json(item, HTTPStatus.CREATED); return
        if parsed.path == "/api/schedules/toggle":
            item = next((x for x in app.get("schedules", []) if x.get("id") == data.get("id")), None)
            if item is None: self.send_json({"error": "计划不存在"}, HTTPStatus.NOT_FOUND); return
            enabled = bool(data.get("enabled"))
            item["enabled"] = enabled
            item["waiting_for_quota"] = False
            item["retry_pending"] = False
            item["next_attempt_at"] = None
            item["consecutive_failures"] = 0
            if enabled:
                item["blocked_reason"] = None
                if item.get("completed_at"):
                    item["completed_at"] = None
                    item["last_run_at"] = None
            write_app_state(app); self.send_json(item); return
        if parsed.path == "/api/schedules/delete":
            old = len(app.get("schedules", [])); app["schedules"] = [x for x in app.get("schedules", []) if x.get("id") != data.get("id")]
            if len(app["schedules"]) == old: self.send_json({"error": "计划不存在"}, HTTPStatus.NOT_FOUND); return
            write_app_state(app); self.send_json({"ok": True}); return
        if parsed.path == "/api/enqueue":
            ok, detail = enqueue(str(data.get("thread_id") or ""), str(data.get("message") or ""))
            app.setdefault("events", []).append({"at": iso(now_ms()), "kind": "manual_enqueue", "message": "手动加入队列", "detail": detail, "ok": ok})
            app["events"] = app["events"][-100:]; write_app_state(app)
            self.send_json({"ok": ok, "detail": detail}, 200 if ok else 502); return
        if parsed.path == "/api/goals/resume":
            thread_id = str(data.get("thread_id") or "").strip()
            message = str(data.get("message") or "继续之前的任务；先检查当前状态，再从上次停下的位置继续。").strip()
            execution_mode = str(data.get("execution_mode") or "auto").strip().lower()
            row = next((item for item in get_thread_rows() if item.get("id") == thread_id), None)
            if row is None:
                self.send_json({"error": "未找到这个线程，可能已不在最近线程列表中"}, HTTPStatus.NOT_FOUND); return
            was_archived = bool(row.get("archived"))
            ok, detail, resolved_mode = restore_goal(thread_id, message, execution_mode)
            app.setdefault("events", []).append({
                "at": iso(now_ms()), "kind": "goal_resume", "message": "继续目标" if ok and resolved_mode == "goal" else "继续会话" if ok else "继续失败",
                "detail": detail, "ok": ok, "thread_id": thread_id, "unarchived": was_archived, "execution_mode": resolved_mode,
            })
            app["events"] = app["events"][-100:]; write_app_state(app)
            self.send_json({"ok": ok, "detail": detail, "unarchived": was_archived, "execution_mode": resolved_mode}, 200 if ok else 502); return
        if parsed.path == "/api/usage-config":
            base_url = str(data.get("base_url") or "").strip()
            path = str(data.get("path") or "/v1/usage").strip()
            api_key = str(data.get("api_key") or "").strip()
            existing = app.get("usage_config") or {}
            if not api_key:
                api_key = str(existing.get("api_key") or "")
            if base_url and not re.match(r"^https?://", base_url):
                self.send_json({"error": "base_url 必须以 http:// 或 https:// 开头"}, HTTPStatus.BAD_REQUEST); return
            app["usage_config"] = {"enabled": bool(data.get("enabled", True)) and bool(base_url and api_key), "base_url": base_url, "path": path or "/v1/usage", "api_key": api_key, "unit": str(data.get("unit") or existing.get("unit") or "USD"), "poll_minutes": max(1, int(data.get("poll_minutes") or existing.get("poll_minutes") or 5))}
            app["usage_probe"] = {"status": "not_checked"}
            write_app_state(app)
            self.send_json({"config": safe_usage_config(app["usage_config"])})
            return
        if parsed.path == "/api/usage-check":
            result = usage_probe(app, force=True)
            app["usage_probe"] = result
            app.setdefault("events", []).append({"at": iso(now_ms()), "kind": "usage_check", "message": "手动检查额度", "ok": result.get("status") == "ok"})
            app["events"] = app["events"][-100:]
            write_app_state(app)
            self.send_json({"probe": result})
            return
        if parsed.path == "/api/official-usage-check":
            result = current_official_usage_probe()
            app["official_usage"] = result
            app.setdefault("events", []).append({"at": iso(now_ms()), "kind": "official_usage_check", "message": "通过 Codex CLI 检查额度", "ok": result.get("status") == "ok", "status": result.get("status"), "source": result.get("credential_source"), "http_status": result.get("http_status")})
            app["events"] = app["events"][-100:]
            write_app_state(app)
            self.send_json({"probe": result})
            return
        if parsed.path == "/api/cli-detect":
            manual_path = str(data.get("path") or "").strip()
            info = codex_command_info(manual_path)
            if info.get("found"):
                command = str(info["path"])
                try:
                    completed = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=4)
                    info["version"] = (completed.stdout or completed.stderr).strip() or None
                    if completed.returncode != 0:
                        info.update({"found": False, "detail": "目标文件无法作为 Codex CLI 运行"})
                except (OSError, subprocess.SubprocessError) as exc:
                    info.update({"found": False, "detail": str(exc)[:300]})
            self.send_json({"cli": info}, 200 if info.get("found") else 404)
            return
        if parsed.path == "/api/cli-status-check":
            # Manual refresh uses the same fallback chain as the scheduler:
            # CLI app-server first, then the local OAuth usage endpoint. This
            # avoids showing a CLI-only failure when the OAuth path is usable.
            result = current_official_usage_probe()
            if result.get("status") == "ok":
                app["official_usage"] = result
            app.setdefault("events", []).append({"at": iso(now_ms()), "kind": "official_usage_check", "message": "检查官方额度（CLI 失败时回退 OAuth）", "ok": result.get("status") == "ok", "status": result.get("status"), "source": result.get("credential_source")})
            app["events"] = app["events"][-100:]
            write_app_state(app)
            self.send_json({"probe": result}, 200 if result.get("status") == "ok" else 502)
            return
        if parsed.path == "/api/settings":
            settings = app.get("settings") or default_app_state()["settings"]
            requested_cli_path = str(data.get("codex_cli_path") if data.get("codex_cli_path") is not None else settings.get("codex_cli_path") or "").strip()
            if requested_cli_path and not codex_command_info(requested_cli_path).get("found"):
                self.send_json({"error": "指定路径中没有找到可执行的 Codex CLI；可以选择 codex 文件或其所在目录"}, HTTPStatus.BAD_REQUEST); return
            try:
                requested_close_behavior = str(data.get("close_behavior") or settings.get("close_behavior") or "tray").strip().lower()
                if requested_close_behavior not in {"tray", "quit"}:
                    requested_close_behavior = "tray"
                settings.update({
                    "poll_seconds": max(5, int(data.get("poll_seconds") or settings.get("poll_seconds") or POLL_SECONDS)),
                    "official_poll_minutes": max(1, int(data.get("official_poll_minutes") or settings.get("official_poll_minutes") or 5)),
                    "codex_cli_path": requested_cli_path,
                    "notifications": bool(data.get("notifications", settings.get("notifications", True))),
                    "close_behavior": requested_close_behavior,
                    "default_network_retries": max(0, int(data.get("default_network_retries") if data.get("default_network_retries") not in (None, "") else settings.get("default_network_retries", 0))),
                    "default_backoff_seconds": max(1, int(data.get("default_backoff_seconds") or settings.get("default_backoff_seconds") or 30)),
                })
            except (TypeError, ValueError):
                self.send_json({"error": "设置值格式不正确"}, HTTPStatus.BAD_REQUEST); return
            app["settings"] = settings; write_app_state(app); self.send_json({"settings": settings}); return
        if parsed.path == "/api/scan":
            self.send_json({"inventory": inventory(), "quota": quota_snapshot(), "threads": get_thread_rows()}); return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists(): write_app_state(default_app_state())
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    # Bind the local API before the first quota probe. The initial CLI/OAuth
    # check may take several seconds; the desktop window should still become
    # usable immediately while the scheduler works in its daemon thread.
    scheduler = Scheduler(); scheduler.start()
    print(f"Auto Codex Companion running at http://{HOST}:{PORT}")
    print(f"Codex home: {CODEX_HOME}")
    try:
        if os.environ.get("AUTOCODEX_OPEN_BROWSER", "1") != "0":
            threading.Timer(0.4, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop(); server.server_close()


if __name__ == "__main__":
    main()
