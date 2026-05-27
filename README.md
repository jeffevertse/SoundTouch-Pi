# SoundTouch Pi Controller

A Raspberry Pi Zero 2 W controller for the Bose SoundTouch 20.

Runs a local web UI accessible from any browser on your home network. Lets you
play internet radio, configure 6 virtual presets, control volume/bass, and
monitor what's playing in real time.

---

## How it works

| Component | What it does |
|---|---|
| `discovery.py` | Finds the SoundTouch on your network via mDNS or SSDP |
| `soundtouch.py` | Clean wrapper around the HTTP API (port 8090) and WebSocket (port 8080) |
| `server.py` | Flask web server running on the Pi at port 5000 |
| `config.json` | Your 6 preset stations + device settings |
| `templates/index.html` | Mobile-friendly web UI |

**On presets:** The SoundTouch Web API does not expose a write endpoint for
hardware presets — you can only read them (`GET /presets`) and recall them
(`POST /key PRESET_1`…`PRESET_6`). This controller maintains its own 6
_virtual presets_ in `config.json`, each mapped to an internet radio station,
and triggers them via the `/select` API. The speaker's physical preset buttons
continue to work independently.

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

git clone https://github.com/YOUR_USERNAME/soundtouch-pi.git   # or scp the folder
cd soundtouch-pi

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. First run

```bash
python3 server.py
```

On first start, if `config.json` has `"host": null`, the controller will search
the network automatically and save the IP once found. If auto-discovery fails,
set the IP manually in `config.json`:

```json
"device": {
  "host": "192.168.1.42",
  ...
}
```

Access the web UI at **http://soundtouch.local:5000** (or the Pi's IP:5000)
from any phone or browser on your home network.

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
ExecStart=/home/pi/soundtouch-pi/venv/bin/python3 /home/pi/soundtouch-pi/server.py
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

---

## Configuring your stations

Edit `config.json` directly, or use the ✏️ button on any preset in the web UI.

### Internet radio stream URLs

Any public HTTP/HTTPS MP3 or AAC stream works:

```json
{
  "id": 1,
  "name": "KEXP Seattle",
  "source": "INTERNET_RADIO",
  "location": "https://kexp-mp3-128.streamguys1.com/kexp128.mp3",
  "source_account": "",
  "icon": "🌲"
}
```

### TuneIn stations

Find the station on [tunein.com](https://tunein.com). The URL looks like:
`tunein.com/radio/BBC-Radio-4-p15` — the station ID is in the page source
(`s25419` for BBC Radio 4).

```json
{
  "id": 2,
  "name": "BBC Radio 4",
  "source": "TUNEIN",
  "location": "/v1/playback/station/s25419",
  "source_account": "",
  "icon": "📻"
}
```

> **Note:** TuneIn integration depends on SoundTouch firmware. Direct stream URLs
> (`INTERNET_RADIO`) are more reliable since they don't require any cloud service.

---

## Optional: Physical buttons on the Pi (GPIO)

Wire 6 momentary push buttons between GPIO pins and GND. Then add a
`gpio_buttons` section to `config.json` mapping GPIO BCM pin numbers to preset IDs:

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

Install the RPi.GPIO library:
```bash
pip install RPi.GPIO
```

Each button press plays that virtual preset on the SoundTouch.

---

## API reference (server endpoints)

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Now playing + volume |
| GET | `/api/presets` | Virtual presets (config) + hardware presets |
| POST | `/api/preset/:id/play` | Play virtual preset by ID |
| POST | `/api/preset/:id/save` | Update virtual preset config |
| POST | `/api/volume` | `{"level": 0–100}` |
| GET | `/api/bass` | Bass level + capabilities |
| POST | `/api/bass` | `{"level": int}` |
| POST | `/api/control/:action` | `play pause play_pause stop power mute volume_up volume_down next prev aux bluetooth` |
| GET | `/api/info` | Device name, type, IP |
| GET | `/api/sources` | Available sources on the speaker |

---

## Community resources

- SoundTouch Web API docs: [bosefirmware/SoundTouch-Web-API-Documentation](https://github.com/bosefirmware/SoundTouch-Web-API-Documentation)
- The API runs on **port 8090** (HTTP) and **port 8080** (WebSocket, protocol: `gabbo`)
