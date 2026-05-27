"""
UPnP AVTransport controller for SoundTouch 20.

The Bose cloud (TuneIn / INTERNET_RADIO sources) is gone, but the device
is a standard UPnP/DLNA MediaRenderer.  We discover its AVTransport
control URL via SSDP, then push any HTTP stream URL to it with SOAP.

No Bose account, no cloud, no /select — pure DLNA.
"""

import socket
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
import requests


# ── SOAP helpers ───────────────────────────────────────────────────────────

AVT_NS = "urn:schemas-upnp-org:service:AVTransport:1"

def _soap_request(control_url: str, action: str, inner_xml: str) -> requests.Response:
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body>{inner_xml}</s:Body>"
        "</s:Envelope>"
    )
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{AVT_NS}#{action}"',
    }
    return requests.post(control_url, data=body.encode("utf-8"),
                         headers=headers, timeout=8)


# ── UPnP discovery ─────────────────────────────────────────────────────────

def _ssdp_find_location(host: str, timeout: int = 5) -> str | None:
    """
    Multicast M-SEARCH for a MediaRenderer and return the Location URL
    of whichever device lives at `host`.
    """
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 3\r\n"
        "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
        "\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)

    try:
        sock.sendto(msg, ("239.255.255.250", 1900))
        while True:
            try:
                data, addr = sock.recvfrom(65507)
                if addr[0] == host:
                    for line in data.decode("utf-8", errors="ignore").split("\r\n"):
                        if line.lower().startswith("location:"):
                            return line.split(":", 1)[1].strip()
            except socket.timeout:
                break
    finally:
        sock.close()

    return None


