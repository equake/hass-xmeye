"""Constants for the XMEye/Sofia alarm integration."""

from __future__ import annotations

DOMAIN = "xmeye"
DEFAULT_PORT = 34567
DEFAULT_USERNAME = "admin"

CONF_CHANNEL_COUNT = "channel_count"
CONF_DEVICE_TYPE = "device_type"
CONF_MOTION_CLEAR_DELAY = "motion_clear_delay"
CONF_STORAGE_REFRESH_INTERVAL = "storage_refresh_interval"

DEFAULT_MOTION_CLEAR_DELAY = 30  # seconds; 0 = disable debounce
DEFAULT_STORAGE_REFRESH_INTERVAL = 300  # seconds (5 min)
MIN_STORAGE_REFRESH_INTERVAL = 60  # 1 min
MAX_STORAGE_REFRESH_INTERVAL = 3600  # 1 hour

# DVRIP message IDs
MSG_LOGIN = 1000
MSG_LOGIN_RSP = 1001
MSG_KEEPALIVE = 1006
MSG_KEEPALIVE_RSP = 1007
MSG_CONFIG_SET = 1040
MSG_CONFIG_GET = 1042
MSG_CHANNEL_TITLE = 1048
MSG_SYSTEM_INFO = 1020  # SystemInfo/StorageInfo/WorkState query (response: 1021)
MSG_ABILITY_GET = 1360  # SystemFunction capability query (response: 1361)
MSG_PTZ_CONTROL = 1400
MSG_ALARM_SUBSCRIBE = 1500
MSG_ALARM_NOTIFY = 1504

# ConfigGet/Set key names
CONF_NAME_GENERAL = "General"
# Storage is runtime info queried via SystemInfo (cmd 1020), NOT a ConfigGet (1042)
# block — ConfigGet returns Ret=607 for it. See coordinator._fetch_storage.
CONF_NAME_STORAGE = "StorageInfo"
CONF_NAME_SYSFUNCTION = "SystemFunction"  # capability flags (queried via cmd 1360)

# Per-channel detection lives in the Detect block. Writing the whole 155 KB block
# times out, so address each channel's sub-section directly: "Detect.<Kind>.[ch]".
# The list form "Detect.<Kind>" is used for bulk reads (cache).
CONF_NAME_DETECT = "Detect"
# Detection kinds -> (Detect sub-key, SystemFunction.AlarmFunction capability flag).
DETECT_KINDS = {
    "motion": ("MotionDetect", "MotionDetect"),
    "human": ("HumanDetectionDVR", "HumanDectionNVRNew"),
    "face": ("FaceDetection", "FaceDetect"),
}

# Recording mode lives in the Record block (list, one entry per channel).
CONF_NAME_RECORD = "Record"
RECORD_MODE_OFF = "ClosedRecord"
RECORD_MODE_ALWAYS = "ManualRecord"
RECORD_MODE_SCHEDULE = "ConfigRecord"  # follow the DVR schedule (device default)

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

SERVICE_PTZ = "ptz"

CONFIG_ENTRY_VERSION = 1

# Dispatcher signal fired when a new camera channel is confirmed.
# Format: SIGNAL_NEW_CHANNEL.format(entry_id)
SIGNAL_NEW_CHANNEL = "xmeye_{}_new_channel"

# Minimum JPEG size (bytes) to accept as a real video frame during channel probe.
# Placeholder icons returned by some NVR firmwares are ~750 bytes; real frames
# are typically 50 KB+.
MIN_SNAPSHOT_BYTES = 10_000
