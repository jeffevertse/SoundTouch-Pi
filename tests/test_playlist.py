"""Playlist (PLS/M3U) resolution + HTTPS downgrade."""
import server
from conftest import fake_getaddrinfo


class _Resp:
    def __init__(self, headers=None, body=b""):
        self.status_code = 200
        self.headers = headers or {}
        self._body = body

    def iter_content(self, chunk_size=1024):
        yield self._body

    def close(self):
        pass


def _patch_fetch(monkeypatch, head_ct, body):
    def fake_safe_fetch(method, url, **kw):
        if method == "HEAD":
            return _Resp(headers={"Content-Type": head_ct})
        return _Resp(headers={"Content-Type": head_ct}, body=body)
    monkeypatch.setattr(server, "_safe_fetch", fake_safe_fetch)
    # _resolve_stream_url validates the input URL first
    monkeypatch.setattr(server.socket, "getaddrinfo", fake_getaddrinfo("1.2.3.4"))


def test_resolve_pls_first_entry(monkeypatch):
    _patch_fetch(monkeypatch, "audio/x-scpls",
                 b"[playlist]\nNumberOfEntries=2\nFile1=http://cdn.example.com/stream\nFile2=http://x/2\n")
    assert server._resolve_stream_url("http://radio.example.com/play") == "http://cdn.example.com/stream"


def test_resolve_m3u_first_url_and_downgrade(monkeypatch):
    _patch_fetch(monkeypatch, "audio/mpegurl",
                 b"#EXTM3U\n#EXTINF:-1,Radio\nhttps://secure.example.com/live\n")
    # https inside the playlist must be downgraded to http for the SoundTouch
    assert server._resolve_stream_url("http://radio.example.com/play.m3u") == "http://secure.example.com/live"


def test_direct_stream_downgraded_unchanged(monkeypatch):
    # Not a playlist (plain audio) → returned as-is but https downgraded.
    def fake_safe_fetch(method, url, **kw):
        return _Resp(headers={"Content-Type": "audio/mpeg"})
    monkeypatch.setattr(server, "_safe_fetch", fake_safe_fetch)
    monkeypatch.setattr(server.socket, "getaddrinfo", fake_getaddrinfo("1.2.3.4"))
    assert server._resolve_stream_url("https://cdn.example.com/a.mp3") == "http://cdn.example.com/a.mp3"
