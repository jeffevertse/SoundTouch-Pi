"""
Flask web server for SoundTouch-Pi.
Run with:  python3 server.py
Access at: http://<pi-ip>:5000
"""

import json
import os
import queue
import socket
import subprocess
import threading
import time

import requests as _req
from flask import Flask, Response, jsonify, request, render_template, stream_with_context
from soundtouch import SoundTouch
from upnp_player import UPnPPlayer
from discovery import discover
import state as st
import wifi_manager

_server_port: int = 5000   # updated at startup; used by background threads

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

app = Flask(__name__)

# ── config helpers ─────────────────────────────────────────────────────────

_config_cache: dict | None = None
_config_mtime: float       = 0.0
_config_lock               = threading.Lock()


def load_config() -> dict:
    """
    Return config.json as a dict.  The file is only re-read when it changes
    on disk (mtime check), so repeated calls within a single request are free.
    """
    global _config_cache, _config_mtime
    with _config_lock:
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            mtime = 0.0
        if _config_cache is None or mtime != _config_mtime:
            with open(CONFIG_PATH) as f:
                _config_cache = json.load(f)
            _config_mtime = mtime
        return dict(_config_cache)   # shallow copy — callers must not mutate nested objects


def save_config(cfg: dict):
    global _config_cache, _config_mtime
    with _config_lock:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        # Invalidate cache so the next load_config() sees the new content
        _config_cache = None
        _config_mtime = 0.0

def preset_by_id(preset_id: int) -> dict | None:
    return next((p for p in load_config()["presets"] if p["id"] == preset_id), None)


# ── SSE event bus ──────────────────────────────────────────────────────────
# Browser clients subscribe via GET /api/events.  We push JSON blobs to all
# connected clients whenever the SoundTouch WebSocket delivers an update.

_sse_subscribers: list[queue.Queue] = []
_sse_lock = threading.Lock()


def _sse_subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=30)
    with _sse_lock:
        _sse_subscribers.append(q)
    return q


def _sse_unsubscribe(q: queue.Queue):
    with _sse_lock:
        try:
            _sse_subscribers.remove(q)
        except ValueError:
            pass


def _sse_push(event_type: str, data: dict):
    msg = json.dumps({"type": event_type, "data": data})
    with _sse_lock:
        dead = []
        for q in _sse_subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_subscribers.remove(q)


# ── device singletons ──────────────────────────────────────────────────────

_st:   SoundTouch | None = None
_upnp: UPnPPlayer | None = None
_dev_lock = threading.Lock()


def _resolve_host() -> str:
    cfg = load_config()
    host = cfg["device"].get("host")
    if not host:
        host = discover(timeout=8)
        if host:
            cfg["device"]["host"] = host
            save_config(cfg)
    if not host:
        raise RuntimeError("SoundTouch not found. Set 'host' in config.json manually.")
    return host


def get_device() -> SoundTouch:
    global _st
    with _dev_lock:
        if _st is not None:
            return _st
        host = _resolve_host()
        cfg = load_config()
        _st = SoundTouch(
            host,
            port=cfg["device"].get("port", 8090),
            ws_port=cfg["device"].get("ws_port", 8080),
        )
        _setup_ws_callbacks(_st)
        _st.start_websocket()
        return _st


def get_upnp() -> UPnPPlayer:
    global _upnp
    with _dev_lock:
        if _upnp is None:
            _upnp = UPnPPlayer(_resolve_host())
        return _upnp


# ── auto-resume + physical button handling ─────────────────────────────────
#
# WHY we use state.json instead of an in-memory _prev_source variable:
#
#   When the SoundTouch enters standby its WebSocket server drops the
#   connection.  Our client reconnects.  By that point the in-memory
#   _prev_source might still be "UPNP" (the STANDBY event arrived after
#   the disconnect, or the event never arrived at all).  Reading the
#   persisted "device_source" from state.json is the only reliable way to
#   know the device was in STANDBY across a reconnect or Pi reboot.
#
# WHY we track _last_explicit_play_time:
#
#   The SoundTouch 20 emits transient STANDBY nowPlayingUpdated events
#   during stream initialisation (e.g. after a physical button wakes it
#   from standby and UPnP kicks in).  Without the guard, the STANDBY →
#   UPNP transition looks identical to a genuine power-ON and triggers a
#   spurious auto-resume.  Suppressing for 30 s after any explicit play
#   eliminates the false trigger while leaving genuine power-ON detection
#   intact (user would have to press power within 30 s of an explicit
#   play to ever miss an auto-resume, which is an acceptable edge case).

