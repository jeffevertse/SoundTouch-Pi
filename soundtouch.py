"""
SoundTouch Web API wrapper.
HTTP on port 8090, WebSocket notifications on port 8080 (protocol: gabbo).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import threading
import time
import requests


class SoundTouchError(Exception):
    pass


class SoundTouch:
    def __init__(self, host: str, port: int = 8090, ws_port: int = 8080):
        self.host = host
        self.port = port
        self.ws_port = ws_port
        self._base = f"http://{host}:{port}"
        self._ws_thread: threading.Thread | None = None
        self._ws_callbacks: list = []
        self._ws_reconnect_callbacks: list = []
        self._ws_running = False

    # ── low-level helpers ──────────────────────────────────────────────────

    def _get(self, path: str) -> ET.Element:
        r = requests.get(f"{self._base}{path}", timeout=5)
        r.raise_for_status()
        return ET.fromstring(r.text)

    def _post(self, path: str, body: str) -> ET.Element:
        r = requests.post(
            f"{self._base}{path}",
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            timeout=5,
        )
        if not r.ok:
            raise SoundTouchError(
                f"Device returned {r.status_code} for {path}: {r.text.strip()}"
            )
        return ET.fromstring(r.text)

    def _key(self, key_value: str):
        """Send a press + release for a key (simulates a button click)."""
        for state in ("press", "release"):
            self._post("/key", f'<key state="{state}" sender="Gabbo">{key_value}</key>')

    # ── device info ────────────────────────────────────────────────────────

    def get_info(self) -> dict:
        root = self._get("/info")
        return {
            "device_id": root.get("deviceID"),
            "name": root.findtext("name"),
            "type": root.findtext("type"),
        }

    def get_sources(self) -> list[dict]:
        root = self._get("/sources")
        return [
            {
                "source": item.get("source"),
                "account": item.get("sourceAccount"),
                "status": item.get("status"),
                "name": item.text,
            }
            for item in root.findall("sourceItem")
        ]

    def get_capabilities(self) -> list[dict]:
        root = self._get("/capabilities")
        return [
            {"name": c.get("name"), "url": c.get("url")}
            for c in root.findall("capability")
        ]

    # ── now playing ────────────────────────────────────────────────────────

    def now_playing(self) -> dict:
        root = self._get("/nowPlaying")
        ci = root.find("ContentItem")
        return {
            "source": root.get("source"),
            "status": root.findtext("playStatus"),
            "station": root.findtext("stationName"),
            "track": root.findtext("track"),
            "artist": root.findtext("artist"),
            "album": root.findtext("album"),
            "description": root.findtext("description"),
            "art": root.findtext("art"),
            "content_item": {
                "source": ci.get("source") if ci is not None else None,
                "location": ci.get("location") if ci is not None else None,
                "name": ci.findtext("itemName") if ci is not None else None,
                "presetable": ci.get("isPresetable") if ci is not None else None,
            } if ci is not None else None,
        }

    # ── volume ─────────────────────────────────────────────────────────────

    def get_volume(self) -> dict:
        root = self._get("/volume")
        return {
            "target": int(root.findtext("targetvolume") or 0),
            "actual": int(root.findtext("actualvolume") or 0),
            "muted": root.findtext("muteenabled") == "true",
        }

    def set_volume(self, level: int):
        level = max(0, min(100, level))
        self._post("/volume", f"<volume>{level}</volume>")

    def volume_up(self):
        self._key("VOLUME_UP")

    def volume_down(self):
        self._key("VOLUME_DOWN")

    def mute(self):
        self._key("MUTE")

    # ── playback controls ──────────────────────────────────────────────────

    def play(self):
        self._key("PLAY")

    def pause(self):
        self._key("PAUSE")

    def play_pause(self):
        self._key("PLAY_PAUSE")

    def stop(self):
        self._key("STOP")

    def power(self):
        self._key("POWER")

    def next_track(self):
        self._key("NEXT_TRACK")

    def prev_track(self):
        self._key("PREV_TRACK")

    # ── hardware presets (recall only — no API write support) ──────────────

    def get_hardware_presets(self) -> list[dict]:
        root = self._get("/presets")
        presets = []
        for p in root.findall("preset"):
            ci = p.find("ContentItem")
            presets.append({
                "id": int(p.get("id")),
                "source": ci.get("source") if ci is not None else None,
                "location": ci.get("location") if ci is not None else None,
                "name": ci.findtext("itemName") if ci is not None else None,
            })
        return presets

    def recall_hardware_preset(self, preset_num: int):
        """Recall one of the 6 hardware presets (1–6) stored on the SoundTouch."""
        if not 1 <= preset_num <= 6:
            raise ValueError("Preset must be 1–6")
        self._key(f"PRESET_{preset_num}")

    def store_preset(self, preset_id: int, source: str, location: str,
                     item_name: str, type_attr: str = "stationurl"):
        """
        Write a content item into a hardware preset slot (undocumented /storePreset).
        Use source="LOCAL_INTERNET_RADIO" with a PLS/M3U URL as location so the
        device fetches and resolves the stream itself — physical buttons then work
        without any WebSocket interception.
        """
        if not 1 <= preset_id <= 6:
            raise ValueError("Preset ID must be 1–6")
        ts = int(time.time())
        body = (
            f'<preset id="{preset_id}" createdOn="{ts}" updatedOn="{ts}">'
            f'<ContentItem source="{source}" type="{type_attr}" '
            f'location="{location}" isPresetable="true">'
            f'<itemName>{item_name}</itemName>'
            f'</ContentItem>'
            f'</preset>'
        )
        self._post("/storePreset", body)

    def remove_preset(self, preset_id: int):
        """Clear a hardware preset slot (undocumented /removePreset)."""
        if not 1 <= preset_id <= 6:
            raise ValueError("Preset ID must be 1–6")
        self._post("/removePreset", f'<preset id="{preset_id}"></preset>')

    # ── select / play content ──────────────────────────────────────────────

    def select(self, source: str, location: str = "", source_account: str = "",
               item_name: str = ""):
        """
        Play a content item.
        Builds the minimal ContentItem XML the device will accept.
          - INTERNET_RADIO / TUNEIN: needs location + optional itemName
          - BLUETOOTH / AUX / PRODUCT: needs only source (+ sourceAccount for AUX)
        """
        attrs = f'source="{source}"'
        if location:
            attrs += f' location="{location}"'
        if source_account:
            attrs += f' sourceAccount="{source_account}"'

        if item_name:
            body = f'<ContentItem {attrs}><itemName>{item_name}</itemName></ContentItem>'
        else:
            body = f'<ContentItem {attrs}></ContentItem>'

        print(f"[select] POST /select  body={body}")
        self._post("/select", body)

    def select_aux(self):
        self._post("/select", '<ContentItem source="AUX" sourceAccount="AUX"></ContentItem>')

    def select_bluetooth(self):
        self._post("/select", '<ContentItem source="BLUETOOTH"></ContentItem>')

    # ── bass ───────────────────────────────────────────────────────────────

    def get_bass_capabilities(self) -> dict:
        root = self._get("/bassCapabilities")
        return {
            "available": root.findtext("bassAvailable") == "true",
            "min": int(root.findtext("bassMin") or 0),
            "max": int(root.findtext("bassMax") or 0),
            "default": int(root.findtext("bassDefault") or 0),
        }

    def get_bass(self) -> int:
        root = self._get("/bass")
        return int(root.findtext("actualbass") or 0)

    def set_bass(self, level: int):
        self._post("/bass", f"<bass>{level}</bass>")

    # ── WebSocket live notifications ───────────────────────────────────────

    def on_update(self, callback):
        """Register a callback(event_type: str, data: dict) for live updates."""
        self._ws_callbacks.append(callback)

    def on_reconnect(self, callback):
        """Register a callback() that fires whenever the WebSocket reconnects."""
        self._ws_reconnect_callbacks.append(callback)

    def start_websocket(self):
        """Start background thread listening to SoundTouch WebSocket notifications."""
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._ws_running = True
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()

    def stop_websocket(self):
        self._ws_running = False

    def _ws_loop(self):
        try:
            import websocket as ws_lib
        except ImportError:
            print("websocket-client not installed — live notifications unavailable")
            return

        url = f"ws://{self.host}:{self.ws_port}/"

        def on_message(ws, message):
            self._handle_ws_message(message)

        def on_error(ws, error):
            print(f"WebSocket error: {error}")

        def on_open(ws):
            for cb in self._ws_reconnect_callbacks:
                try:
                    cb()
                except Exception as e:
                    print(f"Reconnect callback error: {e}")

        def on_close(ws, *_):
            pass  # reconnection is handled by the while loop below

        while self._ws_running:
            try:
                app = ws_lib.WebSocketApp(
                    url,
                    header={"Sec-WebSocket-Protocol": "gabbo"},
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                app.run_forever()
            except Exception as e:
                print(f"WebSocket connection failed: {e}")
            finally:
                # Brief pause before every reconnect attempt (standby or error)
                if self._ws_running:
                    time.sleep(5)

    def _handle_ws_message(self, message: str):
        try:
            root = ET.fromstring(message)
        except ET.ParseError:
            return

        for child in root:
            tag = child.tag
            payload = {}

            if tag == "nowPlayingUpdated":
                np = child.find("nowPlaying")
                if np is not None:
                    ci = np.find("ContentItem")
                    payload = {
                        # source tells us STANDBY vs active vs INVALID_SOURCE
                        "source":  np.get("source") or (ci.get("source") if ci is not None else None),
                        "status":  np.findtext("playStatus"),
                        "station": np.findtext("stationName"),
                        "track":   np.findtext("track"),
                        "artist":  np.findtext("artist"),
                    }

            elif tag == "nowSelectionUpdated":
                # Fires when a physical preset button is pressed on the device.
                # The preset id tells us which button (1–6) was pressed.
                preset_el = child.find("preset")
                if preset_el is not None:
                    try:
                        payload = {"preset_id": int(preset_el.get("id", 0))}
                    except (ValueError, TypeError):
                        payload = {}

            elif tag == "volumeUpdated":
                payload = {}

            elif tag == "connectionStateUpdated":
                payload = {}

            for cb in self._ws_callbacks:
                try:
                    cb(tag, payload)
                except Exception as e:
                    print(f"Callback error: {e}")
