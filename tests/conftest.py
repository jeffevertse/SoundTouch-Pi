"""Make the repo root importable so tests can `import server` / `import state`."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def fake_getaddrinfo(*addrs):
    """Return a socket.getaddrinfo stub that resolves a host to the given IPs."""
    def _gai(host, port=None, *a, **k):
        return [(2, 1, 6, "", (addr, 0)) for addr in addrs]
    return _gai
