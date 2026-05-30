# SoundTouch Pi Controller

A Raspberry Pi Zero 2 W controller for the Bose SoundTouch 20.

Runs a local web UI accessible from any browser on your home network. Lets you
play internet radio, configure 6 virtual presets, control volume/bass, manage
WiFi, and monitor what's playing in real time — no Bose account or cloud
required.

---

## How it works

| Component | What it does |
|---|---|
| `discovery.py` | Finds the SoundTouch on your network via mDNS or SSDP |
| `soundtouch.py` | Wrapper around the HTTP API (port 8090) and WebSocket (port 8080) |
| `upnp_player.py` | UPnP/DLNA AVTransport controller — pushes streams to the device via SOAP |
| `server.py` | Flask web server running on the Pi at port 5000 |
| `state.py` | Persists playback state across reboots (`state.json`) |
| `wifi_manager.py` | WiFi network switching and setup hotspot via `nmcli` |
| `config.json` | Your 6 preset stations + device settings |
| `gunicorn.conf.py` | Production server config (gthread worker, 8 threads) |
| `templates/index.html` | Mobile-friendly web UI |

**On streaming:** The Bose cloud services (TuneIn, INTERNET_RADIO) are no
longer reliably available. This controller pushes streams directly to the
SoundTouch as a UPnP/DLNA MediaRenderer using AVTransport SOAP — no cloud, no
Bose account. Any public HTTP or HTTPS MP3/AAC stream URL works. HTTPS URLs are
transparently downgraded to HTTP because the SoundTouch 20 firmware does not
support TLS on media streams.

**On presets:** Six virtual presets are stored in `config.json`. Each maps to a
direct stream URL. Playing a preset sends a `SetAVTransportURI` + `Play` SOAP
call to the device. The "Sync to Device Buttons" button writes the presets into
the speaker's six physical hardware slots (via the undocumented `/storePreset`
endpoint), pointing each slot back to the Pi's stream proxy. This means the
physical buttons on the speaker continue to work even without the web UI.

**On auto-resume:** The controller watches for the device powering on (detected
via WebSocket `nowPlayingUpdated` events and reconnect logic) and automatically
resumes the last-played preset. Playback state is persisted to `state.json` so
this survives Pi reboots.

---

## Pi Zero 2 W setup

### 1. Flash Raspberry Pi OS Lite

Use Raspberry Pi Imager. Before writing:
- Set hostname (e.g. `soundtouch`)
- Enable SSH
- Configure your Wi-Fi credentials

### 2. SSH in and install dependencies

```bash
ssh pi@soundtouch.local
sudo apt update && sudo apt install -y python3-pip python3-venv git

git clone https://github.com/YOUR_USERNAME/soundtouch-pi.git
cd soundtouch-pi

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. First run

```bash
python3 server.py
```

On first start, if `config.json` has `"host": null`, the controller searches
the network automatically and saves the IP once found. If auto-discovery fails,
set the IP manually in `config.json`:

```json
"device": {
  "host": "192.168.1.42"
}
```

Access the web UI at **http://soundtouch.local:5000** from any phone or browser
on your home network.

### 4. Run on boot with systemd

```bash
sudo nano /etc/systemd/system/soundtouch.service
```

```ini
[Unit]
Description=SoundTouch Pi Controller
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/home/pi/soundtouch-pi/venv/bin/gunicorn -c /home/pi/soundtouch-pi/gunicorn.conf.py server:app
WorkingDirectory=/home/pi/soundtouch-pi
Restart=always
RestartSec=10
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable soundtouch
sudo systemctl start soundtouch
```

The gunicorn config (`gunicorn.conf.py`) runs a single gthread worker with 8
threads — enough for 6 concurrent audio proxy streams, the SSE event stream,
and API calls, while fitting comfortably in the Pi Zero 2 W's 512 MB RAM.

---

## Configuring your stations

Edit `config.json` directly, or tap the ✏️ button on any preset in the web UI.

### config.json preset format

```json
{
  "id": 1,
  "name": "BBC Radio 4",
  "stream_url": "http://stream.live.vc.bbcmedia.co.uk/bbc_radio_four_fm",
  "icon": "📻"
}
```

Any public HTTP or HTTPS MP3 or AAC stream URL works. PLS and M3U playlist URLs
are also supported — the Pi resolves them to a direct stream URL automatically.

### Finding stream URLs

- [radio-browser.info](https://www.radio-browser.info) — large community
  database of stream URLs
- BBC streams follow the pattern:
  `http://stream.live.vc.bbcmedia.co.uk/bbc_radio_four_fm`
