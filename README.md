# XMEye / Sofia — Home Assistant Integration

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=equake&repository=hass-xmeye&category=integration)

[![HA Version](https://img.shields.io/badge/Home%20Assistant-2026.3%2B-blue)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A full-featured Home Assistant integration for **XMEye / Sofia / DVRIP** cameras, DVRs and NVRs — the same protocol used by millions of Xiongmai-based devices worldwide.

---

## What is XMEye / Sofia?

**Xiongmai Technology** (雄迈科技) is one of the world's largest manufacturers of CCTV hardware. They produce the OEM boards and firmware found inside a huge share of budget IP cameras, DVRs and NVRs sold globally under hundreds of different brand names — Annke, Sannce, Zosi, Floureon, Reolink (older models), and countless others.

The **Sofia firmware** is Xiongmai's embedded operating system. It exposes a proprietary control protocol called **DVRIP** (Digital Video Recorder Interface Protocol) on TCP port **34567**, which is what this integration uses.

The **XMEye** brand is Xiongmai's end-user mobile app and cloud platform. If your camera, DVR or NVR can be configured through the *XMEye*, *NetSurveillance*, *CMS Pro* or *V-MS* app, it almost certainly speaks the DVRIP protocol and is compatible with this integration.

### How to tell if your device is compatible

- The device uses port **34567** for local control
- Configuration app is **XMEye**, **NetSurveillance**, **NVMS7000**, or similar Sofia-based software
- The web interface loads a page titled "NetSurveillance WEB" or similar
- Your DVR's firmware version contains strings like `IPC_`, `NVR_`, `XM530`, `XM550`, `HI3516`, `HI3518`

> **Not compatible** with ONVIF-only, Dahua, or Hikvision-native devices (even if they happen to also have an XMEye cloud account).

---

## Features

| Platform | What you get |
|---|---|
| **Camera** | Live RTSP stream per channel, on-demand JPEG snapshot, PTZ control (pan/tilt/zoom) |
| **Binary Sensor** | Push alarm events: motion, video loss, video blind/tamper, alarm input, I/O alarm, cross-line, intrusion |
| **Switch** | Enable / disable motion detection and recording per channel |
| **Sensor** | HDD total/used space, HDD status, firmware version |
| **Button** | Reboot the DVR/NVR remotely |

All events are **push-based** (`local_push`) — the integration maintains a persistent TCP connection and receives alarm notifications in real time with no polling delay.

---

## Supported Devices

This integration works with any device running the **Xiongmai Sofia firmware** that exposes the DVRIP protocol. This includes:

- **IP Cameras** — single-channel devices (e.g. HiSilicon HI3516/HI3518-based)
- **DVRs** — 4/8/16/32 channel digital video recorders
- **NVRs** — network video recorders with IP camera inputs
- **HVRs** — hybrid DVRs with both analogue and IP channels

**Compatible brands** (non-exhaustive): Annke, Sannce, Zosi, Floureon, Secam, Techage, Besder, and most OEM Xiongmai devices sold on AliExpress/Amazon.

**Not compatible**: ONVIF-only devices, Dahua, Hikvision, or any device that does not use the XMEye/NetSurveillance mobile app.

---

## Data Update

| Data | Method | Frequency |
|------|--------|-----------|
| Alarm events (motion, video loss, etc.) | **Push** via persistent TCP connection | Real-time |
| Storage / HDD info | Polling via short-lived TCP connection | Every 5 minutes (configurable in Options) |
| Channel detection (which slots have cameras) | HTTP probe at startup + periodic recheck | Startup + every 5 minutes |
| Device info (firmware, serial) | Fetched once at login | Once per connection |
| Channel titles | Fetched once at login | Once per connection |
| Detection / recording state | Fetched at setup and after each write | On demand |

---

## Known Limitations

- **H.265 streams**: Chrome, Chromium and Firefox on Linux cannot decode H.265/HEVC. When a channel probes as H.265 the integration registers it with go2rtc as a two-source stream — the raw RTSP URL first, then `ffmpeg:<stream>#video=h264#audio=opus` — so go2rtc hands the untouched H.265 to clients that support it (VLC, the mobile apps, Safari) and only starts a transcode when a client asks for H.264. See [H.265 handling](#h265-handling) below. Without a reachable go2rtc, live view falls back to still images in browsers without HEVC.
- **Privacy mode is HA-side only**: The "Privacy" switch hides the camera entity and stops recording via `ClosedRecord`, but the device firmware continues to process video. To fully disable a channel, use the Recording switch.
- **Digital channel HVRs**: On hybrid DVRs, encode settings live on the remote IPCs, not the DVR itself. The Privacy switch works around this by hiding the entity and forcing `ClosedRecord` HA-side.
- **Old firmware**: Devices with firmware older than ~2017 may use a different alarm packet format and not all event types may be recognized.
- **PTZ support varies**: Not all XMEye cameras support PTZ. The `xmeye.ptz` service will silently fail on non-PTZ devices.
- **No two-way audio**: The DVRIP protocol supports audio, but this integration does not implement audio streaming or two-way talk.

---

## H.265 handling

Each channel's RTSP stream is probed at startup and its codec read from the SDP. H.264
channels are left completely alone. For H.265 channels the integration registers a go2rtc
stream with two producers:

```yaml
xmeye_<entry_id>_ch<N>_camera:
  - rtsp://<dvr>:554/user=…&password=…&channel=N&stream=0.sdp                # H.265, passthrough
  - ffmpeg:xmeye_<entry_id>_ch<N>_camera#video=h264#audio=opus#bitrate=2048k  # H.264, on demand
```

go2rtc walks the producers in order and only dials the second one when the first one's
codecs do not match what the client asked for, so the ffmpeg transcode does not run until
a browser without HEVC opens the camera.

The stream is deliberately registered under the name Home Assistant's own go2rtc provider
uses (`<platform>_<unique_id>`), and `stream_source()` returns exactly the RTSP URL used as
the first producer. That combination is what stops HA from replacing the stream with its
audio-only variant — its provider skips the rewrite when the stream already exists and one
of its producers matches the camera's stream source. The registration is re-asserted on
every `stream_source()` call, so it self-heals.

### Options

Both settings live in the integration's options (⚙️ on the device page) and apply per
device. Changing either reloads the entry and re-registers the streams.

- **Transcode H.265 to H.264** (default on). Turn it off and the integration registers
  nothing with go2rtc, leaving H.265 handling to Home Assistant's defaults — live view then
  only works in clients that decode HEVC. The repair notices disappear too, since the
  trade-off was made deliberately.
- **H.264 transcode bitrate cap** (default 2048 kbit/s). go2rtc's built-in h264 template
  carries no rate control, so x264 falls back to CRF 23 and spends bits preserving the
  compression artefacts the DVR already introduced. On a 2560×1440 main stream that
  measured **6.6 Mbit/s out of a 0.73 Mbit/s H.265 source** — nine times the input. The cap
  becomes `-b:v/-maxrate/-bufsize` on the encoder. Raise it if the picture looks soft on
  busy scenes; it only affects the transcoded path, never what HEVC-capable clients receive.

Notes:

- **Any go2rtc works.** The integration reuses the connection the `go2rtc` integration
  already set up, so it works with the HA-managed binary (unix socket, no TCP port),
  with `go2rtc: debug_ui: true`, and with an external instance configured via
  `go2rtc: url:`. If HA does not manage a go2rtc at all, it falls back to probing
  `127.0.0.1:11984` and `127.0.0.1:1984`.
- **External go2rtc users:** registering a stream through the go2rtc API rewrites that
  stream's entry in `go2rtc.yaml` on disk. Only the `xmeye_…_camera` names are touched;
  your own stream names are left alone. Home Assistant itself already behaves this way.
- **Prefer H.264 at the source.** Transcoding costs one ffmpeg process per viewer without
  HEVC. Setting the affected channels to H.264 in the recorder's encode settings removes
  that cost entirely — but H.264 needs roughly 40–100 % more disk space per minute of
  recorded video than H.265 at comparable quality. The integration never changes that
  setting for you; it only raises a dismissible repair notice explaining the trade-off.

---

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add `https://github.com/equake/hass-xmeye` as type **Integration**
3. Search for **XMEye / Sofia** and install
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/xmeye/` folder into your HA `custom_components/` directory
2. Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **XMEye / Sofia**
3. Enter your device's IP address, port (default `34567`), username and password

### Auto-discovery

The setup flow includes a **LAN scan** option that broadcasts a DVRIP discovery query (UDP port 34569) to find XMEye devices on your network automatically.

---

## Entities created per device

For a 4-channel DVR, the integration creates:

| Count | Type | Example |
|---|---|---|
| 4 | Camera | `camera.dvr_ch1`, `camera.dvr_ch2` … |
| 28 | Binary Sensor | `binary_sensor.dvr_ch1_motion`, `binary_sensor.dvr_ch1_video_loss` … |
| 8 | Switch | `switch.dvr_ch1_motion_detection`, `switch.dvr_ch1_recording` … |
| 4 | Sensor | `sensor.dvr_hdd_total`, `sensor.dvr_firmware` … |
| 1 | Button | `button.dvr_reboot` |

---

## Camera & Streaming

### RTSP stream

The integration constructs the RTSP URL using the Sofia hash of your password:

```
rtsp://<host>:554/user=<user>&password=<sofia_hash>&channel=<N>&stream=0.sdp
```

- Stream `0` = main stream (high resolution)
- Stream `1` = sub-stream (lower resolution, less bandwidth)

### Snapshot

HTTP snapshots are fetched automatically from the device's CGI endpoint. The integration tries several known URL patterns in sequence:

```
http://<host>/web/cgi-bin/hi3510/snapPicture.cgi?chn=<N>
http://<host>/cgi-bin/snapshot.cgi?chn=<N>&q=0
http://<host>/snap.jpg?channel=<N>
```

### PTZ control

Use the standard HA camera PTZ service:

```yaml
service: camera.ptz
target:
  entity_id: camera.my_dvr_ch1
data:
  pan: right
  tilt: up
  speed: 3
```

Supported: `up`, `down`, `left`, `right`, diagonals, zoom `in`/`out`, and `stop`.

---

## Automation examples

### Notify with snapshot on motion

```yaml
automation:
  trigger:
    platform: state
    entity_id: binary_sensor.dvr_ch1_motion
    to: "on"
  action:
    - service: notify.mobile_app_my_phone
      data:
        message: "Motion detected on camera 1!"
        data:
          image: /api/camera_proxy/camera.dvr_ch1
```

### Reboot DVR if it becomes unreachable

```yaml
automation:
  trigger:
    platform: state
    entity_id: binary_sensor.dvr_ch1_motion
    to: unavailable
    for: "00:05:00"
  action:
    - service: button.press
      target:
        entity_id: button.dvr_reboot
```

### Disable recording at night

```yaml
automation:
  trigger:
    platform: time
    at: "23:00:00"
  action:
    - service: switch.turn_off
      target:
        entity_id:
          - switch.dvr_ch1_recording
          - switch.dvr_ch2_recording
```

---

## The DVRIP / Sofia Protocol

For those interested in how this integration works under the hood:

### Packet structure

Every DVRIP message is a 20-byte binary header followed by a UTF-8 JSON body:

```
Offset  Size  Field
0       1     Magic byte (always 0xFF)
1       1     Request/response flag (0x00 = request, 0x01 = response)
2       2     Reserved
4       4     Session ID (little-endian uint32, assigned at login)
8       4     Sequence number (little-endian uint32)
12      1     Total packets (0 or 1 = single packet)
13      1     Current packet index
14      2     Command code / Message ID (little-endian uint16)
16      4     JSON payload length (little-endian uint32)
20      N     JSON body (UTF-8, terminated with 0x0A 0x00)
```

### Authentication — the Sofia hash

Xiongmai devices do not transmit passwords in plain text. Instead, they use a proprietary MD5-based transformation:

1. Compute the MD5 digest of the password (16 bytes)
2. Take 8 pairs of consecutive bytes
3. For each pair: `index = (byte_a + byte_b) % 62`
4. Map the index to a character in `"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"`

The result is always an 8-character string. An empty password produces `tlJwpbo6`.

This hash is also used as the password in RTSP and HTTP Basic Auth requests to the device.

### Key command codes

| Code | Direction | Purpose |
|---|---|---|
| 1000 | → device | Login request |
| 1001 | ← device | Login response (contains session ID, channel count, keepalive interval) |
| 1006 | → device | Keepalive |
| 1007 | ← device | Keepalive acknowledgement |
| 1040 | → device | ConfigSet (write a named configuration block) |
| 1042 | → device | ConfigGet (read a named configuration block) |
| 1400 | → device | PTZ control (OPPTZControl) |
| 1500 | → device | Subscribe to alarm events |
| 1504 | ← device | Alarm notification (pushed by device) |

### Alarm event flow

```
Client                         Device (Sofia firmware)
  │                               │
  │─── Login (1000) ─────────────▶│
  │◀── LoginReply (1001) ─────────│  ← session ID, channel count
  │                               │
  │─── AlarmSubscribe (1500) ────▶│
  │◀── [optional ACK] ───────────│
  │                               │
  │  [motion detected on ch0]     │
  │◀── AlarmNotify (1504) ───────│  ← {"Event":"MotionDetect","Channel":0,"Status":"Start"}
  │◀── AlarmNotify (1504) ───────│  ← {"Event":"MotionDetect","Channel":0,"Status":"Stop"}
  │                               │
  │─── Keepalive (1006) ─────────▶│  [every AliveInterval seconds]
  │◀── KeepaliveReply (1007) ────│
```

### Architecture inside this integration

The integration maintains **one persistent TCP connection per device** for alarm reception. All user-triggered commands (PTZ, config reads/writes, reboot) use **short-lived secondary connections** — connect, login, execute, close — to avoid interfering with the alarm stream. An `asyncio.Lock` ensures concurrent commands are serialized safely.

---

## Troubleshooting

### Cannot connect

- Verify the device is reachable: `ping <host>`
- Confirm TCP port 34567 is open: `nc -zv <host> 34567`
- Some DVRs require the *local network management* port to be enabled in device settings

### Authentication fails

- Try with an empty password (many devices ship with no password set)
- Verify credentials in the XMEye app or the device's web interface
- The integration always uses the **Sofia hash**, not a plain-text password

### No alarm events received

- Confirm alarm/motion detection is enabled on the device
- Check that the sensitivity is not set to zero in the device settings
- Very old firmware (pre-2017) may use a different alarm packet format

### RTSP stream not working

- Test the URL directly in VLC: `Media → Open Network Stream`
- Some devices use a non-standard RTSP port or URL path
- Check your router/firewall is not blocking TCP 554

### HDD sensors show "unknown"

- Cameras without a storage slot will always show `unknown`
- Some firmware versions do not expose storage info via DVRIP

---

## About the brand icon

The integration ships its icon under `custom_components/xmeye/brand/` (`icon.png` 256×256 and `icon@2x.png` 512×512, both RGBA). Home Assistant 2026.3 introduced the [Brands Proxy API](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api) — from that release onward, the icon is served from `GET /api/brands/integration/xmeye/icon.png` and shows up correctly in **Settings → Devices & Services**.

If the icon is missing from the **HACS dashboard** (the main store list, or the "update available" tile), that is a known gap in the HACS frontend, not a problem with this repository — see [hacs/integration#5171](https://github.com/hacs/integration/issues/5171). The legacy `home-assistant/brands` repository used to host custom-integration icons, but since February 2026 it auto-closes new submissions and points authors to the local `brand/` folder shipped with the integration, which is what we do here.

---

## Contributing

Bug reports and pull requests are welcome. When reporting an issue, please include:

- Home Assistant version
- Device model and firmware version (visible at `sensor.<name>_firmware`)
- Relevant log entries (`Settings → System → Logs`, filter by `xmeye`)

---

## Reference implementations

The protocol details in this integration were derived from the following open-source projects and analyses:

- [alexshpilkin/dvrip](https://github.com/alexshpilkin/dvrip) — clean Python DVRIP library
- [sofia-netsurv/python-netsurv](https://github.com/sofia-netsurv/python-netsurv) — Python NetSurveillance SDK
- [xyyangkun/python-dvr](https://github.com/xyyangkun/python-dvr) — Python DVRIP implementation with alarm support
- [KostasEreksonas/DVRIP_analysis](https://github.com/KostasEreksonas/DVRIP_analysis) — Wireshark dissector and packet-level analysis

---

## License

MIT — see [LICENSE](LICENSE) for details.
