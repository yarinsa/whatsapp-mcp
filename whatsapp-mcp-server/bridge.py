"""Health checks for the Go WhatsApp bridge.

Every MCP tool calls `require_bridge()` first, so a dead, logged-out or
impostor bridge (another app on the port) produces a clear error instead of
empty or misleading results.
"""
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

BRIDGE_PORT = int(os.environ.get("WHATSAPP_BRIDGE_PORT", "8765"))
API_BASE_URL = os.environ.get(
    "WHATSAPP_API_BASE_URL", f"http://127.0.0.1:{BRIDGE_PORT}/api"
).rstrip("/")
SERVICE_NAME = "whatsapp-bridge"
# Warn in bridge_status when no message arrived for this long while connected.
QUIET_WARN_SECONDS = int(os.environ.get("WHATSAPP_QUIET_WARN_SECONDS", str(6 * 3600)))
CACHE_SECONDS = 10

_cache: Dict[str, Any] = {"at": 0.0, "status": None}


class BridgeUnavailable(RuntimeError):
    """Raised when the bridge can't serve requests; the message is user-facing."""


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ago(ts: Optional[str]) -> str:
    t = _parse(ts)
    if not t:
        return "unknown"
    secs = int((datetime.now(timezone.utc) - t).total_seconds())
    if secs < 3600:
        span = f"{secs // 60}m"
    elif secs < 86400:
        span = f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    else:
        span = f"{secs // 86400}d{(secs % 86400) // 3600}h"
    return f"{t.astimezone().strftime('%Y-%m-%d %H:%M')} ({span} ago)"


def bridge_status(use_cache: bool = False) -> Dict[str, Any]:
    """Probe /api/health and return a summary with `ok` and a human `summary`."""
    if use_cache and _cache["status"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["status"]

    url = f"{API_BASE_URL}/health"
    result: Dict[str, Any] = {"ok": False, "url": url}
    try:
        resp = requests.get(url, timeout=3)
    except requests.RequestException as e:
        result.update(state="down", summary=f"Bridge is down: nothing answering at {url} ({e.__class__.__name__}).")
        return _store(result)

    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code != 200 or not isinstance(data, dict) or data.get("service") != SERVICE_NAME:
        result.update(
            state="wrong_service",
            summary=f"Port {BRIDGE_PORT} is answered by another service (HTTP {resp.status_code}), "
                    f"not the WhatsApp bridge (or the bridge build is too old to have /api/health). Set WHATSAPP_BRIDGE_PORT to a free port for both bridge and server.",
        )
        return _store(result)

    result["health"] = data
    last_msg = data.get("last_message_at")
    status = data.get("status")
    if status == "logged_out":
        result.update(state="logged_out", summary=f"Bridge is logged out of WhatsApp; re-pair with a QR scan. Data is stale since {_ago(last_msg)}.")
    elif status != "ok":
        result.update(state="disconnected", summary=f"Bridge is running but disconnected since {_ago(data.get('disconnected_since'))}. Last message {_ago(last_msg)}.")
    else:
        result.update(ok=True, state="ok", summary=f"Connected since {_ago(data.get('connected_since'))}. Last message {_ago(last_msg)}.")
        quiet = data.get("seconds_since_last_message")
        if quiet is not None and quiet > QUIET_WARN_SECONDS:
            result["warning"] = f"Connected but no messages for {quiet // 3600}h; sync may be stuck."
    return _store(result)


def _store(result: Dict[str, Any]) -> Dict[str, Any]:
    _cache.update(at=time.time(), status=result)
    return result


def require_bridge() -> None:
    """Raise BridgeUnavailable unless the bridge is ours and connected."""
    status = bridge_status(use_cache=True)
    if not status["ok"]:
        raise BridgeUnavailable(status["summary"])