_last_explicit_play_time: float = 0.0   # updated by _play_preset_id


def _resolve_stream_url(url: str) -> str:
    """
    If url is a PLS or M3U playlist, fetch it and return the first direct
    stream URL inside.  Otherwise return url unchanged.
    Always returns an HTTP URL (downgrades HTTPS for SoundTouch 20 firmware).
    """
    if not url:
        return url
    if url.startswith("https://"):
        url = "http://" + url[8:]

    # Quick check: is this obviously a playlist by extension or content-type?
    lower = url.lower()
    is_playlist = any(lower.endswith(ext) for ext in (".pls", ".m3u", ".m3u8", ".xspf"))
    if not is_playlist:
        try:
            head = _req.head(url, timeout=5, allow_redirects=True)
            ct = head.headers.get("Content-Type", "")
            is_playlist = any(x in ct for x in ("scpls", "mpegurl", "xspf"))
        except Exception:
            pass

    if not is_playlist:
        return url  # Already a direct stream

    # Fetch and parse the playlist
    try:
        r = _req.get(url, timeout=10)
        text = r.text
        # PLS: look for File1=<url>  (or File2, etc.)
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("file") and "=" in line:
                candidate = line.split("=", 1)[1].strip()
                if candidate.startswith("http"):
                    if candidate.startswith("https://"):
                        candidate = "http://" + candidate[8:]
                    print(f"[server] Resolved playlist {url} → {candidate}")
                    return candidate
        # M3U: first non-comment line that is a URL
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line.startswith("http"):
                if line.startswith("https://"):
                    line = "http://" + line[8:]
                print(f"[server] Resolved M3U {url} → {line}")
                return line
    except Exception as e:
        print(f"[server] Playlist resolution failed for {url}: {e}")

    return url  # Fall back to original if parsing fails


def _play_preset_id(preset_id: int) -> bool:
    """
    Play a virtual preset via UPnP.  Safe to call from any thread.
    Returns True on success, False on any failure.
    """
    global _last_explicit_play_time
    preset = preset_by_id(preset_id)
    if not preset:
        print(f"[server] No virtual preset for id={preset_id}")
        return False
    url = _resolve_stream_url(preset.get("stream_url", ""))
    if not url:
        print(f"[server] Preset {preset_id} has no stream URL")
        return False
    try:
        get_upnp().play_stream(url, preset.get("name", ""))
        # Record play time BEFORE patching state so that the nowPlayingUpdated
        # events that follow don't misfire auto-resume.
        _last_explicit_play_time = time.time()
        st.patch({
            "last_preset_id":   preset_id,
            "now_playing_name": preset.get("name"),
            "now_playing_icon": preset.get("icon"),
        })
        _sse_push("nowPlaying", {
            "station":   preset.get("name"),
            "icon":      preset.get("icon"),
            "status":    "PLAY_STATE",
            "preset_id": preset_id,
        })
        print(f"[server] Playing preset {preset_id}: {preset.get('name')}")
        return True
    except Exception as e:
        print(f"[server] Error playing preset {preset_id}: {e}")
        _sse_push("error", {"message": str(e)})
        return False


def _auto_resume():
    """Resume the last played preset on power-on or Pi reboot."""
    time.sleep(3)   # give the device a moment to finish booting
    saved = st.load()
    last_id = saved.get("last_preset_id")
    if not last_id:
        print("[server] Auto-resume: no last preset saved — skipping")
        return
    # Don't double-play if the device is already streaming something
    try:
        np = get_device().now_playing()
        if np.get("status") in ("PLAY_STATE", "BUFFERING_STATE"):
            print("[server] Auto-resume: device already playing — skipping")
            return
    except Exception:
        pass
    print(f"[server] Auto-resume: playing preset {last_id}")
    _play_preset_id(last_id)


