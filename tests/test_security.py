"""SSRF / URL-validation hardening."""
import pytest
import server
from conftest import fake_getaddrinfo


def test_validate_accepts_public(monkeypatch):
    monkeypatch.setattr(server.socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))
    server._validate_stream_url("http://example.com/s.mp3")   # must not raise


@pytest.mark.parametrize("url", [
    "ftp://example.com/x",
    "file:///etc/passwd",
    "gopher://example.com/",
    "http:///nohost",
])
def test_validate_rejects_bad_scheme_or_host(url):
    with pytest.raises(ValueError):
        server._validate_stream_url(url)


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "192.168.1.5", "172.16.0.1",
    "169.254.169.254",   # cloud metadata
    "0.0.0.0",
])
def test_validate_rejects_private(monkeypatch, ip):
    monkeypatch.setattr(server.socket, "getaddrinfo", fake_getaddrinfo(ip))
    with pytest.raises(ValueError):
        server._validate_stream_url("http://evil.test/x")


def test_resolve_public_ip_returns_first_public(monkeypatch):
    monkeypatch.setattr(server.socket, "getaddrinfo", fake_getaddrinfo("8.8.8.8"))
    assert server._resolve_public_ip("dns.test") == "8.8.8.8"


def test_resolve_public_ip_rejects_any_private(monkeypatch):
    # A host that resolves to BOTH a public and a private address is rejected —
    # this is the core anti-DNS-rebinding behaviour.
    monkeypatch.setattr(server.socket, "getaddrinfo", fake_getaddrinfo("8.8.8.8", "192.168.1.9"))
    with pytest.raises(ValueError):
        server._resolve_public_ip("rebind.test")


def test_safe_fetch_pins_ip_downgrades_and_preserves_host(monkeypatch):
    monkeypatch.setattr(server.socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))
    captured = {}

    def fake_request(method, url, **kw):
        captured.update(method=method, url=url,
                        headers=kw.get("headers"), allow_redirects=kw.get("allow_redirects"))
        return object()

    monkeypatch.setattr(server._req, "request", fake_request)
    server._safe_fetch("GET", "https://example.com:8443/path?q=1", timeout=5)

    assert captured["method"] == "GET"
    assert captured["url"] == "http://93.184.216.34:8443/path?q=1"   # https→http, IP pinned, port kept
    assert captured["headers"]["Host"] == "example.com:8443"          # original host preserved
    assert captured["allow_redirects"] is False                      # caller handles redirects


def test_safe_fetch_blocks_private(monkeypatch):
    monkeypatch.setattr(server.socket, "getaddrinfo", fake_getaddrinfo("10.0.0.5"))
    with pytest.raises(ValueError):
        server._safe_fetch("GET", "http://internal.test/x")
