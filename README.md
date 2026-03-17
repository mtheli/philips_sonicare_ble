# Philips Sonicare BLE Integration for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/mtheli/philips_sonicare_ble)](https://github.com/mtheli/philips_sonicare_ble/releases)
[![License: MIT](https://img.shields.io/github/license/mtheli/philips_sonicare_ble)](LICENSE)

This is a custom component for Home Assistant to integrate **Philips Sonicare BLE toothbrushes**.

### Tested Models

| Model | Type | Direct BLE | ESP32 Bridge | Tested by |
| :--- | :--- | :---: | :---: | :--- |
| [**DiamondClean 9000 / HX992X**](https://www.philips.com/c-p/HX9911_09/diamondclean-9000-sonic-electric-toothbrush-with-app) | Toothbrush | :white_check_mark: | | Maintainer |

Other BLE-enabled Philips Sonicare toothbrushes using the Legacy protocol (service UUID `477ea600-a260-11e4-ae37-0002a5d50001`) should also work. The integration auto-discovers compatible devices via BLE.

The integration connects to your toothbrush via **Bluetooth Low Energy (BLE)** to provide battery status, brushing session data, brush head wear tracking, and more. All communication is fully local -- no cloud, no app required.

Two connection methods are supported:

1.  **Direct Bluetooth** -- connects from the HA host's Bluetooth adapter. Uses a persistent live connection with a poll fallback.
2.  **ESP32 BLE Bridge** -- an ESP32 running ESPHome acts as a wireless BLE relay. Ideal when the toothbrush is out of Bluetooth range of the HA host.

---

## Features

### Main Status
| Entity | Type | Description |
| :--- | :--- | :--- |
| **Handle State** | Sensor | Current state (`Off`, `Standby`, `Running`, `Charging`, `Shutdown`). |
| **Brushing State** | Sensor | Detailed brushing status (`Off`, `On`, `Pause`, `Session Complete`, `Session Aborted`). |
| **Brushing Mode** | Sensor | Active cleaning mode (`Clean`, `White+`, `Gum Health`, `Deep Clean+`). |
| **Intensity** | Sensor | Current intensity level (`Low`, `Medium`, `High`). |
| **Battery Level** | Sensor | Battery charge level (`%`). |
| **Brushing** | Binary Sensor | Indicates if actively brushing. |
| **Charging** | Binary Sensor | Indicates if currently charging. |

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
| **Brush Head Serial** | Sensor | Brush head serial number (from NFC tag). |
| **Brush Head Date** | Sensor | Brush head manufacturing date. |
| **Brush Head Ring ID** | Sensor | Color ring identifier (for family brush head tracking). |

### Diagnostics
| Entity | Type | Description |
| :--- | :--- | :--- |
| **Motor Runtime** | Sensor | Cumulative motor runtime (seconds). |
| **Model Number** | Sensor | Device model number (e.g., HX992X). |
| **Firmware** | Sensor | Installed firmware version. |
| **Last Seen** | Sensor | Timestamp of last successful data read. |

---

## Prerequisites

* A compatible Philips Sonicare toothbrush (see [Tested Models](#tested-models) above).
* A Home Assistant instance with the **Bluetooth integration** enabled and a working Bluetooth adapter.
* **No pairing required** -- the Sonicare uses open GATT without BLE bonding. Simply close any Sonicare phone app to free the BLE connection.

> **Note:** The toothbrush only advertises via BLE when it is awake -- on the charging stand, or shortly after being turned on/off. It enters deep sleep after approximately 20 seconds of inactivity.

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

1.  Wake up your toothbrush (place it on the charger or briefly turn it on).
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

The protocol was documented through BLE analysis and verified against a real HX992X device.

---

## Troubleshooting

* **Toothbrush not discovered**: Wake it up by placing it on the charger or briefly turning it on. It must be advertising for HA to detect it.
* **Slow connection**: The toothbrush advertises every 10-30 seconds. The integration connects as soon as the first advertisement is received, but the BLE stack adds ~6 seconds overhead.
* **Connection drops quickly**: This is normal when the toothbrush is idle. It sleeps after ~20 seconds. The integration will reconnect automatically on the next wake.
* **Phone app conflict**: The toothbrush supports only one BLE connection. Close or uninstall the Sonicare phone app if you experience connection issues.

---

## License

[MIT](LICENSE)