def _setup_ws_callbacks(device: SoundTouch):

    def on_update(event_type: str, data: dict):

        # ── physical preset button pressed ──────────────────────────────────
        if event_type == "nowSelectionUpdated":
            pid = data.get("preset_id")
            if not pid:
                return
            print(f"[server] Physical button {pid} pressed")

            def check_and_play(preset_id: int):
                """
                Wait briefly to see if the device starts playing the preset
                natively (via LOCAL_INTERNET_RADIO stored in the hardware slot).
                If it does, just update our state.  If it doesn't (native
                playback failed), fall back to UPnP.
                """
                time.sleep(3)
                preset = preset_by_id(preset_id)
                if not preset:
                    return
                try:
                    np = device.now_playing()
                    if np.get("status") in ("PLAY_STATE", "BUFFERING_STATE"):
                        # Device is playing natively — sync our state to match
                        print(f"[server] Preset {preset_id} playing natively ✓")
                        st.patch({
                            "last_preset_id":   preset_id,
                            "now_playing_name": preset.get("name"),
                            "now_playing_icon": preset.get("icon"),
                        })
                        _sse_push("nowPlaying", {
                            "station":   preset.get("name"),
                            "icon":      preset.get("icon"),
                            "status":    "PLAY_STATE",
                            "preset_id": preset_id,
                        })
                        return
                except Exception as e:
                    print(f"[server] Native-play check error: {e}")

                # Native playback didn't start — push via UPnP
                print(f"[server] Native play timed out for preset {preset_id} — using UPnP")
                _play_preset_id(preset_id)

            threading.Thread(target=check_and_play, args=(pid,), daemon=True).start()

        # ── now-playing changed ─────────────────────────────────────────────
        elif event_type == "nowPlayingUpdated":
            src = data.get("source") or ""

            # Read the PERSISTED last-known source.  This is the reliable signal
            # for detecting STANDBY → active transitions across reconnects/reboots.
            last_src = st.load().get("device_source") or ""

            if last_src == "STANDBY" and src and src != "STANDBY":
                # Device came out of standby.  Only auto-resume if no explicit
                # play happened recently — the SoundTouch 20 emits transient
                # STANDBY events during stream init (e.g. button-press wake-up)
                # which would otherwise look identical to a genuine power-ON.
                elapsed = time.time() - _last_explicit_play_time
                if elapsed > 30:
                    print(f"[server] Power-ON detected ({last_src!r} → {src!r}) — auto-resume")
                    threading.Thread(target=_auto_resume, daemon=True).start()
                else:
                    print(f"[server] Transient STANDBY→{src!r} suppressed "
                          f"(explicit play {elapsed:.0f}s ago)")

            # Persist new source AFTER the transition check above
            st.patch({"device_source": src})

            # Inject our known station name/icon into the SSE payload
            saved = st.load()
            if saved.get("now_playing_name") and src not in ("STANDBY", ""):
                data = {**data,
                        "station": saved["now_playing_name"],
                        "icon":    saved.get("now_playing_icon")}

            _sse_push("nowPlaying", data)

        # ── volume changed (physical knob or app) ───────────────────────────
        elif event_type == "volumeUpdated":
            try:
                vol = device.get_volume()
                _sse_push("volume", vol)
            except Exception:
                pass

    def on_reconnect():
        """
        WebSocket reconnected.  Could be:
          A) Pi just booted while device was playing  → auto-resume
          B) Device just powered ON from standby      → auto-resume
          C) Device is still in standby after a drop  → do NOT resume
          D) Brief network blip, device still playing → do NOT resume
        """
        print("[server] WebSocket reconnected — checking playback state")
        time.sleep(5)   # let the device settle before polling
        try:
            np = device.now_playing()
            src    = np.get("source", "") or ""
            status = np.get("status",  "") or ""

            # The persisted source is the ground truth for what the device
            # was doing BEFORE this reconnect (survives across Pi reboots).
            last_src = st.load().get("device_source") or ""

            if src == "STANDBY":
                # Device is in standby right now — user turned it off.
                # Persist STANDBY so the power-ON detection in on_update works.
                st.patch({"device_source": "STANDBY"})
                print("[server] Reconnect: device in standby — no auto-resume")

            elif status in ("PLAY_STATE", "BUFFERING_STATE"):
                # Already playing (e.g., brief blip) — nothing to do
                print(f"[server] Reconnect: already playing ({src}) — no auto-resume")

            elif last_src == "STANDBY" and src and src not in ("STANDBY", "INVALID_SOURCE"):
                # Device was in standby and has since come back on — resume
                print(f"[server] Reconnect: woke from standby ({src}) — auto-resume")
                threading.Thread(target=_auto_resume, daemon=True).start()

            else:
                # Nothing playing and not in standby → likely Pi reboot → resume
                print(f"[server] Reconnect: idle (src={src!r}, last={last_src!r}) — auto-resume")
                threading.Thread(target=_auto_resume, daemon=True).start()

        except Exception as e:
            print(f"[server] Reconnect check failed: {e}")

    device.on_update(on_update)
    device.on_reconnect(on_reconnect)


