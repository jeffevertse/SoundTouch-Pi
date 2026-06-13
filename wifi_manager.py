"""
WiFi management via nmcli (NetworkManager CLI).
Works on Raspberry Pi OS Bookworm+ which uses NetworkManager by default.

Key flows
---------
Client mode  — Pi is connected to a normal WiFi network.
Hotspot mode — Pi creates "SoundTouch-Pi-Setup" (10.42.0.1).  User connects to it
               from a phone or laptop, visits https://soundtouch-pi.local:5000 (or
               http://10.42.0.1:5000) and configures the target network.
Auto-hotspot — called by server.py startup if Pi has no WiFi after 30 s.
"""
from __future__ import annotations

import subprocess
import threading
import time

HOTSPOT_SSID = "SoundTouch-Pi-Setup"
HOTSPOT_IP   = "10.42.0.1"


# ── input validation ───────────────────────────────────────────────────────

def _validate_ssid(ssid: str) -> None:
    if not ssid:
        raise ValueError("SSID must not be empty")
    if len(ssid) > 32:
        raise ValueError("SSID must be 32 characters or fewer")
    if ssid.startswith("-"):
        raise ValueError("SSID must not start with '-'")


def _validate_password(password: str) -> None:
    if len(password) > 63:
        raise ValueError("WiFi password must be 63 characters or fewer")


# ── low-level helper ───────────────────────────────────────────────────────

def _nmcli(*args, sudo: bool = False, timeout: int = 20) -> tuple[int, str, str]:
    """Run nmcli (optionally with sudo) and return (rc, stdout, stderr)."""
    cmd = (["sudo"] if sudo else []) + ["nmcli"] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def _nmcli_rw(*args, timeout: int = 30) -> tuple[int, str, str]:
    """Write-mode nmcli: try without sudo first, fall back to sudo."""
    rc, out, err = _nmcli(*args, sudo=False, timeout=timeout)
    if rc != 0:
        rc, out, err = _nmcli(*args, sudo=True, timeout=timeout)
    return rc, out, err


# ── status ─────────────────────────────────────────────────────────────────

def get_status() -> dict:
    """
    Return current WiFi status dict:
      mode       : "client" | "hotspot" | "disconnected"
      connected  : bool
      ssid       : str | None
      signal     : int (0-100) | None
      ip         : str | None
    """
    rc, out, _ = _nmcli("-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi")

    for line in out.splitlines():
        parts = line.split(":")
        if parts and parts[0] == "yes":
            ssid   = parts[1] if len(parts) > 1 else ""
            signal = parts[2] if len(parts) > 2 else "0"
            _, ip_out, _ = _nmcli("-g", "IP4.ADDRESS", "device", "show", "wlan0")
            ip = ip_out.split("/")[0] if ip_out else None
            return {"mode": "client", "connected": True,
                    "ssid": ssid, "signal": _safe_int(signal), "ip": ip}

    # Check if in hotspot mode
    _, conn_out, _ = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show", "--active")
    for line in conn_out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and "wireless" in parts[1].lower():
            if "hotspot" in parts[0].lower():
                return {"mode": "hotspot", "connected": False,
                        "ssid": HOTSPOT_SSID, "signal": None, "ip": HOTSPOT_IP}

    return {"mode": "disconnected", "connected": False, "ssid": None,
            "signal": None, "ip": None}


def _safe_int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except Exception:
        return default


# ── scan ───────────────────────────────────────────────────────────────────

def scan_networks() -> list[dict]:
    """
    Return list of nearby WiFi networks sorted by signal strength.
    Each dict: {ssid, signal, security, open}
    """
    rc, out, _ = _nmcli("-t", "-f", "SSID,SIGNAL,SECURITY",
                        "dev", "wifi", "list", "--rescan", "yes")
    seen     = set()
    networks = []
    for line in out.splitlines():
        # nmcli -t escapes colons inside values as \:
        parts = line.replace("\\:", "\x00").split(":")
        ssid  = parts[0].replace("\x00", ":").strip() if parts else ""
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        signal   = _safe_int(parts[1]) if len(parts) > 1 else 0
        security = parts[2].strip()     if len(parts) > 2 else ""
        networks.append({
            "ssid":     ssid,
            "signal":   signal,
            "security": security,
            "open":     not security or security in ("--", ""),
        })
    networks.sort(key=lambda x: x["signal"], reverse=True)
    return networks


