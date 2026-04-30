# ESP32 BLE Bridge Setup Guide

This guide explains how to set up an ESP32 as a Bluetooth Low Energy (BLE) bridge
for the Philips Sonicare Home Assistant integration. The ESP32 connects to the
toothbrush via BLE and relays data to Home Assistant over WiFi, removing the need
for direct Bluetooth access from the HA host.

> [!IMPORTANT]
> This is a **dedicated ESPHome component**, not a standard
> [ESPHome Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html).
> The bridge provides stable bonding, notification throttling, and multi-device
> support; `bluetooth_proxy` is available as a less stable fallback — see
> [Option C: Bluetooth Proxy](../README.md#option-c-bluetooth-proxy) in the
> main README. Both can run on the same ESP32; if you enable both, apply the
> [Bluedroid NULL-check patch](../esphome/README.md#bluedroid-null-check-patch-bluedroid_null_fixpy).

## Tested Hardware

| Board | Status |
|-------|--------|
| [M5Stack Atom Lite](https://docs.m5stack.com/en/core/ATOM%20Lite) (ESP32-PICO) | Confirmed working (used by maintainer) |
| Generic ESP32-DevKit | Should work (same SoC) |
| ESP32-S3 / ESP32-C3 | Should work — BLE stack is compatible |

## Prerequisites

- **ESP32 board** — see [Tested Hardware](#tested-hardware) above
- **ESPHome** — installed as Home Assistant add-on or standalone
- **Philips Sonicare toothbrush** — see [Tested Models](../README.md#tested-models)

## Step 1: Find Your Toothbrush's MAC Address

The toothbrush advertises via BLE for ~20 seconds after being picked up from the
charger or turned on/off. It advertises as "Philips Sonicare" with a MAC starting
with `24:E5:AA:`.

**Option A — Home Assistant Bluetooth:**
1. Go to **Settings > Devices & Services > Bluetooth**
2. Look for a device named "Philips Sonicare"
3. Note the MAC address (e.g. `24:E5:AA:14:9B:86`)

**Option B — nRF Connect (Android/iOS):**
1. Open the [nRF Connect](https://www.nordicsemi.com/Products/Development-tools/nRF-Connect-for-mobile) app and scan for devices
2. Filter for "Philips" — the toothbrush shows up with its MAC address

**Option C — ESPHome logs:**
1. Deploy any ESP32 with `esp32_ble_tracker` enabled
2. Check logs for `Found device ... Name: 'Philips Sonicare'`

## Step 2: Create the ESPHome Configuration

Use the template [`esphome/esp32-generic.yaml`](../esphome/esp32-generic.yaml) as a
starting point. Copy it to your ESPHome configuration directory and customize.

If you already have an ESP32 running other components (e.g. `ble_adv_proxy`,
`bluetooth_proxy`), you can add the Sonicare component to your existing config
instead of using the template.

### Required changes

1. **Toothbrush MAC address** — replace `XX:XX:XX:XX:XX:XX` with your toothbrush's MAC:
   ```yaml
   ble_client:
     - mac_address: "XX:XX:XX:XX:XX:XX"   # <-- your toothbrush's MAC
   ```

2. **Board type** — change `esp32dev` if needed:
   ```yaml
   esp32:
     board: esp32dev   # or esp32-s3-devkitc-1, m5stack-atoms3, etc.
   ```

3. **Secrets** — create or update your `secrets.yaml` with:
   ```yaml
   api_encryption_key: "<generate with `esphome wizard`>"
   ota_password: "<your OTA password>"
   wifi_ssid: "<your WiFi SSID>"
   wifi_password: "<your WiFi password>"
   fallback_password: "<fallback AP password>"
   ```

### What you should NOT change

- **Framework**: must be `esp-idf` (not Arduino) — the Arduino framework uses a
  precompiled Bluetooth library (`libbt.a`) that limits notification subscriptions
  to 5 (`BTA_GATTC_NOTIF_REG_MAX=5`). The Sonicare needs 11+ concurrent
  subscriptions. With ESP-IDF, this limit is configurable via `sdkconfig_options`
- **sdkconfig options**: `CONFIG_BT_GATTC_MAX_CACHE_CHAR: "80"` and
  `CONFIG_BT_GATTC_NOTIF_REG_MAX: "20"` — the Sonicare has ~45 GATT attributes
  and we subscribe to 11+ characteristics
- **API flags**: `custom_services: true` and `homeassistant_services: true` — required
  for the bridge component to register its services
- **external_components**: the component is loaded directly from this GitHub repository.
  The `refresh: 0s` setting ensures the latest code is fetched on every build

### Minimal config snippet

If you're adding this to an existing ESPHome config, these are the blocks you need:

```yaml
esp32:
  framework:
    type: esp-idf
    sdkconfig_options:
      CONFIG_BT_GATTC_MAX_CACHE_CHAR: "80"
      CONFIG_BT_GATTC_NOTIF_REG_MAX: "20"

api:
  custom_services: true
  homeassistant_services: true

esp32_ble:
  io_capability: none
  max_notifications: 20

esp32_ble_tracker:
  scan_parameters:
    active: true

external_components:
  - source:
      type: git
      url: https://github.com/mtheli/philips_sonicare_ble
      ref: master
      path: esphome/components
    components: [philips_sonicare]
    refresh: 0s

ble_client:
  - mac_address: "XX:XX:XX:XX:XX:XX"
    id: philips_sonicare_ble
    auto_connect: true
    on_connect:
      then:
        - logger.log: "Connected to Sonicare"
    on_disconnect:
      then:
        - logger.log: "Disconnected from Sonicare"

philips_sonicare:
  ble_client_id: philips_sonicare_ble
  connected:
    name: "Sonicare Connected"
```

## Step 3: Flash the ESP32

1. Open the **ESPHome Dashboard** in Home Assistant
2. Add a new device or upload your customized YAML
3. Click **Install** and choose your flashing method:
   - USB for first-time flash
   - OTA for subsequent updates
4. Wait for the build and flash to complete

> [!NOTE]
> Switching between Arduino and ESP-IDF framework requires a full clean build
> ("Clean Build Files" in the ESPHome dashboard before flashing).

## Step 4: Verify BLE Connection

After flashing, check the ESPHome device logs for a successful connection sequence.
Make sure the toothbrush is awake (pick it up from the charger or press the button).

```
[D][esp32_ble_tracker:726]:   Name: 'Philips Sonicare'
[I][esp32_ble_client:111]: [0] [24:E5:AA:xx:xx:xx] 0x01 Connecting
[I][esp32_ble_client:326]: [0] [24:E5:AA:xx:xx:xx] Connection open
[I][philips_sonicare:061]: Connected to Sonicare
[I][esp32_ble_client:435]: [0] [24:E5:AA:xx:xx:xx] Service discovery complete
```

> [!NOTE]
> Most Sonicare models (DiamondClean Smart HX992X) use **open GATT without
> bonding** — no `auth success` line is expected. Some models (ExpertClean
> HX962X, Prestige HX999X) require BLE pairing — you will see `auth success`
> in the logs after a successful connection.

The toothbrush only stays awake for ~20 seconds after waking up. Once connected
with active subscriptions, the connection keeps the toothbrush awake indefinitely.

## Step 5: Add the Integration in Home Assistant

1. Install the **Philips Sonicare** integration in Home Assistant (via [HACS](../README.md#installation) or manually)
2. Go to **Settings > Devices & Services > + Add Integration** and search for **Philips Sonicare**
3. Select **"ESP32 Bridge"** and pick your ESP32 device from the list
4. The **Bridge Status** page shows bridge health: component version, BLE connection state, and toothbrush MAC address — verify everything looks good and click **Submit**
5. Wake the toothbrush if it has gone back to sleep
6. The integration reads the toothbrush capabilities and GATT services via the bridge
7. Review the detected capabilities and click **Submit** to finish

## Bridge vs. bluetooth_proxy

The standard ESPHome `bluetooth_proxy` transparently relays BLE connections
from Home Assistant through the ESP32. With the [Bluedroid NULL-check
patch](../esphome/README.md#bluedroid-null-check-patch-bluedroid_null_fixpy)
and ESPHome 2026.2+, it works for a single Sonicare — but it has real
drawbacks:

- **No notification throttling** — the full BLE stream goes over WiFi.
  With multiple Sonicares on one proxy, this overloads the WiFi socket
  queue.
- **Silent-connection failures** in multi-device setups — a brush can
  stop delivering notifications while the proxy stays reachable,
  requiring a Home Assistant integration reload to recover.
- **Bond keys live on the proxy's NVS** — first connect after reboot
  races service discovery against re-encryption (~5 s extra delay).

This dedicated Bridge component manages the GATT connection directly on
the ESP32 with its own pairing stack, notification throttle, and
per-device subscription state — avoiding all three issues.

## Troubleshooting

### ESP32 crashes/reboots when connecting

If `bluetooth_proxy:` is enabled in your ESPHome config, the ESP32 crashes
in the Bluedroid GATT cache during service discovery with
`Fault - LoadProhibited / bta_gattc_cache_save`. Apply the
[Bluedroid NULL-check patch](../esphome/README.md#bluedroid-null-check-patch-bluedroid_null_fixpy)
to fix this. If you don't need `bluetooth_proxy`, disabling it also
resolves the crash.

### "No philips_sonicare services found"

Make sure your ESPHome config includes `custom_services: true` and
`homeassistant_services: true` under the `api:` section. These flags are required
since ESPHome 2025.7.0.

### "No ESPHome devices found" in HA config flow

- The ESP32 must be fully set up and connected to Home Assistant via the ESPHome
  integration first
- Check **Settings > Devices & Services > ESPHome** — your device should be listed there
- If using a fresh ESPHome install, wait for the device to come online after flashing

### Toothbrush not connecting

- The toothbrush only advertises for ~20 seconds after waking up. Pick it up from
  the charger or press the button, then check the ESP32 logs for `Connected to Sonicare`
- The toothbrush supports only one BLE connection. Close or uninstall the Sonicare
  phone app if the ESP32 can't connect
- The toothbrush is **not reachable** via BLE while on the charging stand

### No data after OTA update

After an OTA flash, the ESP32 reboots and reconnects to the toothbrush via BLE before
Home Assistant re-establishes the API stream (~5-10 seconds). The bridge automatically
re-fires the "ready" event every 15 seconds until HA subscribes to notifications.
If data still doesn't flow:

- **Reload the integration** in HA (Settings > Devices & Services > Philips Sonicare > ... > Reload)
- Check ESPHome logs for `BLE connected, no subscriptions — re-firing ready`
- Check HA logs for `ESP bridge rebooted — forcing re-setup`

## Architecture

```
┌─────────────┐   BLE    ┌─────────┐  WiFi/API  ┌─────────────────┐
│ Toothbrush  │◄────────►│  ESP32  │◄──────────►│ Home Assistant   │
│             │   open   │  Bridge │  ESPHome   │ Philips Sonicare │
│             │   GATT   │         │  services  │ Integration      │
└─────────────┘          └─────────┘            └─────────────────┘
```

- **ESP32 → HA**: fires `esphome.philips_sonicare_ble_data` events with characteristic
  UUID and hex payload
- **HA → ESP32**: calls ESPHome services (`ble_read_char`, `ble_subscribe`,
  `ble_write_char`, `ble_unsubscribe`, `ble_get_info`) with service and characteristic UUIDs
- **Heartbeat**: the bridge sends a `heartbeat` status event every 15 seconds with
  BLE connection state, MAC address, and component version

---

## Events

The bridge publishes two event types on the Home Assistant event bus. Both can be
watched live in **Developer Tools → Events** by entering the event name and clicking
**Start Listening**.

### `esphome.philips_sonicare_ble_data`

GATT-side traffic — notifications, read results, and read errors.

| Field        | When set                            | Example                                  |
|--------------|-------------------------------------|------------------------------------------|
| `mac`        | Always (the brush MAC)              | `24:E5:AA:14:9B:86`                      |
| `uuid`       | Characteristic UUID                 | `477ea600-a260-11e4-ae37-0002a5d54010`   |
| `payload`    | Hex-encoded bytes (notify or read)  | `02`                                     |
| `error`      | Read failure reason (mutually exclusive with `payload`) | `auth_required` |
| `bridge_id`  | Multi-device setups — identifies which bridge fired the event | `prestige` |

### `esphome.philips_sonicare_ble_status`

Bridge lifecycle — heartbeats, info responses, ready/connected/disconnected
transitions. The exact field set depends on the value of `status`.

| `status`        | Meaning                                                     | Additional fields                                |
|-----------------|-------------------------------------------------------------|--------------------------------------------------|
| `heartbeat`     | Periodic keep-alive (every ~15 s)                           | `version`, `uptime_s`, `ble_connected`, `mac`    |
| `info`          | Response to a `ble_get_info` service call                   | `version`, `uptime_s`, `mac`, `identity_address`, `paired`, `ble_connected`, `mode`, `pair_capable`, `pair_mode_active`, `bridge_id` |
| `ready`         | GATT discovery complete on the brush                        | `mac`, `bridge_id`, `version`, `uptime_s`        |
| `connected`     | BLE link came up (before service discovery completes)       | `mac`, `bridge_id`                               |
| `disconnected`  | BLE link dropped                                            | `mac`, `bridge_id`                               |
| `pair_complete` | Pairing succeeded (Mode-B / Open-GATT)                      | `mac`, `bonding`, `model`, `ble_name`, `bridge_id` |
| `pair_timeout`  | Pair-mode window expired without success                    | `bridge_id`                                      |
| `scan_result`   | One advertised device seen during `ble_scan`                | `mac`, `name`, `rssi`, `bridge_id`               |

**Filtering tip**: in multi-bridge setups, every event carries a `bridge_id` field
(`""` for legacy single-bridge configs). To watch a single device, filter on
`bridge_id == "<your_id>"` in your automation/script. The integration's
`EspBridgeTransport` does this internally.

**Heartbeat-driven restart detection**: the integration tracks `uptime_s`
across heartbeats; a regression flags an ESP restart and triggers BLE
re-subscription. So if you spot the bridge boot-time sensor jumping in HA,
that is what the heartbeat told us.

---

## Multi-Device Setup

A single ESP32 can bridge **multiple** Sonicare toothbrushes. Each device needs its own `ble_client` and `philips_sonicare` entry with a unique `bridge_id`:

```yaml
ble_client:
  - mac_address: "AA:BB:CC:DD:EE:01"
    id: sonicare_prestige
    auto_connect: true

  - mac_address: "AA:BB:CC:DD:EE:02"
    id: sonicare_kids
    auto_connect: true

philips_sonicare:
  - ble_client_id: sonicare_prestige
    bridge_id: prestige

  - ble_client_id: sonicare_kids
    bridge_id: kids
```

The `bridge_id` is **required** when using multiple instances — the ESP will refuse to compile without it. It serves as a suffix for service names (e.g., `ble_read_char_prestige`) so HA can address each device separately.

A full example is available in [`esphome/atom-lite-dual.yaml`](../esphome/atom-lite-dual.yaml).

> [!NOTE]
> Each toothbrush should only be connected via **one** path — either Direct BLE or ESP Bridge, not both simultaneously.