# ── SSE endpoint ───────────────────────────────────────────────────────────

@app.get("/api/events")
def api_events():
    """
    Server-Sent Events stream.  Browser connects once; we push JSON blobs
    whenever the SoundTouch WebSocket delivers volume, now-playing, or
    error events.
    """
    q = _sse_subscribe()

    def generate():
        try:
            # Send an immediate heartbeat so the browser knows we're live
            yield "data: {\"type\":\"connected\"}\n\n"
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    # Keepalive comment to prevent proxies from closing the connection
                    yield ": keepalive\n\n"
        finally:
            _sse_unsubscribe(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable Nginx buffering if behind a proxy
        },
    )


# ── API routes ─────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    try:
        device = get_device()
        np  = device.now_playing()
        vol = device.get_volume()

        # Overlay our station name/icon when the device is playing a UPnP stream
        saved = st.load()
        if saved.get("now_playing_name"):
            src = np.get("source", "")
            if src not in ("STANDBY", "AUX", "BLUETOOTH", "AIRPLAY", ""):
                np["station"] = saved["now_playing_name"]
                np["icon"]    = saved.get("now_playing_icon")

        return jsonify({"ok": True, "now_playing": np, "volume": vol})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.get("/api/presets")
def api_presets():
    cfg = load_config()
    hw = []
    try:
        hw = get_device().get_hardware_presets()
    except Exception:
        pass
    return jsonify({"virtual": cfg["presets"], "hardware": hw})


@app.post("/api/preset/<int:preset_id>/play")
def api_play_preset(preset_id: int):
    preset = preset_by_id(preset_id)
    if not preset:
        return jsonify({"ok": False, "error": "Preset not found"}), 404
    if not preset.get("stream_url"):
        return jsonify({"ok": False, "error": "Preset has no stream URL configured"}), 400
    # Delegate to _play_preset_id so _last_explicit_play_time is always set.
    # Without this guard, playing from the UI could trigger a spurious auto-resume
    # if the SoundTouch emits a transient STANDBY during stream initialisation.
    ok = _play_preset_id(preset_id)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Stream failed — check server logs"}), 503


@app.post("/api/preset/<int:preset_id>/save")
def api_save_preset(preset_id: int):
    if not 1 <= preset_id <= 6:
        return jsonify({"ok": False, "error": "Preset ID must be 1–6"}), 400
    data = request.get_json()
    cfg = load_config()
    for preset in cfg["presets"]:
        if preset["id"] == preset_id:
            preset["name"]       = data.get("name", preset["name"])
            preset["stream_url"] = data.get("stream_url", preset.get("stream_url", ""))
            preset["icon"]       = data.get("icon", preset.get("icon", "📻"))
            break
    else:
        cfg["presets"].append({
            "id":         preset_id,
            "name":       data.get("name", f"Preset {preset_id}"),
            "stream_url": data.get("stream_url", ""),
            "icon":       data.get("icon", "📻"),
        })
    save_config(cfg)
    return jsonify({"ok": True})


@app.post("/api/volume")
def api_set_volume():
    data = request.get_json()
    level = data.get("level")
    if level is None:
        return jsonify({"ok": False, "error": "Missing 'level'"}), 400
    try:
        get_device().set_volume(int(level))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.post("/api/control/<action>")
def api_control(action: str):
    actions = {
        "play":       lambda d: d.play(),
        "pause":      lambda d: d.pause(),
        "play_pause": lambda d: d.play_pause(),
        "stop":       lambda d: d.stop(),
        "power":      lambda d: d.power(),
        "mute":       lambda d: d.mute(),
        "volume_up":  lambda d: d.volume_up(),
        "volume_down":lambda d: d.volume_down(),
        "next":       lambda d: d.next_track(),
        "prev":       lambda d: d.prev_track(),
        "aux":        lambda d: d.select_aux(),
        "bluetooth":  lambda d: d.select_bluetooth(),
    }
    fn = actions.get(action)
    if not fn:
        return jsonify({"ok": False, "error": "Unknown action"}), 400
    try:
        fn(get_device())
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.get("/api/info")
def api_info():
    try:
        d = get_device()
        return jsonify({"ok": True, "info": d.get_info(), "host": d.host})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.get("/api/sources")
