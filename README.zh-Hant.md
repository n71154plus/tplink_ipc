# TP-Link IPC

Home Assistant 自訂整合，用來接 **TP-Link 中國區 IPC／NVR**。區域網走與 tpSurveillance 相同的 stok API，也可以改登中國區雲端帳號。

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Release](https://img.shields.io/github/v/release/n71154plus/tplink_ipc)](https://github.com/n71154plus/tplink_ipc/releases)

本專案與 TP-Link 無關。

## 安裝

需要 Home Assistant **2026.10** 或更新版本，並啟用 `stream` 與 `ffmpeg`。

1. 在 HACS 把這個網址加為自訂整合儲存庫：`https://github.com/n71154plus/tplink_ipc`
2. 下載 **TP-Link IPC**，然後重啟 Home Assistant。
3. 到 **設定 → 裝置與服務 → 新增整合 → TP-Link IPC**。

手動安裝：把 `custom_components/tplink_ipc` 複製到 Home Assistant 的 `config/custom_components/`，再重啟。

## 會建立什麼

- **區域網攝影機或 NVR。** 使用網頁登入（stok）與 RTSP。請先在攝影機網頁開啟 RTSP。NVR 歷史回放使用連接埠 8000。
- **即時畫面。** 主碼流與子碼流。NVR 會再為每個通道各建一台攝影機。
- **控制。** 裝置有提供時，會建立隱私、LED、移動偵測等開關。PTZ、預置點、重新啟動、錄影搜尋以服務提供。
- **雲端帳號（選用）。** 登入中國區 IPC 雲端（`n-wap-ipc`），為綁定裝置建立在線、儲存與控制實體。雲端即時影像仍要靠媒體 relay。

## go2rtc 名稱

`const.py` 裡的 `GO2RTC_LIVE_BY_HOST` 與 `GO2RTC_PLAYBACK_BY_HOST` 預設是空的。若要固定 go2rtc 串流名稱，填入 `主機 → 串流名稱`。沒填時，回放串流名稱會是 `tplink_pb_<主機>_ch<n>`。

## 圖示

Home Assistant 會讀 `custom_components/tplink_ipc/brand/icon.png`（256×256，另有 512×512 的 `@2x`）。
