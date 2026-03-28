# Philips Sonicare BLE Integration for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/mtheli/philips_sonicare_ble)](https://github.com/mtheli/philips_sonicare_ble/releases)
[![License: MIT](https://img.shields.io/github/license/mtheli/philips_sonicare_ble)](LICENSE)

This is a custom component for Home Assistant to integrate **Philips Sonicare BLE toothbrushes**.

<p align="center">
  <img src="screenshots/ToothBrush1.png" alt="Toothbrush device page" width="800">
</p>

### Tested Models

| Model | Type | Direct BLE | ESP32 Bridge | Tested by |
| :--- | :--- | :---: | :---: | :--- |
| [**DiamondClean 9000 / HX992B**](https://www.usa.philips.com/c-p/HX9903_11/sonicare-diamondclean-smart-9300-sonic-electric-toothbrush-with-app/partsandaccessories) | Toothbrush | :white_check_mark: | | Maintainer |

Any BLE-enabled Philips Sonicare toothbrush should work (DiamondClean Smart, Expert Clean, Sonicare 6500/7100, 9900 Prestige, and more). The integration auto-discovers compatible devices via BLE. If you have a different model — happy to hear your test results!

The integration connects to your toothbrush via **Bluetooth Low Energy (BLE)** to provide battery status, brushing session data, brush head wear tracking, and more. All communication is fully local -- no cloud, no app required.

<!-- Two connection methods are supported:

1.  **Direct Bluetooth** -- connects from the HA host's Bluetooth adapter. Uses a persistent live connection with a poll fallback.
2.  **ESP32 BLE Bridge** -- an ESP32 running ESPHome acts as a wireless BLE relay. Ideal when the toothbrush is out of Bluetooth range of the HA host. -->

---

## Screenshots

<details>
<summary>Toothbrush sensors & diagnostics</summary>

![Toothbrush sensors](screenshots/ToothBrush2.png)

</details>

<details>
<summary>Integration overview</summary>

![Integration overview](screenshots/DeviceOverview.png)

</details>

<details>
<summary>Brush head device</summary>

![Brush head device](screenshots/BrushHead.png)

</details>

---

## Features

### Main Status
| Entity | Type | Description |
| :--- | :--- | :--- |
| **Handle State** | Sensor | Current state (`Off`, `Standby`, `Running`, `Charging`, `Shutdown`). |
| **Activity** | Sensor | Composite state derived from handle + brushing state (`Off`, `Standby`, `Brushing`, `Paused`, `Charging`). |
| **Brushing State** | Sensor | Detailed brushing status (`Off`, `On`, `Pause`, `Session Complete`, `Session Aborted`). |
| **Brushing Mode** | Sensor | Active cleaning mode (`Clean`, `White+`, `Gum Health`, `Deep Clean+`, `Sensitive`, `Tongue Care`). |
| **Intensity** | Sensor | Current intensity level (`Low`, `Medium`, `High`). |
| **Battery Level** | Sensor | Battery charge level (`%`). |
| **Brushing** | Binary Sensor | Indicates if actively brushing. |
| **Charging** | Binary Sensor | Indicates if currently charging. |
| **Pressure Alert** | Binary Sensor | Indicates if too much pressure is applied (during brushing). |

### Controls
| Entity | Type | Description |
| :--- | :--- | :--- |
| **Brushing Mode** | Select | Set the brushing mode for the next session. Shows only modes available on your device. **Disabled by default** -- see [Known Issues](#known-issues). |

### Sensor Data (live during brushing)

These sensors are only available while actively brushing and stream live data from the toothbrush IMU.

| Entity | Type | Description |
| :--- | :--- | :--- |
| **Pressure** | Sensor | Brushing pressure force (`g`). |
| **Pressure State** | Sensor | Pressure classification (`No Contact`, `Optimal`, `Too High`). |
| **Temperature** | Sensor | Handle temperature (`°C`). |

### Brushing Session
| Entity | Type | Description |
| :--- | :--- | :--- |
| **Brushing Time** | Sensor | Current session brushing time (seconds). |
| **Routine Length** | Sensor | Target brushing duration (typically 120s). |
| **Session ID** | Sensor | Current brushing session identifier. |
| **Latest Session ID** | Sensor | Most recently completed session identifier. |
| **Session Count** | Sensor | Total number of stored sessions. |

### Brush Head
| Entity | Type | Description |
| :--- | :--- | :--- |
| **Brush Head Wear** | Sensor | Brush head wear level (`%`, computed from usage/lifetime limit). |
| **Brush Head Usage** | Sensor | Accumulated brush head usage counter. |
| **Brush Head Limit** | Sensor | Maximum brush head lifetime. |
| **Brush Head Type** | Sensor | Brush head type (`Adaptive Clean`, `Adaptive White`, `Tongue Care`, `Adaptive Gums`, `Sensitive`). |
| **Brush Head Serial** | Sensor | Brush head serial number (from NFC tag). |
| **Brush Head Date** | Sensor | Brush head manufacturing date. |
| **Brush Head Ring ID** | Sensor | Color ring identifier (for family brush head tracking). |
| **Brush Head NFC Version** | Sensor | NFC chip version on the brush head. |
| **Brush Head Payload** | Sensor | Raw NFC payload data (hex). |

### Diagnostics
| Entity | Type | Description |
| :--- | :--- | :--- |
| **Motor Runtime** | Sensor | Cumulative motor runtime (seconds). |
| **Handle Time** | Sensor | Total handle operating time since manufacture (seconds). |
| **Model Number** | Sensor | Device model number (e.g., HX992B). |
| **Firmware** | Sensor | Installed firmware version. |
| **Last Seen** | Sensor | Timestamp of last successful data read. |
| **RSSI** | Sensor | BLE signal strength in dBm (Direct BLE only). |
| **Bridge Version** | Sensor | ESP bridge firmware version (ESP Bridge only). |

---

## Dashboard Card

For a visual brushing dashboard, use the [**Toothbrush Card**](https://github.com/mtheli/toothbrush-card) -- a custom Lovelace card with live sector tracking, pressure display, and brush head wear indicator. Works with both Philips Sonicare and Oral-B toothbrushes.

<p align="center">
  <img src="screenshots/Card.png" alt="Toothbrush Card with Sonicare" width="400">
</p>

---

## Prerequisites

* A compatible Philips Sonicare toothbrush (see [Tested Models](#tested-models) above).
* A Home Assistant instance with the **Bluetooth integration** enabled and a working Bluetooth adapter.
* **No pairing required** -- the Sonicare uses open GATT without BLE bonding. Simply close any Sonicare phone app to free the BLE connection.

> **Note:** The toothbrush only advertises via BLE for a short time after being picked up from the charger or turned on/off. It enters deep sleep after approximately 20 seconds of inactivity. While on the charging stand, it is **not reachable** via BLE.

---

## Installation

### HACS (Recommended)

1.  Go to **HACS** > **Integrations** in your Home Assistant UI.
2.  Click the three-dot menu in the top right and select **Custom repositories**.
3.  Add `https://github.com/mtheli/philips_sonicare_ble` and select the category **Integration**.
4.  Find the "Philips Sonicare" integration and click **Install**.
5.  Restart Home Assistant.

### Manual Installation

1.  Copy the `custom_components/philips_sonicare_ble` directory from this repository into your Home Assistant `config/custom_components/` folder.
2.  Restart Home Assistant.

---

## Configuration

### Automatic Discovery

1.  Wake up your toothbrush (pick it up from the charger or briefly turn it on).
2.  Navigate to **Settings > Devices & Services**.
3.  The toothbrush should appear under **Discovered** -- click **Configure**.
4.  Click **Submit**. The integration connects and reads device information.

### Manual Setup

1.  Click **+ Add Integration** and search for "**Philips Sonicare**".
2.  Enter the BLE MAC address of your toothbrush.

### Options

| Option | Default | Description |
| :--- | :--- | :--- |
| Poll Interval | 60s | How often to poll when live connection is unavailable (30-300s). |
| Live Updates | Enabled | Use BLE notifications for real-time updates during brushing. |
| Pressure Sensor | Enabled | Stream live pressure data during brushing. |
| Temperature Sensor | Enabled | Stream live temperature data during brushing. |
| Gyroscope Sensor | Disabled | Stream live 6-axis IMU data during brushing (experimental). |
| Notify Throttle | 500ms | Minimum interval between BLE notification updates (ESP Bridge only, 100-5000ms). |

---

## How It Works

### Connection Behavior

The Sonicare toothbrush has unique BLE behavior compared to other Philips devices:

* **Slow advertising** -- the toothbrush sends BLE advertisements only every 10-30 seconds (most BLE devices: every 100-500ms).
* **Short wake window** -- after turning off, the toothbrush stays connectable for only ~20 seconds before entering deep sleep.
* **No pairing** -- unlike Philips Shavers, the Sonicare uses open GATT without BLE bonding.

The integration handles this with:

1. **Advertisement-triggered reconnect** -- a BLE advertisement callback immediately wakes the connection thread, eliminating unnecessary backoff delays.
2. **Subscribe-first pattern** -- after connecting, BLE notification subscriptions are established immediately (before reading data). This keeps the connection alive because the toothbrush stays awake as long as active subscriptions exist.
3. **Smart lock management** -- the polling fallback yields to the live monitoring thread when an advertisement is detected, preventing connection contention.

### Data Flow

```
Toothbrush wakes up
    --> BLE Advertisement detected by HA
    --> Integration connects (~6s BLE stack overhead)
    --> Subscribe to 13 notification characteristics (~1s)
    --> Read all characteristics (~3s)
    --> Live updates flow until toothbrush sleeps
    --> Disconnect detected --> wait for next advertisement
```

---

## BLE Protocol

For a detailed technical description of the BLE protocol including all service UUIDs, characteristic reference, data formats, and enum values, see **[PROTOCOL.md](PROTOCOL.md)**.

The protocol was documented through BLE analysis and verified against a real HX992B device.

---

## Known Issues

* **Brushing Mode Select has no effect**: On BrushSync-enabled models (e.g. DiamondClean Smart HX992B), the toothbrush accepts BLE mode writes at the GATT level but ignores them on the firmware level. The brushing mode is determined by the attached brush head (BrushSync) or the physical button. The Select entity is disabled by default. If you have a non-BrushSync model where mode writes work, please open an issue.

---

## Troubleshooting

* **Toothbrush not discovered**: Wake it up by picking it up from the charger or briefly turning it on. The toothbrush is not reachable via BLE while on the charging stand.
* **Slow connection**: The toothbrush advertises every 10-30 seconds. The integration connects as soon as the first advertisement is received, but the BLE stack adds ~6 seconds overhead.
* **Connection drops quickly**: This is normal when the toothbrush is idle. It sleeps after ~20 seconds. The integration will reconnect automatically on the next wake.
* **Phone app conflict**: The toothbrush supports only one BLE connection. Close or uninstall the Sonicare phone app if you experience connection issues.

---

## Disclaimer

This is an independent community project and is not affiliated with, endorsed by, or sponsored by Philips. All product names, trademarks, and registered trademarks are property of their respective owners.

## License

[MIT](LICENSE)