def api_sources():
    try:
        return jsonify({"ok": True, "sources": get_device().get_sources()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.get("/api/bass")
def api_get_bass():
    try:
        d = get_device()
        caps  = d.get_bass_capabilities()
        level = d.get_bass() if caps["available"] else None
        return jsonify({"ok": True, "level": level, "caps": caps})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.post("/api/bass")
def api_set_bass():
    data = request.get_json()
    try:
        get_device().set_bass(int(data["level"]))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


# ── stream proxy ──────────────────────────────────────────────────────────

@app.get("/api/stream/<int:preset_id>")
def api_stream_proxy(preset_id: int):
    """
    Transparent HTTP audio proxy for a preset stream.

    The SoundTouch fetches this URL when a hardware preset stored with
    source=LOCAL_INTERNET_RADIO is recalled.  Routing through the Pi means:
      • HTTPS streams are transparently downgraded (SoundTouch 20 firmware
        does not support TLS on media streams)
      • The preset location URL never changes even if the upstream moves
      • ICY metadata is forwarded so the device gets station/track info

    Flask is run with threaded=True so this long-lived connection does not
    block other API calls.
    """
    preset = preset_by_id(preset_id)
    if not preset:
        return "Preset not found", 404
    stream_url = _resolve_stream_url(preset.get("stream_url", ""))
    if not stream_url:
        return "No stream URL configured", 404

    try:
        upstream = _req.get(
            stream_url,
            stream=True,
            timeout=15,
            headers={
                "User-Agent":   "SoundTouch/1.0",
                "Icy-MetaData": "1",
            },
        )
        content_type = upstream.headers.get("Content-Type", "audio/mpeg")

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        resp_headers = {"Content-Type": content_type}
        for h, v in upstream.headers.items():
            if h.lower().startswith("icy-"):
                resp_headers[h] = v

        return Response(
            stream_with_context(generate()),
            headers=resp_headers,
        )
    except Exception as e:
        print(f"[proxy] Stream error for preset {preset_id}: {e}")
        return f"Stream error: {e}", 502


def _resync_hardware_presets():
    """
    Write all virtual presets into the SoundTouch hardware slots.
    Safe to call from a background thread (no Flask request context needed).
    Uses the Pi's stream proxy URL so the device always has a reachable,
    HTTP endpoint regardless of upstream stream format.
    """
    try:
        device = get_device()
        pi_ip  = _get_pi_ip()
        cfg    = load_config()
        for preset in cfg["presets"]:
            pid        = preset["id"]
            name       = preset.get("name", f"Preset {pid}")
            stream_url = preset.get("stream_url", "")
            if not stream_url:
                continue
            proxy_url = f"http://{pi_ip}:{_server_port}/api/stream/{pid}"
            try:
                device.store_preset(
                    preset_id=pid,
                    source="LOCAL_INTERNET_RADIO",
                    location=proxy_url,
                    item_name=name,
                    type_attr="stationurl",
                )
                print(f"[server] Hardware preset {pid} → {proxy_url}")
            except Exception as e:
                print(f"[server] store_preset {pid} failed: {e}")
    except Exception as e:
        print(f"[server] _resync_hardware_presets failed: {e}")


# ── hardware preset sync ───────────────────────────────────────────────────

def _get_pi_ip() -> str:
    """
    Return the Pi's LAN IP — the address the SoundTouch can reach back to.

    Uses the UDP-connect trick: connecting a UDP socket sets its source
    address via the kernel routing table without sending any packets, so
    this works even when the remote host is unreachable.
    """
    # Primary: use the route to the SoundTouch so we pick the right interface
    # if the Pi has multiple network interfaces (e.g. eth0 + wlan0).
    try:
        host = _resolve_host()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    # Fallback: any routable interface via the default gateway.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"   # last resort — hardware preset sync will fail visibly



@app.post("/api/sync-hardware-presets")
def api_sync_hardware_presets():
    """
    Write all configured virtual presets into the SoundTouch hardware preset
    slots using the undocumented /storePreset endpoint.

    Each preset is stored as LOCAL_INTERNET_RADIO with a PLS URL pointing back
    to this Pi server.  When the user presses a physical button the device
    fetches the PLS, resolves the real stream URL, and plays it — no WebSocket
    interception needed.
    """
    try:
        device = get_device()
        pi_ip  = _get_pi_ip()
        cfg    = load_config()
        results = []
        for preset in cfg["presets"]:
            pid        = preset["id"]
            name       = preset.get("name", f"Preset {pid}")
            stream_url = preset.get("stream_url", "")
            if not stream_url:
                results.append({"id": pid, "ok": False, "reason": "no stream_url"})
                continue
            # Use the Pi stream proxy — always HTTP, handles redirects,
            # works even when the real stream is HTTPS
            proxy_url = f"http://{pi_ip}:{_server_port}/api/stream/{pid}"
            try:
                device.store_preset(
                    preset_id=pid,
                    source="LOCAL_INTERNET_RADIO",
                    location=proxy_url,
                    item_name=name,
                    type_attr="stationurl",
                )
                results.append({"id": pid, "ok": True, "proxy_url": proxy_url})
                print(f"[server] Stored hardware preset {pid}: {name} → {proxy_url}")
            except Exception as e:
                results.append({"id": pid, "ok": False, "reason": str(e)})
                print(f"[server] Failed to store preset {pid}: {e}")
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


# ── Device reconnect ──────────────────────────────────────────────────────

def _reset_device():
    """Stop the current device WebSocket and clear both singletons."""
    global _st, _upnp
    with _dev_lock:
        if _st is not None:
            try:
                _st.stop_websocket()
            except Exception:
                pass
            _st = None
        _upnp = None


@app.post("/api/device/reconnect")
def api_device_reconnect():
    """
    Rediscover the SoundTouch device (e.g. after a factory reset or IP change).

    Accepts an optional JSON body {"host": "x.x.x.x"} to skip auto-discovery
    and connect directly to a known IP.  Without a body (or with host omitted /
    null) the server runs mDNS + SSDP discovery to locate the device.

    On success: updates config.json, tears down the stale singleton, creates a
    new connection, and kicks off a hardware preset resync in the background.
    """
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip() or None

    if not host:
        host = discover(timeout=10)
    if not host:
        return jsonify({
            "ok": False,
            "error": "Device not found on the network. Enter the IP address manually.",
        }), 404

    # Validate: actually reach the device before saving the new host
    from soundtouch import SoundTouch as _ST
    try:
        probe = _ST(host, port=8090)
        probe.get_info()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Found {host} but could not connect: {e}"}), 503

    # Persist new host
    cfg = load_config()
    cfg["device"]["host"] = host
    save_config(cfg)

    # Drop old singleton → next get_device() builds a fresh one
    _reset_device()

    try:
        get_device()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Reconnected but setup failed: {e}"}), 503

    threading.Thread(target=_resync_hardware_presets, daemon=True).start()
    return jsonify({"ok": True, "host": host, "resyncing": True})


# ── System ─────────────────────────────────────────────────────────────────

@app.post("/api/system/reboot")
def api_system_reboot():
    """
    Reboot the Pi.  Fires the actual reboot in a background thread after a
    short delay so this HTTP response is delivered to the browser first.
    """
    def _reboot():
        time.sleep(3.0)   # give gunicorn time to flush the HTTP response
        print("[server] System reboot requested via web UI")
        subprocess.run(["sudo", "shutdown", "-r", "now"])

    threading.Thread(target=_reboot, daemon=True).start()
    return jsonify({
        "ok": True,
        "message": (
            "Rebooting…\n"
            "The controller will be back in about 30 seconds.\n"
            "Reconnect and visit http://soundtouch-pi.local:5000"
        ),
    })


# ── WiFi management ────────────────────────────────────────────────────────

@app.get("/api/wifi/status")
def api_wifi_status():
    try:
        return jsonify({"ok": True, **wifi_manager.get_status()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.get("/api/wifi/scan")
def api_wifi_scan():
    try:
        networks = wifi_manager.scan_networks()
        return jsonify({"ok": True, "networks": networks})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.post("/api/wifi/connect")
def api_wifi_connect():
    """
    Start a WiFi connection attempt in the background.
    Returns immediately — the Pi will switch networks after a short delay
    so this HTTP response can reach the browser first.
    """
    data = request.get_json()
    ssid = (data.get("ssid") or "").strip()
    password = data.get("password", "")
    if not ssid:
        return jsonify({"ok": False, "error": "SSID required"}), 400
    # delay=2.0 gives the HTTP response time to be delivered before
    # the Pi drops the current network connection.
    wifi_manager.connect_wifi(ssid, password, delay=2.0)
    return jsonify({
        "ok": True,
        "message": (
            f"Connecting to \"{ssid}\"…\n"
            "The Pi will switch networks in a moment.\n"
            "Reconnect your phone/laptop to the new WiFi, then visit:\n"
            "http://soundtouch-pi.local:5000"
        ),
    })


@app.post("/api/wifi/hotspot")
def api_wifi_hotspot():
    """Enable the setup hotspot (SoundTouch-Setup / soundtouch / 10.42.0.1)."""
    wifi_manager.enable_hotspot(delay=2.0)
    return jsonify({
        "ok": True,
        "message": (
            f"Starting hotspot \"{wifi_manager.HOTSPOT_SSID}\"…\n"
            f"Connect your phone/laptop to that network\n"
            f"(password: {wifi_manager.HOTSPOT_PASSWORD}), then visit:\n"
            f"http://{wifi_manager.HOTSPOT_IP}:5000"
        ),
    })


@app.post("/api/wifi/hotspot/stop")
def api_wifi_hotspot_stop():
    """Stop the hotspot and reconnect to any saved network."""
    wifi_manager.disable_hotspot(delay=2.0)
    return jsonify({
        "ok": True,
        "message": (
            "Stopping hotspot…\n"
            "Reconnect to your home WiFi, then visit:\n"
            "http://soundtouch-pi.local:5000"
        ),
    })


# ── debug ──────────────────────────────────────────────────────────────────

@app.get("/api/debug")
def api_debug():
    d = get_device()
    out = {}
    for key, fn in {
        "info":         d.get_info,
        "sources":      d.get_sources,
        "hw_presets":   d.get_hardware_presets,
        "now_playing":  d.now_playing,
        "capabilities": d.get_capabilities,
        "volume":       d.get_volume,
    }.items():
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = {"error": str(e)}
    out["state"] = st.load()
    return jsonify(out)


@app.post("/api/debug/raw-select")
def api_debug_raw_select():
    data = request.get_json()
    xml = data.get("xml", "")
    if not xml:
        return jsonify({"ok": False, "error": "Provide 'xml' field"}), 400
    try:
        import requests as req
        d = get_device()
        r = req.post(
            f"http://{d.host}:{d.port}/select",
            data=xml.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            timeout=5,
        )
        return jsonify({"status_code": r.status_code, "response": r.text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


# ── UI ─────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html")


# ── GPIO optional ──────────────────────────────────────────────────────────

def setup_gpio(cfg: dict):
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        button_pins = cfg.get("gpio_buttons", {})
        for pin_str, preset_id in button_pins.items():
            pin = int(pin_str)
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

            def make_cb(pid):
                def cb(channel):
                    threading.Thread(
                        target=_play_preset_id, args=(pid,), daemon=True
                    ).start()
                return cb

            GPIO.add_event_detect(pin, GPIO.FALLING,
                                  callback=make_cb(preset_id), bouncetime=300)
        print(f"GPIO buttons configured: {button_pins}")
    except ImportError:
        pass
    except Exception as e:
        print(f"GPIO setup error: {e}")


# ── startup ────────────────────────────────────────────────────────────────

def warmup():
    try:
        d    = get_device()
        info = d.get_info()
        print(f"Connected to: {info.get('name')} ({info.get('type')}) at {d.host}")
        # Write all presets into the hardware slots so physical buttons work
        # immediately after (re)start without needing a manual sync
        time.sleep(5)   # let the device finish its own boot tasks
        _resync_hardware_presets()
    except Exception as e:
        print(f"Could not connect at startup: {e}")


def _startup(port: int = 5000):
    """
    Initialise background tasks.

    Called by gunicorn's post_worker_init hook (see gunicorn.conf.py) when
    running in production, or directly from __main__ during development.
    Daemon threads must be started here rather than at module import time
    because forking (gunicorn) kills threads that ran in the parent process.
    """
    global _server_port
    _server_port = port
    cfg = load_config()
    if cfg.get("gpio_buttons"):
        setup_gpio(cfg)
    threading.Thread(target=warmup, daemon=True).start()
    # If the Pi has no WiFi after 30 s, enable the setup hotspot automatically
    # so the user can reconfigure via http://10.42.0.1:5000
    wifi_manager.auto_hotspot_if_disconnected(wait=30)
    print(f"[server] Background tasks started (port={_server_port})")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    _startup(args.port)
    print(f"SoundTouch-Pi running at http://0.0.0.0:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)
