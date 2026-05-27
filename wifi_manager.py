"""
WiFi management via nmcli (NetworkManager CLI).
Works on Raspberry Pi OS Bookworm+ which uses NetworkManager by default.

Key flows
---------
Client mode  — Pi is connected to a normal WiFi network.
Hotspot mode — Pi creates "SoundTouch-Setup" (10.42.0.1).  User connects to it
               from a phone or laptop, visits http://soundtouch.local:5000 (or
               http://10.42.0.1:5000) and configures the target network.
Auto-hotspot — called by server.py startup if Pi has no WiFi after 30 s.
"""

import subprocess
import threading
import time

HOTSPOT_SSID     = "SoundTouch-Setup"
HOTSPOT_PASSWORD = "soundtouch"
HOTSPOT_IP       = "10.42.0.1"


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
            # NAME contains "Hotspot" for nmcli-created hotspots
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
    on_done(ok: bool, message: str) is called on completion (optional).
    delay lets the HTTP response be sent before the network switch happens.
    """
    def _run():
        time.sleep(delay)
        print(f"[wifi] Connecting to {ssid!r}…")

        # Stop hotspot if running
        _nmcli_rw("connection", "down", "Hotspot", timeout=10)

        args = ["dev", "wifi", "connect", ssid, "ifname", "wlan0"]
        if password:
            args += ["password", password]

        rc, out, err = _nmcli_rw(*args, timeout=40)
        ok  = rc == 0 and "successfully activated" in (out + err).lower()
        msg = out or err or ("Connected" if ok else "Connection failed")
        print(f"[wifi] connect {ssid!r}: {'✓' if ok else '✗'} {msg}")
        if on_done:
            on_done(ok, msg)

    threading.Thread(target=_run, daemon=True).start()


# ── hotspot ────────────────────────────────────────────────────────────────

def enable_hotspot(on_done=None, delay: float = 1.5):
    """
    Switch Pi to hotspot mode.
    SSID: SoundTouch-Setup  |  Password: soundtouch  |  IP: 10.42.0.1
    """
    def _run():
        time.sleep(delay)
        print(f"[wifi] Enabling hotspot {HOTSPOT_SSID!r}…")
        rc, out, err = _nmcli_rw(
            "dev", "wifi", "hotspot",
            "ssid",     HOTSPOT_SSID,
            "password", HOTSPOT_PASSWORD,
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

def auto_hotspot_if_disconnected(wait: int = 30):
    """
    Wait up to `wait` seconds for a WiFi connection.
    If still disconnected, enable the setup hotspot automatically.
    Call once from server startup in a background thread.
    """
    def _run():
        for _ in range(wait):
            time.sleep(1)
            status = get_status()
            if status["connected"] or status["mode"] == "hotspot":
                return  # Already connected or already in hotspot
        # Still no connection
        print(f"[wifi] No WiFi after {wait}s — enabling setup hotspot")
        enable_hotspot(delay=0)

    threading.Thread(target=_run, daemon=True).start()
