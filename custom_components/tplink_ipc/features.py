"""Feature declarations mapped from tpSurveillance APK local JSON API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


def _on_off(v: Any) -> bool:
    return str(v).lower() in ("on", "1", "true")


def _to_on_off(v: bool) -> str:
    return "on" if v else "off"


@dataclass(frozen=True)
class SwitchFeature:
    key: str
    name: str
    icon: str
    # path into coordinator.data dict, e.g. ("led","config","enabled")
    path: tuple[str, ...]
    # payload builder: enabled bool -> request body
    set_payload: Callable[[bool], dict[str, Any]]
    # module_spec key required (None = always try if path present)
    requires: str | None = None


@dataclass(frozen=True)
class SelectFeature:
    key: str
    name: str
    icon: str
    path: tuple[str, ...]
    options: list[str]
    set_payload: Callable[[str], dict[str, Any]]
    requires: str | None = None
    option_labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NumberFeature:
    key: str
    name: str
    icon: str
    path: tuple[str, ...]
    minimum: float
    maximum: float
    step: float
    set_payload: Callable[[float], dict[str, Any]]
    requires: str | None = None
    unit: str | None = None


def dig(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = data
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


# --- Switches ---

SWITCHES: list[SwitchFeature] = [
    SwitchFeature(
        "privacy",
        "Privacy mode",
        "mdi:eye-off",
        ("lens_mask", "lens_mask_info", "enabled"),
        lambda on: {
            "method": "set",
            "lens_mask": {"lens_mask_info": {"enabled": _to_on_off(on)}},
        },
        "lens_mask",
    ),
    SwitchFeature(
        "led",
        "Status LED",
        "mdi:led-on",
        ("led", "config", "enabled"),
        lambda on: {"method": "set", "led": {"config": {"enabled": _to_on_off(on)}}},
        "led",
    ),
    SwitchFeature(
        "motion",
        "Motion detection",
        "mdi:motion-sensor",
        ("motion_detection", "motion_det", "enabled"),
        lambda on: {
            "method": "set",
            "motion_detection": {"motion_det": {"enabled": _to_on_off(on)}},
        },
    ),
    SwitchFeature(
        "motion_people",
        "Motion people filter",
        "mdi:walk",
        ("motion_detection", "motion_det", "people_enabled"),
        lambda on: {
            "method": "set",
            "motion_detection": {"motion_det": {"people_enabled": _to_on_off(on)}},
        },
    ),
    SwitchFeature(
        "motion_vehicle",
        "Motion vehicle filter",
        "mdi:car",
        ("motion_detection", "motion_det", "vehicle_enabled"),
        lambda on: {
            "method": "set",
            "motion_detection": {"motion_det": {"vehicle_enabled": _to_on_off(on)}},
        },
    ),
    SwitchFeature(
        "people_detection",
        "People detection",
        "mdi:account-alert",
        ("people_detection", "detection", "enabled"),
        lambda on: {
            "method": "set",
            "people_detection": {"detection": {"enabled": _to_on_off(on)}},
        },
        "smart_detection",
    ),
    SwitchFeature(
        "tamper",
        "Tamper detection",
        "mdi:shield-alert",
        ("tamper_detection", "tamper_det", "enabled"),
        lambda on: {
            "method": "set",
            "tamper_detection": {"tamper_det": {"enabled": _to_on_off(on)}},
        },
    ),
    SwitchFeature(
        "intrusion",
        "Intrusion detection",
        "mdi:vector-polygon",
        ("intrusion_detection", "detection", "enabled"),
        lambda on: {
            "method": "set",
            "intrusion_detection": {"detection": {"enabled": _to_on_off(on)}},
        },
        "smart_detection",
    ),
    SwitchFeature(
        "linecrossing",
        "Line crossing detection",
        "mdi:arrow-collapse-horizontal",
        ("linecrossing_detection", "detection", "enabled"),
        lambda on: {
            "method": "set",
            "linecrossing_detection": {"detection": {"enabled": _to_on_off(on)}},
        },
        "smart_detection",
    ),
    SwitchFeature(
        "msg_push",
        "Message push",
        "mdi:message-badge",
        ("msg_push", "chn1_msg_push_info", "enabled"),
        lambda on: {
            "method": "set",
            "msg_push": {"chn1_msg_push_info": {"enabled": _to_on_off(on)}},
        },
        "msg_push",
    ),
    SwitchFeature(
        "siren",
        "Alarm siren",
        "mdi:alarm-light",
        ("msg_alarm", "chn1_msg_alarm_info", "enabled"),
        lambda on: {
            "method": "set",
            "msg_alarm": {"chn1_msg_alarm_info": {"enabled": _to_on_off(on)}},
        },
    ),
    SwitchFeature(
        "siren_sound",
        "Alarm sound",
        "mdi:volume-high",
        ("msg_alarm", "chn1_msg_alarm_info", "sound_alarm_enabled"),
        lambda on: {
            "method": "set",
            "msg_alarm": {
                "chn1_msg_alarm_info": {"sound_alarm_enabled": _to_on_off(on)}
            },
        },
    ),
    SwitchFeature(
        "siren_light",
        "Alarm light",
        "mdi:alarm-light-outline",
        ("msg_alarm", "chn1_msg_alarm_info", "light_alarm_enabled"),
        lambda on: {
            "method": "set",
            "msg_alarm": {
                "chn1_msg_alarm_info": {"light_alarm_enabled": _to_on_off(on)}
            },
        },
    ),
    SwitchFeature(
        "timing_reboot",
        "Scheduled reboot",
        "mdi:calendar-clock",
        ("timing_reboot", "reboot", "enabled"),
        lambda on: {
            "method": "set",
            "timing_reboot": {"reboot": {"enabled": _to_on_off(on)}},
        },
        "timing_reboot",
    ),
    SwitchFeature(
        "pir_detection",
        "PIR detection",
        "mdi:motion-sensor",
        ("pir_detection", "pir_det", "enabled"),
        lambda on: {
            "method": "set",
            "pir_detection": {"pir_det": {"enabled": _to_on_off(on)}},
        },
        "pir_detection",
    ),
    SwitchFeature(
        "pir_led",
        "PIR LED",
        "mdi:led-on",
        ("pir_detection", "pir_det", "led"),
        lambda on: {
            "method": "set",
            "pir_detection": {"pir_det": {"led": _to_on_off(on)}},
        },
        "pir_detection",
    ),
    SwitchFeature(
        "target_track",
        "Auto tracking",
        "mdi:crosshairs-gps",
        ("target_track", "target_track_info", "enabled"),
        lambda on: {
            "method": "set",
            "target_track": {"target_track_info": {"enabled": _to_on_off(on)}},
        },
        "target_track",
    ),
    SwitchFeature(
        "track_people",
        "Track people",
        "mdi:account-arrow-right",
        ("target_track", "target_track_info", "people_enabled"),
        lambda on: {
            "method": "set",
            "target_track": {
                "target_track_info": {"people_enabled": _to_on_off(on)}
            },
        },
        "target_track",
    ),
    SwitchFeature(
        "track_vehicle",
        "Track vehicles",
        "mdi:car-cruise-control",
        ("target_track", "target_track_info", "vehicle_enabled"),
        lambda on: {
            "method": "set",
            "target_track": {
                "target_track_info": {"vehicle_enabled": _to_on_off(on)}
            },
        },
        "target_track",
    ),
    SwitchFeature(
        "track_sound",
        "Track by sound",
        "mdi:ear-hearing",
        ("target_track", "target_track_info", "sound_enabled"),
        lambda on: {
            "method": "set",
            "target_track": {
                "target_track_info": {"sound_enabled": _to_on_off(on)}
            },
        },
        "target_track",
    ),
    SwitchFeature(
        "video_cover",
        "Video cover / mask",
        "mdi:checkbox-blank",
        ("cover", "cover", "enabled"),
        lambda on: {
            "method": "set",
            "cover": {"cover": {"enabled": _to_on_off(on)}},
        },
    ),
    SwitchFeature(
        "record_plan",
        "Recording schedule",
        "mdi:record-rec",
        ("record_plan", "chn1_channel", "enabled"),
        lambda on: {
            "method": "set",
            "record_plan": {"chn1_channel": {"enabled": _to_on_off(on)}},
        },
        "local_storage",
    ),
    SwitchFeature(
        "loop_record",
        "Loop recording",
        "mdi:repeat",
        ("harddisk_manage", "harddisk", "loop"),
        lambda on: {
            "method": "set",
            "harddisk_manage": {"harddisk": {"loop": _to_on_off(on)}},
        },
        "local_storage",
    ),
    SwitchFeature(
        "media_encrypt",
        "Media encryption",
        "mdi:lock",
        ("cet", "media_encrypt", "enabled"),
        lambda on: {
            "method": "set",
            "cet": {"media_encrypt": {"enabled": _to_on_off(on)}},
        },
        "media_encrypt",
    ),
    SwitchFeature(
        "wide_dynamic",
        "Wide dynamic range",
        "mdi:white-balance-sunny",
        ("image", "common", "wide_dynamic"),
        lambda on: {
            "method": "set",
            "image": {"common": {"wide_dynamic": _to_on_off(on)}},
        },
        "image_capability",
    ),
    SwitchFeature(
        "hlc",
        "Highlight compensation",
        "mdi:brightness-7",
        ("image", "common", "high_light_compensation"),
        lambda on: {
            "method": "set",
            "image": {"common": {"high_light_compensation": _to_on_off(on)}},
        },
        "image_capability",
    ),
    SwitchFeature(
        "dehaze",
        "Dehaze",
        "mdi:weather-fog",
        ("image", "common", "dehaze"),
        lambda on: {
            "method": "set",
            "image": {"common": {"dehaze": _to_on_off(on)}},
        },
        "image_capability",
    ),
    SwitchFeature(
        "eis",
        "Electronic image stabilization",
        "mdi:axis-arrow",
        ("image", "common", "eis"),
        lambda on: {
            "method": "set",
            "image": {"common": {"eis": _to_on_off(on)}},
        },
        "image_capability",
    ),
    SwitchFeature(
        "backlight",
        "Backlight compensation",
        "mdi:lightbulb-on",
        ("image", "common", "backlight"),
        lambda on: {
            "method": "set",
            "image": {"common": {"backlight": _to_on_off(on)}},
        },
        "image_capability",
    ),
    SwitchFeature(
        "people_enhance",
        "Full-color people enhance",
        "mdi:account-cowboy-hat",
        ("image", "switch", "full_color_people_enhance"),
        lambda on: {
            "method": "set",
            "image": {
                "switch": {"full_color_people_enhance": _to_on_off(on)}
            },
        },
        "ai_enhance_capability",
    ),
]


SELECTS: list[SelectFeature] = [
    SelectFeature(
        "night_vision",
        "Night vision mode",
        "mdi:weather-night",
        ("image", "switch", "night_vision_mode"),
        ["inf_night_vision", "md_night_vision", "wtl_night_vision"],
        lambda v: {
            "method": "set",
            "image": {"switch": {"night_vision_mode": v}},
        },
        "daynight_subdivision",
        {
            "inf_night_vision": "Infrared",
            "md_night_vision": "Smart / minimum illumination",
            "wtl_night_vision": "White light / full color",
        },
    ),
    SelectFeature(
        "flip",
        "Image flip",
        "mdi:flip-vertical",
        ("image", "switch", "flip_type"),
        ["off", "center", "left_and_right", "up_and_down"],
        lambda v: {"method": "set", "image": {"switch": {"flip_type": v}}},
        "image_capability",
    ),
    SelectFeature(
        "rotate",
        "Image rotate",
        "mdi:rotate-right",
        ("image", "switch", "rotate_type"),
        ["off", "anticlockwise_90", "anticlockwise_180"],
        lambda v: {"method": "set", "image": {"switch": {"rotate_type": v}}},
        "image_capability",
    ),
    SelectFeature(
        "daynight_mode",
        "Day/night switch mode",
        "mdi:theme-light-dark",
        ("image", "switch", "switch_mode"),
        ["common", "schedule", "auto"],
        lambda v: {"method": "set", "image": {"switch": {"switch_mode": v}}},
        "daynight_subdivision",
    ),
    SelectFeature(
        "motion_sensitivity",
        "Motion sensitivity",
        "mdi:tune",
        ("motion_detection", "motion_det", "sensitivity"),
        ["low", "medium", "high"],
        lambda v: {
            "method": "set",
            "motion_detection": {"motion_det": {"sensitivity": v}},
        },
    ),
    SelectFeature(
        "tamper_sensitivity",
        "Tamper sensitivity",
        "mdi:tune-vertical",
        ("tamper_detection", "tamper_det", "sensitivity"),
        ["low", "medium", "high"],
        lambda v: {
            "method": "set",
            "tamper_detection": {"tamper_det": {"sensitivity": v}},
        },
    ),
    SelectFeature(
        "track_mode",
        "Tracking mode",
        "mdi:radar",
        ("target_track", "target_track_info", "track_mode"),
        ["pan", "tilt", "pan_tilt"],
        lambda v: {
            "method": "set",
            "target_track": {"target_track_info": {"track_mode": v}},
        },
        "target_track",
    ),
    SelectFeature(
        "scene_mode",
        "Image scene mode",
        "mdi:image-filter-hdr",
        ("image", "switch", "image_scene_mode"),
        ["normal", "street", "indoor", "low_illumination"],
        lambda v: {
            "method": "set",
            "image": {"switch": {"image_scene_mode": v}},
        },
        "image_capability",
    ),
    SelectFeature(
        "ir_type",
        "IR lamp mode",
        "mdi:spotlight-beam",
        ("image", "common", "inf_type"),
        ["auto", "on", "off"],
        lambda v: {"method": "set", "image": {"common": {"inf_type": v}}},
        "image_capability",
    ),
    SelectFeature(
        "white_lamp_type",
        "White lamp mode",
        "mdi:lightbulb-fluorescent-tube",
        ("image", "common", "wtl_type"),
        ["auto", "on", "off"],
        lambda v: {"method": "set", "image": {"common": {"wtl_type": v}}},
        "image_capability",
    ),
    SelectFeature(
        "alarm_type",
        "Alarm sound type",
        "mdi:bell-ring",
        ("msg_alarm", "chn1_msg_alarm_info", "alarm_type"),
        ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"],
        lambda v: {
            "method": "set",
            "msg_alarm": {"chn1_msg_alarm_info": {"alarm_type": str(v)}},
        },
    ),
]


NUMBERS: list[NumberFeature] = [
    NumberFeature(
        "motion_digital_sensitivity",
        "Motion digital sensitivity",
        "mdi:gauge",
        ("motion_detection", "motion_det", "digital_sensitivity"),
        0,
        100,
        1,
        lambda v: {
            "method": "set",
            "motion_detection": {
                "motion_det": {"digital_sensitivity": str(int(v))}
            },
        },
        "video_detection_digital_sensitivity",
    ),
    NumberFeature(
        "people_sensitivity",
        "People detection sensitivity",
        "mdi:gauge",
        ("people_detection", "detection", "sensitivity"),
        0,
        100,
        1,
        lambda v: {
            "method": "set",
            "people_detection": {"detection": {"sensitivity": str(int(v))}},
        },
        "smart_detection",
    ),
    NumberFeature(
        "track_back_time",
        "Tracking return time",
        "mdi:timer-outline",
        ("target_track", "target_track_info", "back_time"),
        0,
        300,
        1,
        lambda v: {
            "method": "set",
            "target_track": {
                "target_track_info": {"back_time": str(int(v))}
            },
        },
        "target_track",
        "s",
    ),
    NumberFeature(
        "wtl_intensity",
        "White lamp intensity",
        "mdi:brightness-6",
        ("image", "switch", "wtl_intensity_level"),
        1,
        5,
        1,
        lambda v: {
            "method": "set",
            "image": {"switch": {"wtl_intensity_level": str(int(v))}},
        },
        "image_capability",
    ),
    NumberFeature(
        "luma",
        "Brightness",
        "mdi:brightness-5",
        ("image", "common", "luma"),
        0,
        100,
        1,
        lambda v: {
            "method": "set",
            "image": {"common": {"luma": str(int(v))}},
        },
        "image_capability",
    ),
    NumberFeature(
        "contrast",
        "Contrast",
        "mdi:contrast-circle",
        ("image", "common", "contrast"),
        0,
        100,
        1,
        lambda v: {
            "method": "set",
            "image": {"common": {"contrast": str(int(v))}},
        },
        "image_capability",
    ),
    NumberFeature(
        "saturation",
        "Saturation",
        "mdi:palette",
        ("image", "common", "saturation"),
        0,
        100,
        1,
        lambda v: {
            "method": "set",
            "image": {"common": {"saturation": str(int(v))}},
        },
        "image_capability",
    ),
    NumberFeature(
        "sharpness",
        "Sharpness",
        "mdi:image-filter-black-white",
        ("image", "common", "sharpness"),
        0,
        100,
        1,
        lambda v: {
            "method": "set",
            "image": {"common": {"sharpness": str(int(v))}},
        },
        "image_capability",
    ),
    NumberFeature(
        "chroma",
        "Chroma",
        "mdi:invert-colors",
        ("image", "common", "chroma"),
        0,
        100,
        1,
        lambda v: {
            "method": "set",
            "image": {"common": {"chroma": str(int(v))}},
        },
        "image_capability",
    ),
    NumberFeature(
        "mic_volume",
        "Microphone volume",
        "mdi:microphone",
        ("audio_config", "microphone", "volume"),
        0,
        100,
        1,
        lambda v: {
            "method": "set",
            "audio_config": {"microphone": {"volume": int(v)}},
        },
        "audio_config",
        unit="%",
    ),
    NumberFeature(
        "speaker_volume",
        "Speaker volume",
        "mdi:volume-high",
        ("audio_config", "speaker", "volume"),
        0,
        100,
        1,
        lambda v: {
            "method": "set",
            "audio_config": {"speaker": {"volume": int(v)}},
        },
        "audio_config",
        unit="%",
    ),
]

# Poll payloads (APK-aligned). Failed modules are ignored per device.
POLL_REQUESTS: list[dict[str, Any]] = [
    {"method": "get", "function": {"name": ["module_spec"]}},
    {"method": "get", "device_info": {"name": ["basic_info"]}},
    {"method": "get", "lens_mask": {"name": ["lens_mask_info"]}},
    {"method": "get", "led": {"name": ["config"]}},
    {"method": "get", "motion_detection": {"name": ["motion_det"]}},
    {"method": "get", "people_detection": {"name": ["detection"]}},
    {"method": "get", "tamper_detection": {"name": ["tamper_det"]}},
    {"method": "get", "intrusion_detection": {"name": ["detection"]}},
    {"method": "get", "linecrossing_detection": {"name": ["detection"]}},
    {"method": "get", "msg_push": {"name": ["chn1_msg_push_info"]}},
    {"method": "get", "msg_alarm": {"name": ["chn1_msg_alarm_info"]}},
    {"method": "get", "target_track": {"name": ["target_track_info"]}},
    {"method": "get", "cover": {"name": ["cover"]}},
    {"method": "get", "record_plan": {"name": ["chn1_channel"]}},
    {
        "method": "get",
        "record_plan": {
            "name": [
                "chn1_channel",
                "chn2_channel",
                "chn3_channel",
                "chn4_channel",
            ]
        },
    },
    {
        "method": "get",
        "harddisk_manage": {"table": ["hd_info"], "name": ["harddisk"]},
    },
    {"method": "get", "chm": {"table": ["chn_info"]}},
    {"method": "get", "cet": {"name": ["media_encrypt", "rtsp", "vhttpd"]}},
    {"method": "get", "image": {"name": ["switch", "common"]}},
    {"method": "get", "video": {"name": ["main", "minor"]}},
    {"method": "get", "network": {"name": ["wan_status"]}},
    {"method": "get", "wlan": {"name": ["default_ap"]}},
    {"method": "get", "ptz": {"name": ["capability"]}},
    {"method": "get", "preset": {"name": ["preset"]}},
    {"method": "get", "system": {"name": ["basic", "sys", "clock_status"]}},
    # APK gaps filled in 0.10.x
    {"method": "get", "audio_config": {"name": ["microphone", "speaker"]}},
    {"method": "get", "timing_reboot": {"name": ["reboot"]}},
    {"method": "get", "msg_alarm_plan": {"name": ["chn1_msg_alarm_plan"]}},
    {"method": "get", "osd": {"name": ["label_info"]}},
    {"method": "get", "sd_manage": {"table": ["sd_info"], "name": ["sd"]}},
    {"method": "get", "battery": {"name": ["setting"]}},
    {"method": "get", "low_power": {"name": ["power_mode"]}},
    {"method": "get", "pir_detection": {"name": ["pir_det"]}},
    {"method": "get", "doorbell_ring": {"name": ["wakeup_status"]}},
    {"method": "get", "doorbell_capability": {"name": ["ring"]}},
]