# ── connect ────────────────────────────────────────────────────────────────

def connect_wifi(ssid: str, password: str,
                 on_done=None, delay: float = 1.5):
    """
    Connect to a WiFi network in a background thread.
    Uses nmcli connection add/up (key-value API) to avoid argument injection.
    on_done(ok: bool, message: str) is called on completion (optional).
    """
    def _run():
        time.sleep(delay)
        print(f"[wifi] Connecting to {ssid!r}…")

        # Stop hotspot if running
        _nmcli_rw("connection", "down", "Hotspot", timeout=10)
        # Remove stale profile for this SSID if one exists
        _nmcli_rw("connection", "delete", ssid, timeout=10)

        args = [
            "connection", "add",
            "type",                   "wifi",
            "ifname",                 "wlan0",
            "con-name",               ssid,
            "ssid",                   ssid,
            "connection.autoconnect", "yes",
        ]
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]

        rc, out, err = _nmcli_rw(*args, timeout=40)
        if rc != 0:
            msg = err or "Connection profile creation failed"
            print(f"[wifi] connect {ssid!r}: ✗ {msg}")
            if on_done:
                on_done(False, msg)
            return

        rc2, out2, err2 = _nmcli_rw("connection", "up", ssid, timeout=40)
        ok  = rc2 == 0
        msg = out2 or err2 or ("Connected" if ok else "Connection failed")
        print(f"[wifi] connect {ssid!r}: {'✓' if ok else '✗'} {msg}")
        if on_done:
            on_done(ok, msg)

    threading.Thread(target=_run, daemon=True).start()


# ── hotspot ────────────────────────────────────────────────────────────────

def enable_hotspot(password: str, on_done=None, delay: float = 1.5):
    """Switch Pi to hotspot mode. Password is supplied by caller from config."""
    def _run():
        time.sleep(delay)
        print(f"[wifi] Enabling hotspot {HOTSPOT_SSID!r}…")
        rc, out, err = _nmcli_rw(
            "dev", "wifi", "hotspot",
            "ssid",     HOTSPOT_SSID,
            "password", password,
            "ifname",   "wlan0",
            timeout=30,
        )
        ok  = rc == 0
        msg = out or err or ("Hotspot active" if ok else "Hotspot failed")
        print(f"[wifi] hotspot: {'✓' if ok else '✗'} {msg}")
        if on_done:
            on_done(ok, msg)

    threading.Thread(target=_run, daemon=True).start()


def disable_hotspot(on_done=None, delay: float = 1.5):
    """Stop hotspot and reconnect to saved networks."""
    def _run():
        time.sleep(delay)
        print("[wifi] Disabling hotspot…")
        _nmcli_rw("connection", "down", "Hotspot", timeout=10)
        _nmcli_rw("device", "connect", "wlan0", timeout=20)
        if on_done:
            on_done(True, "Hotspot stopped")

    threading.Thread(target=_run, daemon=True).start()


# ── auto-hotspot on boot ───────────────────────────────────────────────────

def auto_hotspot_if_disconnected(wait: int = 30, hotspot_password: str = "soundtouch-pi"):
    """
    Wait up to `wait` seconds for a WiFi connection.
    If still disconnected, enable the setup hotspot automatically.
    """
    def _run():
        for _ in range(wait):
            time.sleep(1)
            status = get_status()
            if status["connected"] or status["mode"] == "hotspot":
                return
        print(f"[wifi] No WiFi after {wait}s — enabling setup hotspot")
        enable_hotspot(password=hotspot_password, delay=0)

    threading.Thread(target=_run, daemon=True).start()
