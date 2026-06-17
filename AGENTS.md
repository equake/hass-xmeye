# AGENTS.md — XMEye / Sofia Integration

Read this file before making any changes.

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

**Key CMDs**: 1000=login, 1001=login_rsp, 1042=config_get, 1400=ptz, 1500=alarm_subscribe, 1504=alarm_notify

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
python test_client.py <host> [port] [username] [password]
```