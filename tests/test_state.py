"""Persistent state defaults + round-trip."""
import state


def test_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_PATH", str(tmp_path / "state.json"))
    d = state.load()
    assert d["last_preset_id"] is None
    assert d["device_source"] is None


def test_patch_roundtrip_and_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_PATH", str(tmp_path / "state.json"))
    state.patch({"last_preset_id": 3})
    state.patch({"now_playing_name": "Jazz24"})
    d = state.load()
    assert d["last_preset_id"] == 3            # earlier key preserved
    assert d["now_playing_name"] == "Jazz24"
    assert "device_source" in d               # defaults still present
