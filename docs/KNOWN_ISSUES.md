# Known Issues

## bluetooth_proxy is not compatible with the Sonicare

**Status:** Confirmed — ESP-IDF Bluedroid bug, fix exists but not yet released

The standard ESPHome `bluetooth_proxy` cannot be used with Philips Sonicare
toothbrushes. The ESP32 crashes during GATT service discovery on every
connection attempt, regardless of framework. The same crash also occurs when
`bluetooth_proxy` runs alongside our `ble_client` component on the same ESP32 —
even if the proxy itself does not connect to the toothbrush.

### Symptoms

- ESP32 reboots every ~20 seconds in a loop
- HA log shows repeated `Processing unexpected disconnect from ESPHome API`
- Config flow shows "Could not connect to the toothbrush" after 7 attempts
- Config flow detects the Sonicare (Main) service via advertisement but all
  other services show red crosses (GATT connection never succeeds)

### Root cause

The crash occurs in `bta_gattc_cache_save` in the Bluedroid BLE stack during
GATT service discovery. During characteristic descriptor discovery, the
Sonicare returns an error response for some descriptors. The Bluedroid error
handling path calls `bta_gattc_disc_cmpl` → `bta_gattc_cache_save` with a
NULL `p_srcb` pointer, causing a `LoadProhibited` fault.

The crash only occurs when **multiple GATT client interfaces** (`gattc_if`)
are registered — which happens whenever `bluetooth_proxy` is present (it
registers 3 additional connection slots). With only our `ble_client`, a single
`gattc_if` is registered and the error response is handled gracefully via
`ESP_GATTC_SEARCH_CMPL_EVT`.

**ESP-IDF crash backtrace:**
```
Reason: Fault - LoadProhibited
PC: bta_gattc_cache_save at bta_gattc_cache.c:2118

bta_gattc_cache_save (line 2118)
  → bta_gattc_explore_srvc (line 638)
  → bta_gattc_char_dscpt_disc_cmpl (line 724)
  → bta_gattc_char_disc_cmpl (line 681)
  → gatt_end_operation (line 2280)
  → gatt_proc_disc_error_rsp (line 536)
  → gatt_process_error_rsp (line 571)
```

The Sonicare returns an error response during characteristic descriptor
discovery, and the Bluedroid stack crashes when trying to save the incomplete
discovery result to the GATT cache.

### Tested configurations

All tests on ESPHome 2026.3.1, M5Stack Atom Lite (ESP32 rev1.1), Sonicare
HX992B (`24:E5:AA:14:9B:86`).

| Setup | Framework | Result |
|-------|-----------|--------|
| `bluetooth_proxy` only | ESP-IDF | Crash (`bta_gattc_cache_save`) |
| `bluetooth_proxy` only | Arduino | Crash (same, less debug) |
| `bluetooth_proxy` + `ble_adv_proxy` | ESP-IDF | Crash (`bta_gattc_cache_save`) |
| `philips_sonicare` + `bluetooth_proxy` | ESP-IDF | Crash (`bta_gattc_cache_save`) |
| `philips_sonicare` + `bluetooth_proxy` (cache 160) | ESP-IDF | Crash (cache size irrelevant) |
| `philips_sonicare` only | ESP-IDF | **Works** |

### Related reports

