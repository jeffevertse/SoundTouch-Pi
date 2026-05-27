"""
Discovers a SoundTouch speaker on the local network.
Tries mDNS first (_soundtouch._tcp.local), then SSDP as fallback.
"""

import socket
import time
import struct
import threading
import xml.etree.ElementTree as ET

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_MX = 3
SSDP_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"


def _ssdp_search(timeout=5) -> str | None:
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        f"MAN: \"ssdp:discover\"\r\n"
        f"MX: {SSDP_MX}\r\n"
        f"ST: {SSDP_ST}\r\n"
        "\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)

    try:
        sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
        while True:
            try:
                data, addr = sock.recvfrom(65507)
                response = data.decode("utf-8", errors="ignore")
                if "bose" in response.lower() or "soundtouch" in response.lower():
                    return addr[0]
            except socket.timeout:
                break
    finally:
        sock.close()

    return None


def _mdns_search(timeout=5) -> str | None:
    try:
        from zeroconf import Zeroconf, ServiceBrowser

        found = threading.Event()
        result = {"ip": None}

        class Listener:
            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if info and info.addresses:
                    result["ip"] = socket.inet_ntoa(info.addresses[0])
                    found.set()

            def remove_service(self, *_):
                pass

            def update_service(self, *_):
                pass

        zc = Zeroconf()
        ServiceBrowser(zc, "_soundtouch._tcp.local.", Listener())
        found.wait(timeout)
        zc.close()
        return result["ip"]

    except ImportError:
        return None


def discover(timeout=5, verbose=True) -> str | None:
    """Return the IP address of a SoundTouch speaker, or None if not found."""

    if verbose:
        print("Searching for SoundTouch speaker via mDNS…")
    ip = _mdns_search(timeout)
    if ip:
        if verbose:
            print(f"  Found via mDNS: {ip}")
        return ip

    if verbose:
        print("  Not found via mDNS. Trying SSDP…")
    ip = _ssdp_search(timeout)
    if ip:
        if verbose:
            print(f"  Found via SSDP: {ip}")
        return ip

    if verbose:
        print("  Speaker not found automatically.")
    return None


if __name__ == "__main__":
    ip = discover()
    if ip:
        print(f"\nSoundTouch is at {ip}")
    else:
        print("\nNo SoundTouch found. Is it on the same Wi-Fi network?")
