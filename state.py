"""
Persistent state across restarts — survives Pi reboots and SoundTouch power cycles.
Stored in state.json next to this file.
"""

import json
import os
import threading

_PATH = os.path.join(os.path.dirname(__file__), "state.json")
_lock = threading.Lock()

_DEFAULTS: dict = {
    "last_preset_id":   None,   # 1–6
    "now_playing_name": None,   # station name string
    "now_playing_icon": None,   # emoji
    "device_source":    None,   # last known SoundTouch source string
}


def load() -> dict:
    try:
        with open(_PATH) as f:
            return {**_DEFAULTS, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULTS)


def patch(updates: dict) -> dict:
    """Merge `updates` into persistent state and return the new full state."""
    with _lock:
        d = load()
        d.update(updates)
        # Atomic write: flush to a temp file then rename so a power cut
        # mid-write can never leave state.json in a partially-written state.
        tmp = _PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, _PATH)
        return d