- [Issue #1](https://github.com/mtheli/philips_sonicare_ble/issues/1): User
  with `ble_adv_proxy` + `bluetooth_proxy` saw `auth fail reason=97` before
  crash. Also crashed with our `ble_client` component when `ble_adv_proxy` and
  `bluetooth_proxy` were still present in the YAML.

### ESP-IDF fix (not yet released)

The root cause is a missing NULL pointer check in `bta_gattc_disc_cmpl()`.
This was fixed in ESP-IDF commit [`d4f3517`](https://github.com/espressif/esp-idf/commit/d4f3517da4a81144eaa3f091848e61ec68ab3700)
(2026-02-27) which adds `if (p_clcb->p_srcb == NULL)` checks in several
functions including `bta_gattc_disc_cmpl`, `bta_gattc_conn`, `bta_gattc_close`,
and `bta_gattc_sm_execute`.

**This fix is merged to ESP-IDF `master` but is not yet included in any
release tag** (not in 5.5.3, not in 5.5.4, not in 6.0). ESPHome 2026.3.2
uses ESP-IDF 5.5.x and does not have the fix.

Once a new ESP-IDF release includes this commit and ESPHome adopts it,
`bluetooth_proxy` should be able to coexist with our component without
the workaround below.

Related issues:
- [esphome/esphome#15783](https://github.com/esphome/esphome/issues/15783) —
  ESPHome issue requesting cherry-pick of the fix (filed 2026-04-16)
- [espressif/esp-idf#4971](https://github.com/espressif/esp-idf/issues/4971) —
  `LoadProhibited` in `bta_gattc_co_cache_find_src_addr` (open since 2020,
  fixed in same commit)

### Workaround

A pre-build patch script
([`bluedroid_null_fix.py`](../esphome/bluedroid_null_fix.py)) is included in
this repository. It applies the critical NULL pointer checks from ESP-IDF
commit `d4f3517` during the PlatformIO build. To use it:

1. Copy `bluedroid_null_fix.py` to your ESPHome config directory
   (typically `/config/esphome/`)
2. Add the following to your YAML under `esphome:`:
   ```yaml
   esphome:
     platformio_options:
       extra_scripts:
         - pre:/config/esphome/bluedroid_null_fix.py
   ```
3. Add `bluetooth_proxy:` to your YAML:
   ```yaml
   bluetooth_proxy:
     active: true
   ```
4. Perform a **clean build** ("Clean Build Files" in ESPHome) before flashing

The example YAMLs in this repository show the configuration (commented out).

### Without the workaround

Do **not** run `bluetooth_proxy` on the same ESP32 as the Sonicare component.
Use the [ESP32 BLE Bridge](ESP32_BRIDGE.md) component alone. If you need a
Bluetooth Proxy for other devices, run it on a **separate ESP32**.

---

## Notification subscription limit (default too low)

**Status:** Solved in our ESP32 Bridge component

The ESP32 Bluedroid BLE stack limits the number of concurrent GATT notification
subscriptions. The Sonicare requires 11+ subscriptions for full live data.

### Symptoms

- Some sensors never update (silently fail to subscribe)
- `E BT_APPL: Max Notification Reached, registration failed` in ESP32 logs
- Battery or basic status works but brushing data / pressure / brush head missing

### Root cause

| Config option | Default | Required |
|---------------|---------|----------|
| `CONFIG_BT_GATTC_NOTIF_REG_MAX` | 5 | 20 |
| `CONFIG_BT_GATTC_MAX_CACHE_CHAR` | 40 | 80 |

The Arduino framework uses a precompiled `libbt.a` where these values are
hardcoded and cannot be changed. The ESP-IDF framework allows configuration
via `sdkconfig_options`.

### Affected projects

All ESPHome YAML projects (iamjoshk, Andrecall, v6ak, edwardtfn, xuio) do
**not** set these options. They work around the limit by polling characteristics
on a timer (`set_update_interval`) instead of using GATT notifications. This
means they miss real-time updates during brushing.

### Solution

Use ESP-IDF framework with explicit `sdkconfig_options`:

```yaml
esp32:
  framework:
    type: esp-idf
    sdkconfig_options:
      CONFIG_BT_GATTC_MAX_CACHE_CHAR: "80"
      CONFIG_BT_GATTC_NOTIF_REG_MAX: "20"
```

Our [ESP32 Bridge](ESP32_BRIDGE.md) component requires ESP-IDF and documents
these settings.

---

## Brushing Mode Select has no effect on BrushSync models

**Status:** Confirmed, firmware limitation

On BrushSync-enabled models (e.g. DiamondClean Smart HX992B), the toothbrush
accepts BLE mode writes at the GATT level but ignores them on the firmware
level. The brushing mode is determined by the attached brush head (BrushSync)
or the physical button.

The Select entity is disabled by default. If you have a non-BrushSync model
where mode writes work, please open an issue.

---

## Toothbrush not reachable on charger

**Status:** By design (hardware behavior)

The Sonicare enters deep sleep while on the charging stand and does not
advertise via BLE. It only advertises for ~20 seconds after being picked up
from the charger or turned on/off.

This is not a bug — it is the toothbrush's power management behavior. The
integration reconnects automatically when the toothbrush wakes up.

---

## Unnecessary pair() calls in other ESPHome projects

**Status:** Informational

Some community ESPHome configs (notably Andrecall/esphome-sonicare) call
`pair()` or `esp_ble_set_encryption()` in the `on_connect` handler. This is
unnecessary on DiamondClean Smart (HX992X) models which use open GATT.
However, on models that require BLE bonding (ExpertClean HX962X, Prestige
HX999X), the `pair()` call is actually needed — Andrecall likely had
such a model.

The `pair()` call happens to be harmless when using `ble_client` directly
(the auth failure is handled gracefully), but it contributes to confusion
about whether pairing is required. It is not.