- Direct MP3/AAC URLs from station websites

---

## Physical preset buttons (hardware sync)

The Pi's stream proxy (`GET /api/stream/<id>`) serves each preset as a plain
HTTP audio stream. Tapping **Sync to Device Buttons** in the web UI writes all
six presets into the speaker's hardware slots pointing at these proxy URLs:

```
http://<pi-ip>:5000/api/stream/1  →  Preset 1
http://<pi-ip>:5000/api/stream/2  →  Preset 2
…
```

After syncing, pressing a physical button on the speaker fetches the proxy URL
and starts playing — no Pi app logic required. Re-sync after any preset change.

---

## WiFi management

The web UI includes a collapsible **WiFi Settings** panel that lets you:

- See the current network, signal strength, and IP address
- Scan for nearby networks and connect with a password
- Switch to **setup hotspot** mode (`SoundTouch-Setup` / `soundtouch` / `10.42.0.1`)

**Auto-hotspot:** If the Pi has no WiFi connection 30 seconds after boot, it
automatically creates the setup hotspot so you can reconfigure it from any
device without needing SSH.

WiFi switching uses `nmcli` (NetworkManager CLI), which is the default on
Raspberry Pi OS Bookworm and later.

---

## Optional: Physical GPIO buttons on the Pi

Wire 6 momentary push buttons between GPIO pins and GND. Add a `gpio_buttons`
section to `config.json` mapping GPIO BCM pin numbers to preset IDs:

```json
"gpio_buttons": {
  "17": 1,
  "27": 2,
  "22": 3,
  "5":  4,
  "6":  5,
  "13": 6
}
```

Suggested wiring (Pi Zero 2 W GPIO header):

```
Preset 1 → GPIO 17 (pin 11) → GND (pin 9)
Preset 2 → GPIO 27 (pin 13) → GND (pin 14)
Preset 3 → GPIO 22 (pin 15) → GND (pin 20)
Preset 4 → GPIO 5  (pin 29) → GND (pin 30)
Preset 5 → GPIO 6  (pin 31) → GND (pin 34)
Preset 6 → GPIO 13 (pin 33) → GND (pin 34)
```

Install RPi.GPIO:

```bash
pip install RPi.GPIO
```

Each button press plays that virtual preset via UPnP.

---

## API reference

### Playback

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Now playing + volume |
| GET | `/api/presets` | Virtual presets (config) + hardware presets |
| POST | `/api/preset/:id/play` | Play virtual preset by ID via UPnP |
| POST | `/api/preset/:id/save` | Update virtual preset config |
| POST | `/api/volume` | `{"level": 0–100}` |
| GET | `/api/bass` | Bass level + capabilities |
| POST | `/api/bass` | `{"level": int}` |
| POST | `/api/control/:action` | `play pause play_pause stop power mute volume_up volume_down next prev aux bluetooth` |
| GET | `/api/info` | Device name, type, IP |
| GET | `/api/sources` | Available sources on the speaker |

### Hardware preset sync

| Method | Path | Description |
|---|---|---|
| POST | `/api/sync-hardware-presets` | Write all virtual presets into the 6 hardware slots |
| GET | `/api/stream/:id` | Transparent HTTP audio proxy for a preset stream |

### Real-time events

| Method | Path | Description |
|---|---|---|
| GET | `/api/events` | Server-Sent Events stream (`nowPlaying`, `volume`, `error` events) |

### WiFi

| Method | Path | Description |
|---|---|---|
| GET | `/api/wifi/status` | Current WiFi mode, SSID, signal, IP |
| GET | `/api/wifi/scan` | Scan for nearby networks |
| POST | `/api/wifi/connect` | `{"ssid": "...", "password": "..."}` |
| POST | `/api/wifi/hotspot` | Enable setup hotspot |
| POST | `/api/wifi/hotspot/stop` | Stop hotspot, reconnect to saved network |

### System

| Method | Path | Description |
|---|---|---|
| POST | `/api/system/reboot` | Reboot the Pi (two-tap confirmed in the UI) |
| GET | `/api/debug` | Full device state dump for troubleshooting |

---

## Community resources

- SoundTouch Web API docs: [bosefirmware/SoundTouch-Web-API-Documentation](https://github.com/bosefirmware/SoundTouch-Web-API-Documentation)
- The HTTP API runs on **port 8090** and WebSocket on **port 8080** (protocol: `gabbo`)
- The SoundTouch 20 is a standard UPnP/DLNA MediaRenderer; its AVTransport service is on **port 8091**
