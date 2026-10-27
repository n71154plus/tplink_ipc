"""Constants."""

DOMAIN = "tplink_ipc"
PLATFORMS = ["camera", "switch", "select", "number", "button", "sensor"]
CLOUD_PLATFORMS = ["sensor", "switch", "button"]

CONF_HOST = "host"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_USE_HTTPS = "use_https"
CONF_VERIFY_SSL = "verify_ssl"
CONF_RTSP_PORT = "rtsp_port"
CONF_STREAM_PATH = "stream_path"

DEFAULT_PORT = 80
DEFAULT_RTSP_PORT = 554
DEFAULT_USERNAME = "admin"
DEFAULT_STREAM_PATH = "stream1"
DEFAULT_VERIFY_SSL = False

TP_ENCRYPT_A = "RDpbLfCPsJZ7fiv"
TP_ENCRYPT_C = (
    "yLwVl0zKqws7LgKPRQ84Mdt708T1qQ3Ha7xv3H7NyU84p21BriUWBU43odz3iP4rBL3cD02KZciX"
    "TysVXiV8ngg6vL48rPJyAUw0HurW20xqxv9aYb4M9wK1Ae0wlro510qXeU07kV57fQMc8L6aLgML"
    "wygtc0F10a0Dg70TOoouyFhdysuRMO51yY5ZlOZZLEal1h0t9YQW0Ko7oBwmCAHoic4HYbUyVeU3"
    "sfQ1xtXcPcf1aT303wAQhv66qzW "
)

ATTR_DIRECTION = "direction"
ATTR_X = "x"
ATTR_Y = "y"
ATTR_ZOOM = "zoom"
ATTR_PAN = "pan"
ATTR_TILT = "tilt"
ATTR_PRESET_ID = "preset_id"
ATTR_PRESET_NAME = "preset_name"

SERVICE_PTZ_MOVE = "ptz_move"
SERVICE_PTZ_STEP = "ptz_step"
SERVICE_PTZ_STOP = "ptz_stop"
SERVICE_PTZ_CONTINUOUS = "ptz_continuous"
SERVICE_PTZ_ZOOM = "ptz_zoom"
SERVICE_PTZ_CALIBRATE = "ptz_calibrate"
SERVICE_PTZ_PRESET = "ptz_goto_preset"
SERVICE_PTZ_SET_PRESET = "ptz_set_preset"
SERVICE_REBOOT = "reboot"
SERVICE_RAW = "raw_request"
SERVICE_SEARCH_DAYS = "search_recording_days"
SERVICE_SEARCH_VIDEO = "search_video_segments"
SERVICE_SEARCH_EVENTS = "search_events"
SERVICE_SEARCH_TIMELINE = "search_day_timeline"
SERVICE_REFRESH_CALENDARS = "refresh_all_calendars"
SERVICE_PLAY_RECORDING = "play_recording"
SERVICE_STREAM_PLAYBACK = "stream_playback"
SERVICE_FORMAT_DISK = "format_disk"
SERVICE_FORMAT_SD = "format_sd"
SERVICE_GET_FORMAT_STATUS = "get_format_status"
SERVICE_SET_TIMING_REBOOT = "set_timing_reboot"
SERVICE_DOORBELL_WAKEUP = "doorbell_wakeup"
SERVICE_TRIGGER_ALARM = "trigger_alarm"

ATTR_DISK_NAME = "disk_name"
ATTR_SD_NAME = "sd_name"
ATTR_DAY = "day"
ATTR_TIME = "time"
ATTR_ENABLED = "enabled"
ATTR_SECONDS = "seconds"

CONF_GO2RTC_URL = "go2rtc_url"
DEFAULT_GO2RTC_URL = "http://127.0.0.1:1984"
PLAYBACK_BRIDGE_PORT = 8791

# Optional camera LAN IP → go2rtc stream names (live / history WebRTC).
# Leave empty to use generated names like tplink_pb_<host>_ch0.
GO2RTC_LIVE_BY_HOST: dict[str, str] = {}
GO2RTC_PLAYBACK_BY_HOST: dict[str, str] = {}


def playback_stream_name(host: str, channel: int = 0) -> str:
    host = (host or "").strip()
    named = GO2RTC_PLAYBACK_BY_HOST.get(host)
    if named and int(channel) == 0:
        return named
    if named:
        return f"{named}_ch{int(channel)}"
    key = "".join(ch if ch.isalnum() else "_" for ch in host)
    return f"tplink_pb_{key}_ch{int(channel)}"

ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"
ATTR_DATE = "date"
ATTR_CHANNEL = "channel"
ATTR_START_TIME = "start_time"
ATTR_END_TIME = "end_time"
ATTR_CLIENT_ID = "client_id"
ATTR_START_INDEX = "start_index"
ATTR_END_INDEX = "end_index"
ATTR_MAX_NUM = "max_num"
ATTR_MAX_SECONDS = "max_seconds"
ATTR_MAX_BYTES = "max_bytes"
ATTR_SEGMENT_INDEX = "segment_index"

CONF_STREAM_PORT = "stream_port"
DEFAULT_STREAM_PORT = 8000

# Cloud (official tpSurveillance / n-wap-ipc)
CONF_CONNECTION_TYPE = "connection_type"
CONN_LOCAL = "local"
CONN_CLOUD = "cloud"
CONF_TERMINAL_UUID = "terminal_uuid"
CONF_APP_TYPE = "app_type"
CONF_CLOUD_HOST = "cloud_host"

SERVICE_CLOUD_DEVICE_LIST = "cloud_device_list"
SERVICE_CLOUD_PASSTHROUGH = "cloud_passthrough"
SERVICE_CLOUD_REBOOT = "cloud_reboot"
SERVICE_CLOUD_GET_DISK = "cloud_get_disk"
SERVICE_CLOUD_GET_RECORD_PLAN = "cloud_get_record_plan"
SERVICE_CLOUD_GET_DEVICE_INFO = "cloud_get_device_info"
SERVICE_CLOUD_GET_BIZ_TOKEN = "cloud_get_biz_token"
SERVICE_CLOUD_GET_RELAY_CLUSTER = "cloud_get_relay_cluster"
SERVICE_CLOUD_PREPARE_RELAY = "cloud_prepare_relay"
SERVICE_CLOUD_PLAY_RECORDING = "cloud_play_recording"
ATTR_DEVICE_ID = "device_id"
ATTR_PAYLOAD = "payload"
ATTR_STREAM_TYPE = "stream_type"
ATTR_BIZ_SERVICE_ID = "biz_service_id"

DIR_RIGHT = "0"
DIR_UP = "90"
DIR_LEFT = "180"
DIR_DOWN = "270"
