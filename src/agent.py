#!/usr/bin/env python3
"""Outbound Hermes fleet sidecar.

Phones home to the hub. Does not run `hermes update` and does not copy
platform tokens. If Hermes is down, chat returns error text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HEARTBEAT_S = 15
RECONNECT_S = 5
AGENT_ENV_KEYS = {
    "HUB_URL",
    "AGENT_ID",
    "FLEET_TOKEN",
    "SITE",
    "HERMES_BIN",
    "CHAT_TIMEOUT",
    "API_SERVER_ENABLED",
    "API_SERVER_HOST",
    "API_SERVER_PORT",
    "API_SERVER_KEY",
    "HERMES_HOME",
    "FLEET_AGENT_LOG",
}
_version = ""
_version_at = 0.0


def _parse_env(path: Path, allow: set[str] | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if allow is not None and key not in allow:
            continue
        out[key] = value.strip().strip("'\"")
    return out


def load_agent_env() -> None:
    candidates = []
    if os.environ.get("AGENT_ENV"):
        candidates.append(Path(os.environ["AGENT_ENV"]))
    here = Path(__file__).resolve().parent
    candidates.extend([Path.cwd() / "agent.env", here / "agent.env", here.parent / "agent.env"])
    for path in candidates:
        if path.is_file():
            for key, value in _parse_env(path, AGENT_ENV_KEYS).items():
                os.environ.setdefault(key, value)
            return


def hermes_home() -> Path:
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"])
    local = os.environ.get("LOCALAPPDATA")
    if local:
        win = Path(local) / "hermes"
        if win.is_dir():
            return win
    return Path.home() / ".hermes"


def _scrub(text: str) -> str:
    token = os.environ.get("FLEET_TOKEN") or ""
    key = os.environ.get("API_SERVER_KEY") or ""
    if token:
        text = text.replace(token, "[redacted]")
    if key:
        text = text.replace(key, "[redacted]")
    kept = []
    for line in text.splitlines():
        low = line.lower()
        if any(bit in low for bit in ("token=", "api_key", "password", "secret", "authorization")):
            continue
        kept.append(line)
    return "\n".join(kept)[:400]


def setup_log() -> None:
    path = os.environ.get("FLEET_AGENT_LOG")
    if not path:
        path = str(Path(__file__).resolve().parent / "agent.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stderr)],
    )


def ws_url(agent_id: str) -> str:
    base = (os.environ.get("HUB_URL") or "").strip().rstrip("/")
    if base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    elif base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base and not base.startswith(("ws://", "wss://")):
        base = "ws://" + base
    suffix = "/ws/agent/" + agent_id
    if base.endswith(suffix):
        return base
    return base + suffix


def read_model(home: Path) -> tuple[str, str]:
    path = home / "config.yaml"
    model = provider = ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", ""
    in_model = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_model = line.startswith("model:")
            continue
        if not in_model:
            continue
        stripped = line.strip()
        if stripped.startswith("default:"):
            model = stripped.split(":", 1)[1].strip().strip("'\"")
        elif stripped.startswith("provider:"):
            provider = stripped.split(":", 1)[1].strip().strip("'\"")
    return model[:128], provider[:64]


def read_gateway(home: Path) -> tuple[dict[str, str], list[str]]:
    path = home / "gateway_state.json"
    platforms: dict[str, str] = {}
    services: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return platforms, services
    raw = data.get("platforms") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        for name, info in list(raw.items())[:32]:
            if not isinstance(name, str):
                continue
            state = info.get("state") if isinstance(info, dict) else info
            state_s = str(state or "")[:64]
            platforms[name[:64]] = state_s
            if state_s == "connected":
                services.append(name[:64])
    if isinstance(data, dict) and data.get("gateway_state") == "running":
        services.append("hermes-gateway")
    return platforms, services


def read_tokens(home: Path) -> tuple[int, int, str]:
    path = home / "state.db"
    if not path.is_file():
        return 0, 0, ""
    try:
        con = sqlite3.connect(str(path), timeout=1)
        con.execute("PRAGMA query_only=ON")
        row = con.execute(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM sessions"
        ).fetchone()
        tin, tout = int(row[0] or 0), int(row[1] or 0)
        job = ""
        try:
            last = con.execute(
                "SELECT COALESCE(title,''), COALESCE(last_activity_description,'') "
                "FROM sessions ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 1"
            ).fetchone()
            if last:
                job = (last[0] or last[1] or "")[:200]
        except sqlite3.Error:
            job = ""
        con.close()
        return tin, tout, job
    except Exception:
        return 0, 0, ""


def sample_host() -> tuple[float | None, float | None]:
    cpu = None
    ram = None
    if os.name == "nt":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            ram = float(stat.dwMemoryLoad)
        return cpu, ram
    try:
        cpu = round(float(os.getloadavg()[0]), 2)
    except (AttributeError, OSError):
        cpu = None
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            info[key] = int(value.strip().split()[0])
        total = info.get("MemTotal") or 0
        avail = info.get("MemAvailable") or 0
        if total:
            ram = round((total - avail) * 100 / total, 1)
    except (OSError, ValueError):
        ram = None
    return cpu, ram


def hermes_bin() -> str | None:
    """Find hermes after updates. Do not pin a venv path that dies on `hermes update`."""
    explicit = os.environ.get("HERMES_BIN")
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
    found = shutil.which("hermes")
    if found:
        return found
    home = os.environ.get("LOCALAPPDATA") or os.environ.get("HOME") or ""
    candidates = [
        Path(home) / "hermes" / "hermes-agent" / "venv" / "Scripts" / "hermes.exe",
        Path(home) / "hermes" / "hermes-agent" / "venv" / "bin" / "hermes",
        Path(home) / ".local" / "bin" / "hermes",
        Path("/usr/local/bin/hermes"),
    ]
    if os.environ.get("LOCALAPPDATA"):
        candidates.insert(
            0,
            Path(os.environ["LOCALAPPDATA"]) / "hermes" / "hermes-agent" / "venv" / "Scripts" / "hermes.exe",
        )
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def hermes_version() -> str:
    global _version, _version_at
    now = time.time()
    if _version and now - _version_at < 3600:
        return _version
    binary = hermes_bin()
    if not binary:
        return ""
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            timeout=3,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        text = (proc.stdout or proc.stderr or b"").decode("utf-8", "replace").strip()
        _version = (text.splitlines()[0] if text else "")[:64]
        _version_at = now
        return _version
    except Exception:
        return ""


def _api_env() -> dict[str, str]:
    allow = {"API_SERVER_ENABLED", "API_SERVER_HOST", "API_SERVER_PORT", "API_SERVER_KEY"}
    found = {k: os.environ[k] for k in allow if os.environ.get(k)}
    env_path = hermes_home() / ".env"
    file_vals = _parse_env(env_path, allow)
    for key, value in file_vals.items():
        found.setdefault(key, value)
    return found


def api_enabled() -> bool:
    flag = _api_env().get("API_SERVER_ENABLED", "")
    return flag.lower() in ("1", "true", "yes", "on")


def chat_via_api(text: str, timeout: float) -> tuple[bool, str] | None:
    """None means the API was unreachable and the CLI may be tried."""
    conf = _api_env()
    host = conf.get("API_SERVER_HOST") or "127.0.0.1"
    if host in ("0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    port = conf.get("API_SERVER_PORT") or "8642"
    key = conf.get("API_SERVER_KEY") or ""
    url = f"http://{host}:{port}/v1/chat/completions"
    body = json.dumps(
        {"model": "hermes-agent", "messages": [{"role": "user", "content": text}], "stream": False}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        content = data["choices"][0]["message"]["content"]
        return True, str(content)
    except urllib.error.HTTPError as exc:
        return False, f"Hermes API error: HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, KeyError, json.JSONDecodeError, TypeError):
        return None


def chat_via_cli(text: str, timeout: float) -> tuple[bool, str]:
    binary = hermes_bin()
    if not binary:
        return False, "Hermes is down (API unreachable and hermes CLI not found)."
    fd, path = tempfile.mkstemp(prefix="fleet-q-", suffix=".txt")
    try:
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
        fd = -1
        proc = subprocess.run(
            [binary, "chat", "--query-file", path, "-Q", "--oneshot"],
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "Hermes is down (CLI timed out)."
    except Exception as exc:
        return False, f"Hermes is down ({type(exc).__name__})."
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.remove(path)
        except OSError:
            pass
    if proc.returncode != 0:
        err = _scrub((proc.stderr or b"").decode("utf-8", "replace").strip())
        return False, f"Hermes is down (CLI exit {proc.returncode}). {err}".strip()
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    return True, out or "(empty reply)"


def ask_hermes(text: str) -> tuple[bool, str]:
    timeout = float(os.environ.get("CHAT_TIMEOUT") or "20")
    text = text[:16000]
    try:
        if api_enabled():
            api = chat_via_api(text, timeout)
            if api is not None:
                return api
        return chat_via_cli(text, timeout)
    except Exception as exc:
        return False, f"Hermes is down ({type(exc).__name__})."


def snapshot(agent_id: str) -> dict:
    home = hermes_home()
    platforms, services = read_gateway(home)
    model, provider = read_model(home)
    tokens_in, tokens_out, last_job = read_tokens(home)
    cpu, ram = sample_host()
    body = {
        "agent_id": agent_id,
        "hostname": socket.gethostname()[:128],
        "site": (os.environ.get("SITE") or "")[:64],
        "hermes_version": hermes_version(),
        "platforms": platforms,
        "model": model,
        "provider": provider,
        "cpu": cpu,
        "ram": ram,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "last_job": last_job,
        "services": services,
    }
    return body


def _ws_connect(url: str, token: str):
    import websockets

    headers = {"X-Fleet-Token": token}
    try:
        return websockets.connect(url, additional_headers=headers, open_timeout=10, max_size=262144)
    except TypeError:
        return websockets.connect(url, extra_headers=headers, open_timeout=10, max_size=262144)


async def run_once(agent_id: str, token: str) -> None:
    url = ws_url(agent_id)
    async with _ws_connect(url, token) as ws:
        hello = snapshot(agent_id)
        hello["type"] = "hello"
        await ws.send(json.dumps(hello))
        logging.info("connected agent_id=%s", agent_id)

        async def beats() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_S)
                beat = snapshot(agent_id)
                beat["type"] = "heartbeat"
                await ws.send(json.dumps(beat))

        hb = asyncio.create_task(beats())
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                typ = str(msg.get("type") or "")
                if typ == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif typ == "chat":
                    rid = str(msg.get("request_id") or "")
                    text = str(msg.get("text") or "")
                    ok, reply = await asyncio.to_thread(ask_hermes, text)
                    await ws.send(
                        json.dumps(
                            {"type": "chat_result", "request_id": rid, "text": reply, "ok": ok}
                        )
                    )
                    logging.info("chat request_id=%s ok=%s", rid, ok)
        finally:
            hb.cancel()
        logging.info("disconnected agent_id=%s", agent_id)


async def main() -> None:
    load_agent_env()
    setup_log()
    agent_id = (os.environ.get("AGENT_ID") or "").strip()
    token = os.environ.get("FLEET_TOKEN") or ""
    hub = os.environ.get("HUB_URL") or ""
    if not agent_id or not token or not hub:
        logging.error("AGENT_ID, FLEET_TOKEN, and HUB_URL are required")
        sys.exit(1)
    logging.info("sidecar start agent_id=%s", agent_id)
    while True:
        try:
            await run_once(agent_id, token)
        except Exception as exc:
            logging.warning("reconnect in %ss: %s", RECONNECT_S, _scrub(f"{type(exc).__name__}: {exc}"))
        await asyncio.sleep(RECONNECT_S)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
