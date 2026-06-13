"""Auto-resume guards: don't wake a device the user turned off."""
import time
import server


class _Device:
    def __init__(self, source, status):
        self._np = {"source": source, "status": status}

    def now_playing(self):
        return self._np


def _setup(monkeypatch, device=None, last_off=0.0):
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(server.st, "load", lambda: {"last_preset_id": 2})
    server._last_explicit_off_time = last_off
    played = []
    monkeypatch.setattr(server, "_play_preset_id", lambda pid: played.append(pid))
    if device is not None:
        monkeypatch.setattr(server, "get_device", lambda: device)
    return played


def test_suppressed_after_explicit_off(monkeypatch):
    played = _setup(monkeypatch, last_off=time.time())
    server._auto_resume()
    assert played == []                       # recent power-off → stay off


def test_skips_when_device_in_standby(monkeypatch):
    played = _setup(monkeypatch, device=_Device("STANDBY", "STANDBY"))
    server._auto_resume()
    assert played == []                       # never wake a sleeping device


def test_skips_when_already_playing(monkeypatch):
    played = _setup(monkeypatch, device=_Device("UPNP", "PLAY_STATE"))
    server._auto_resume()
    assert played == []


def test_resumes_on_genuine_power_on(monkeypatch):
    played = _setup(monkeypatch, device=_Device("UPNP", "STOP_STATE"))
    server._auto_resume()
    assert played == [2]                       # idle + on + no recent off → resume
