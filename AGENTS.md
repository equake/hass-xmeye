# AGENTS.md — XMEye / Sofia Integration

Read this file before making any changes.

## Workflow (required)

**Always work on a feature branch and open a PR — never commit directly to `main`.**
Branch off `main` (`fix/...`, `feat/...`), commit there, push, and open a PR with `gh pr create`.

## Quick Reference

| Item | Value |
|------|-------|
| Domain | `xmeye` |
| Protocol | DVRIP (TCP 34567) |
| Min HA | 2026.1.0 |
| Min Python | 3.12 |

## Key Files

- `custom_components/xmeye/` — integration code
- `client.py` — DVRIP protocol (sofia_hash, XMEyeClient)
- `coordinator.py` — persistent connection + commands
- `entity.py` — XMEyeEntity base class
- `strings.json` — translation source of truth

## Architecture

- **Persistent connection**: login → subscribe_alarms → read_events (alarm loop)
- **Command connections**: short-lived per-call via `async_run_command(fn)`, serialized by `_command_lock`
- **Config entry**: `type XMEyeConfigEntry = ConfigEntry[XMEyeCoordinator]`
- All platforms: use `entry.runtime_data`, never `hass.data`

## Coding Conventions

- `from __future__ import annotations` at top of every file
- Type aliases: `type X = Y`
- Union syntax: `X | Y` (not `Optional[X]` or `Union[X, Y]`)
- Entity category: `EntityCategory.DIAGNOSTIC` (info) or `EntityCategory.CONFIG` (actions)
- `_attr_has_entity_name = True` on all entities
- Multi-channel: `_attr_translation_placeholders = {"channel": str(channel + 1)}` with `{channel}` in strings.json
- No comments unless explaining non-obvious protocol quirks

## DVRIP Essentials

**Packet**: 20-byte header + JSON body
```
struct "<BB2xII2xHI>" — magic(1) + flag(1) + sid(4) + seq(4) + cmd(2) + len(4)
```

**Sofia hash** (password encoding):
```python
def sofia_hash(password: str) -> str:
    digest = hashlib.md5(password.encode()).digest()
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "".join(chars[(digest[i*2] + digest[i*2+1]) % 62] for i in range(8))
```

**Commands** (request cmd → response cmd; every request body carries `"SessionID": "0x%08X"`):

| CMD | Resp | client method | `Name` in payload | Purpose |
|-----|------|---------------|-------------------|---------|
| 1000 | 1001 | `login` | — | Login (MD5 / sofia_hash) |
| 1006 | 1007 | `keepalive` | `KeepAlive` | Keep session alive |
| 1020 | 1021 | `system_info` | `SystemInfo` / `StorageInfo` / `WorkState` | Runtime info (see below) |
| 1040 | 1041 | `config_set` / `reboot` | block name / `OPMachine` | Write config / reboot |
| 1042 | 1043 | `config_get` | `General`, `Detect.*`, `Record` | Read config blocks |
| 1048 | 1049 | `channel_title` | `ChannelTitle` | Channel names |
| 1360 | 1361 | `ability_get` | `SystemFunction` | Capability flags (NOT a 1042 block → 607) |
| 1400 | — | `ptz_control` | `OPPTZControl` | PTZ (often un-ACKed) |
| 1500 | — | `subscribe_alarms` | `""` | Subscribe to alarm push |
| 1504 | — | (push) | `AlarmInfo` | Alarm event notification |

**ConfigGet (1042) vs SystemInfo (1020) — important**: 1042 is only for *config* blocks. Runtime
info (`SystemInfo`, `StorageInfo`, `WorkState`) is **not** a config block — querying it via 1042
returns **`Ret=607`**. Use `client.system_info(name)` (cmd 1020) instead.

**`SystemInfo`** (cmd 1020, `Name="SystemInfo"`) → device identity (NOT in `General`):
- `SoftWareVersion` = firmware · `SerialNo` = serial · `HardWare` = hardware model code
  (e.g. `NBD80X16S-KL`) · `BuildTime`, `DeviceRunTime`, channel counts.

**`StorageInfo`** (cmd 1020, `Name="StorageInfo"`) → list of physical disks, each:
`{ModelNumber, SerialNumber, PartNumber, Partition: [...]}`. Each partition has `TotalSpace` /
`RemainSpace` as **hex strings in MB** (e.g. `"0x001D1C11"`), plus `Status` (0 = OK), `IsCurrent`
(partition being recorded to), `DirverType` (0 = read/write), and `Old*/New*Time` record windows.

**`WorkState`** (cmd 1020, `Name="WorkState"`) → per-channel record/bitrate state.

**Per-channel controls** (switch.py / coordinator):
- **Detection** lives in the `Detect` block, but the whole block is ~155 KB and SET **times out** —
  address each channel's sub-section directly: `Detect.<Kind>.[ch]` (e.g. `Detect.MotionDetect.[2]`),
  a small `{Enable, EventHandler, Level, Region}` dict. `Detect.<Kind>` (no index) returns the
  per-channel `list`, used for bulk reads. Kinds: `MotionDetect`, `FaceDetection`, `HumanDetectionDVR`
  (the last may be a degenerate non-per-channel list on some firmwares → gate on shape).
- **Recording** = `Record[ch].RecordMode` ∈ `ManualRecord` (always) / `ClosedRecord` (off) /
  `ConfigRecord` (follow schedule). `Record` SET (full list) works.
- **Capabilities**: `SystemFunction.AlarmFunction.{MotionDetect, FaceDetect, HumanDectionNVRNew}`.
- **Privacy/encode caveat**: disabling video encode (`Simplify.Encode[ch].MainFormat.VideoEnable`) is
  the universal "blank the channel" lever, BUT on **digital-channel** HVRs `Simplify.Encode`/`Encode`
  are degenerate (`{"0":null,"1":null,"Enable":true}`) — encode lives on the remote IPCs. There the
  Privacy switch is HA-side (`coordinator.private_channels` → camera entity hidden + `ClosedRecord`).

**Response codes** (`Ret`): `100`/`515` = OK (`RET_OK`); `607` = wrong command channel for that
`Name` (e.g. 1042 asked for runtime info); `101`/`106`/`203` = auth failure (`RET_AUTH_FAIL`).

## Adding Platforms/Entities

1. Create `<platform>.py` with class inheriting `XMEyeEntity, <HAEntityBase>`
2. Add platform to `PLATFORMS` in `__init__.py`
3. Add translation keys to `strings.json`, `translations/en.json`, `translations/pt.json`

## Linting

```bash
pip install ruff
ruff check custom_components/
```

## Manual Testing

```bash
python scripts/test_client.py <host> [port] [username] [password]
```
Prints General config + SystemInfo/StorageInfo (cmd 1020) and then streams alarm events.