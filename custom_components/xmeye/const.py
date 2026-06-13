"""Constants for the XMEye/Sofia alarm integration."""

from __future__ import annotations

DOMAIN = "xmeye"
DEFAULT_PORT = 34567
DEFAULT_USERNAME = "admin"

CONF_CHANNEL_COUNT = "channel_count"
CONF_DEVICE_TYPE = "device_type"

# DVRIP message IDs
MSG_LOGIN = 1000
MSG_LOGIN_RSP = 1001
MSG_KEEPALIVE = 1006
MSG_KEEPALIVE_RSP = 1007
MSG_CONFIG_SET = 1040
MSG_CONFIG_GET = 1042
MSG_PTZ_CONTROL = 1400
MSG_ALARM_SUBSCRIBE = 1500
MSG_ALARM_NOTIFY = 1504

# ConfigGet/Set key names
CONF_NAME_GENERAL = "General"
CONF_NAME_STORAGE = "StorageDeviceInfo"
CONF_NAME_STORAGE_ALT = "StorageInfo"
CONF_NAME_MOTION = "MotionDetect"
CONF_NAME_ENCODE = "Simplify.Encode"
CONF_NAME_ENCODE_ALT = "Encode"

# UDP device discovery
UDP_DISCOVERY_PORT = 34569

# Response codes
RET_OK = {100, 515}
RET_AUTH_FAIL = {101, 106, 203}

# Alarm event types (as reported by device)
EVENT_MOTION = "MotionDetect"
EVENT_VIDEO_LOSS = "VideoLost"
EVENT_VIDEO_BLIND = "HideAlarm"
EVENT_ALARM_INPUT = "AlarmLocal"
EVENT_IO_ALARM = "IOAlarm"
EVENT_CROSS_LINE = "CrossLineDetection"
EVENT_INTRUSION = "PEAAlarm"

ALL_EVENT_TYPES = [
    EVENT_MOTION,
    EVENT_VIDEO_LOSS,
    EVENT_VIDEO_BLIND,
    EVENT_ALARM_INPUT,
    EVENT_IO_ALARM,
    EVENT_CROSS_LINE,
    EVENT_INTRUSION,
]

RECONNECT_DELAY = 30  # seconds between reconnection attempts

CONFIG_ENTRY_VERSION = 1