def _parse_avt_control_url(description_url: str) -> str | None:
    """
    Fetch the UPnP device description XML and extract the AVTransport
    service controlURL.
    """
    r = requests.get(description_url, timeout=5)
    r.raise_for_status()

    parsed = urlparse(description_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    root = ET.fromstring(r.text)

    # Walk every element regardless of namespace
    service_type_tag = None
    control_url_tag = None

    # Collect all elements into a flat list so we can walk siblings
    all_elems = list(root.iter())

    for i, elem in enumerate(all_elems):
        local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if local == "serviceType" and elem.text and "AVTransport" in elem.text:
            # The controlURL is a sibling — walk forward from the parent
            # Find parent of this element
            for parent in root.iter():
                if elem in list(parent):
                    for child in parent:
                        cl = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                        if cl == "controlURL" and child.text:
                            ctrl = child.text.strip()
                            return base + ctrl if ctrl.startswith("/") else ctrl
    return None


def find_avt_control_url(host: str) -> str:
    """
    Return the AVTransport SOAP control URL for the SoundTouch at `host`.
    Tries SSDP discovery first, then falls back to known SoundTouch paths.
    """
    # 1. Try SSDP to get the exact device description URL
    location = _ssdp_find_location(host)
    if location:
        print(f"[upnp] SSDP found device description: {location}")
        url = _parse_avt_control_url(location)
        if url:
            print(f"[upnp] AVTransport control URL: {url}")
            return url

    # 2. Fallback: try common SoundTouch UPnP ports/paths
    candidates = [
        f"http://{host}:8091/",
        f"http://{host}:8091/DeviceDescription.xml",
        f"http://{host}:8092/DeviceDescription.xml",
    ]
    for url in candidates:
        try:
            r = requests.get(url, timeout=3)
            if r.ok and "AVTransport" in r.text:
                ctrl = _parse_avt_control_url(url)
                if ctrl:
                    print(f"[upnp] AVTransport control URL (fallback): {ctrl}")
                    return ctrl
        except Exception:
            continue

    raise RuntimeError(
        f"Could not find UPnP AVTransport service on {host}. "
        "Check the device is on the same network and UPnP is not blocked."
    )


# ── SOAP fault parsing ─────────────────────────────────────────────────────

def _parse_soap_fault(xml_text: str) -> str:
    """Extract a human-readable message from a SOAP Fault envelope."""
    try:
        root = ET.fromstring(xml_text)
        fault = root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Fault")
        if fault is None:
            # Some devices don't namespace the Fault element
            fault = root.find(".//Fault")
        if fault is not None:
            code   = fault.findtext("faultcode", "")
            string = fault.findtext("faultstring", "")
            # UPnP error code lives inside <detail><UPnPError><errorCode>
            err_code = fault.findtext(".//errorCode", "")
            err_desc = fault.findtext(".//errorDescription", "")
            parts = [p for p in [code, string, err_code, err_desc] if p]
            return " | ".join(parts)
    except Exception:
        pass
    return xml_text[:400]


# ── stream URL normalisation ───────────────────────────────────────────────

def _to_http(url: str) -> str:
    """
    SoundTouch 20 firmware does not support HTTPS streams.
    Downgrade https:// → http:// transparently.
    """
    if url.startswith("https://"):
        return "http://" + url[8:]
    return url


# ── AVTransport actions ────────────────────────────────────────────────────

def avt_set_uri(control_url: str, stream_url: str) -> requests.Response:
    """
    SetAVTransportURI with empty CurrentURIMetaData.

    We intentionally omit DIDL-Lite metadata here.  The SoundTouch 20
    returns s:Client SOAP faults when any metadata is present — empty
    is the most compatible value across all DLNA renderers anyway.
    """
    inner = (
        f'<u:SetAVTransportURI xmlns:u="{AVT_NS}">'
        "<InstanceID>0</InstanceID>"
        f"<CurrentURI>{stream_url}</CurrentURI>"
        "<CurrentURIMetaData></CurrentURIMetaData>"
        "</u:SetAVTransportURI>"
    )
    return _soap_request(control_url, "SetAVTransportURI", inner)


def avt_play(control_url: str) -> requests.Response:
    inner = (
        f'<u:Play xmlns:u="{AVT_NS}">'
        "<InstanceID>0</InstanceID>"
        "<Speed>1</Speed>"
        "</u:Play>"
    )
    return _soap_request(control_url, "Play", inner)


def avt_stop(control_url: str) -> requests.Response:
    inner = (
        f'<u:Stop xmlns:u="{AVT_NS}">'
        "<InstanceID>0</InstanceID>"
        "</u:Stop>"
    )
    return _soap_request(control_url, "Stop", inner)


def avt_get_position(control_url: str) -> dict:
    inner = (
        f'<u:GetPositionInfo xmlns:u="{AVT_NS}">'
        "<InstanceID>0</InstanceID>"
        "</u:GetPositionInfo>"
    )
    r = _soap_request(control_url, "GetPositionInfo", inner)
    return {"status_code": r.status_code, "body": r.text}


# ── High-level player ──────────────────────────────────────────────────────

class UPnPPlayer:
    """
    Stateful UPnP player that caches the control URL after first discovery.
    """

    def __init__(self, host: str):
        self.host = host
        self._control_url: str | None = None

    def _get_url(self) -> str:
        if not self._control_url:
            self._control_url = find_avt_control_url(self.host)
        return self._control_url

    def play_stream(self, stream_url: str, title: str = ""):
        """Set the stream URI then send Play. Raises on failure."""
        stream_url = _to_http(stream_url)   # SoundTouch 20 doesn't support HTTPS
        ctrl = self._get_url()
        print(f"[upnp] SetAVTransportURI  url={stream_url}")
        r = avt_set_uri(ctrl, stream_url)
        if not r.ok:
            raise RuntimeError(
                f"SetAVTransportURI failed: {_parse_soap_fault(r.text)}"
            )
        print(f"[upnp] Play")
        r = avt_play(ctrl)
        if not r.ok:
            raise RuntimeError(f"Play failed: {_parse_soap_fault(r.text)}")

    def stop(self):
        try:
            avt_stop(self._get_url())
        except Exception as e:
            print(f"[upnp] Stop error: {e}")

    def reset(self):
        """Force re-discovery of control URL on next call."""
        self._control_url = None


# ── CLI test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    host = sys.argv[1] if len(sys.argv) > 1 else input("SoundTouch IP: ").strip()
    stream = sys.argv[2] if len(sys.argv) > 2 else "https://stream-relay-geo.ntslive.net/stream"
    title = sys.argv[3] if len(sys.argv) > 3 else "NTS Radio"

    player = UPnPPlayer(host)
    player.play_stream(stream, title)   # title kept for future use; not sent to device
    print("Done — check if the SoundTouch started playing.")
