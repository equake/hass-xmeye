#!/usr/bin/env python3
"""Standalone test script — connect to an XMEye device and print alarm events.

Usage:
    python test_client.py <host> [port] [username] [password]

Examples:
    python test_client.py 192.168.1.100
    python test_client.py 192.168.1.100 34567 admin MyPass123
"""

import asyncio
import sys
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Make the integration importable from this directory
sys.path.insert(0, ".")
from custom_components.xmeye.client import XMEyeClient, sofia_hash


async def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    host = args[0]
    port = int(args[1]) if len(args) > 1 else 34567
    username = args[2] if len(args) > 2 else "admin"
    password = args[3] if len(args) > 3 else ""

    print(f"Connecting to {host}:{port} as '{username}'")
    print(f"Sofia hash of password: {sofia_hash(password)!r}")
    print()

    client = XMEyeClient(host, port, username, password)
    try:
        await client.connect()
        print("TCP connection established.")

        info = await client.login()
        print(
            f"Login OK — device={info.device_type!r}  "
            f"channels={info.channel_count}  "
            f"keepalive={info.keepalive_interval}s"
        )

        print("\n--- Device General config ---")
        general = await client.config_get("General")
        for k, v in general.items():
            print(f"  {k}: {v}")

        print("\n--- Storage info ---")
        for name in ("StorageDeviceInfo", "StorageInfo"):
            storage = await client.config_get(name)
            if storage:
                print(f"  ({name}): {storage}")
                break
        else:
            print("  Not available on this device")

        await client.subscribe_alarms()
        print("\nSubscribed to alarms. Waiting for events (Ctrl+C to stop)...\n")

        async for event in client.read_events():
            active = "START" if event.active else "STOP "
            print(f"  [{active}] Channel {event.channel + 1} — {event.event_type}")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
