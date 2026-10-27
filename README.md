# TP-Link IPC

Home Assistant custom integration for **TP-Link China IPC / NVR** cameras that speak the same local API as the tpSurveillance app, plus an optional China-region cloud account.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Release](https://img.shields.io/github/v/release/n71154plus/tplink_ipc)](https://github.com/n71154plus/tplink_ipc/releases)

> **繁體中文：** [README.zh-Hant.md](README.zh-Hant.md)

This project is not affiliated with TP-Link.

## Install

Requires Home Assistant **2026.10** or newer, with the `stream` and `ffmpeg` integrations.

1. In HACS, add this repository as a custom integration: `https://github.com/n71154plus/tplink_ipc`
2. Download **TP-Link IPC**, then restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → TP-Link IPC**.

Manual install: copy `custom_components/tplink_ipc` into your Home Assistant `config/custom_components/` directory and restart.

## What it sets up

- **LAN camera or NVR.** Uses the camera web login (stok) and RTSP. Enable RTSP on the camera first. NVR playback uses port 8000.
- **Live view.** Main stream and sub-stream camera entities. An NVR also gets one camera per channel.
- **Controls.** Privacy, LED, motion detection, and related switches when the device exposes them. PTZ move, preset, reboot, and recording search are available as services.
- **Cloud account (optional).** Signs in to the China IPC cloud (`n-wap-ipc`) and creates online, storage, and control entities for bound devices. Live cloud video still depends on the media relay.

## go2rtc names

`GO2RTC_LIVE_BY_HOST` and `GO2RTC_PLAYBACK_BY_HOST` in `const.py` are empty. Fill them with `host → stream name` if you want fixed go2rtc stream names. Otherwise playback streams are named `tplink_pb_<host>_ch<n>`.

## Brand icon

Home Assistant reads `custom_components/tplink_ipc/brand/icon.png` (256×256, with a 512×512 `@2x` variant).
