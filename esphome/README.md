# ESP Bridge

Everything needed to turn an ESP32 into a Bluetooth bridge for the Philips
Sonicare integration. The ESP handles the BLE connection to the toothbrush
(including LE Secure Connections bonding for models that require it) and
exposes it to Home Assistant via ESPHome service calls and events.

Use this when Home Assistant itself has no Bluetooth adapter in range of
the toothbrush, when you want to monitor multiple brushes from one bridge,
or when you prefer a dedicated, always-connected bridge over the less
stable `bluetooth_proxy` path.

For end-to-end setup instructions (flashing, integration configuration,
multi-device setups) see [`docs/ESP32_BRIDGE.md`](../docs/ESP32_BRIDGE.md).

## Contents

| File | Description |
|------|-------------|
| [`components/philips_sonicare/`](components/philips_sonicare/) | The C++ ESPHome external component. This is the actual bridge implementation — BLE client, GATT read/write/subscribe, bonding, and the HA event/service interface. |
| [`atom-lite.yaml`](atom-lite.yaml) | Ready-to-flash config for the M5Stack Atom Lite, one toothbrush. Also serves as a `bluetooth_proxy` for other BLE devices in parallel (requires the Bluedroid fix below). |
| [`atom-lite-dual.yaml`](atom-lite-dual.yaml) | Same board, but configured for **two** Sonicares via one bridge. Raises `BTA_GATTC_NOTIF_REG_MAX` and `BTA_GATTC_MAX_CACHE_CHAR` accordingly. |
| [`esp32-generic.yaml`](esp32-generic.yaml) | Generic ESP32 dev-board config (`esp32dev`). Use as a starting point for other boards. |
| [`bluedroid_null_fix.py`](bluedroid_null_fix.py) | Compile-time patch — see next section. |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history for the external component. |

## Bluedroid NULL-check patch (`bluedroid_null_fix.py`)

> [!IMPORTANT]
> **If you enable `bluetooth_proxy:` in the same ESP config as this bridge,
> the ESP will crash on boot** with a `LoadProhibited` / `bta_gattc_cache_save`
> exception. This is an ESP-IDF bug (not specific to this integration) —
> tracked in [esphome#15783](https://github.com/esphome/esphome/issues/15783).

Until ESP-IDF **v5.5.5** is released (expected mid-May 2026, contains the
fix from ESP-IDF commit [`d4f3517`](https://github.com/espressif/esp-idf/commit/d4f3517)),
compile the bridge with the pre-build script in this directory:

```yaml
esphome:
  name: atom-lite
  platformio_options:
    extra_scripts:
      - pre:/config/esphome/bluedroid_null_fix.py
```

Place `bluedroid_null_fix.py` in your ESPHome config directory
(`/config/esphome/` on Home Assistant OS). The script is **idempotent** —
it won't patch twice, and it leaves a `[bluedroid-fix] Patched: ...` line
in the compile log so you can verify it ran. The line only appears at
**compile time**, not at runtime — check the "Install" output in the
ESPHome dashboard, not the device's live log.

Once ESP-IDF v5.5.5 ships, the `extra_scripts:` entry can be removed. The
script is harmless to leave in place — it becomes a no-op against patched
source.

**If you only run this bridge and no `bluetooth_proxy:`, the patch is not
required.**

## Requirements

- **ESP-IDF framework** (not Arduino). Arduino's precompiled Bluedroid has
  `BTA_GATTC_MAX_CACHE_CHAR=40` and `BTA_GATTC_NOTIF_REG_MAX=5`, both too
  small for the toothbrush's ~45 attributes and 11 subscriptions per device.
  The YAMLs set the larger limits via `sdkconfig_options`.
- An ESP32 within BLE range of the toothbrush (ideally RSSI better than
  -85 dBm). RSSI around -100 dBm is the noise floor — bonding will fail
  at that range on models that require it (ExpertClean, HX991M,
  DiamondClean Prestige).

## Version pinning

The integration enforces a minimum bridge version (`MIN_BRIDGE_VERSION`
in `custom_components/philips_sonicare_ble/const.py`). On version mismatch
Home Assistant shows a Repairs notification asking you to reflash. Always
flash a matching pair — the ref in your YAML's `external_components:`
should point at the same tag/commit as the integration version.
