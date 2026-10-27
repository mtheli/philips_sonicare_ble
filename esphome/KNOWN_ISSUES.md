# ESP bridge known issues

Issues with the ESP32 bridge, ESPHome and ESP-IDF. Issues with Bluetooth adapters on the Home Assistant host and with the toothbrushes themselves are in the [general known issues](../docs/KNOWN_ISSUES.md).

## Contents

| Issue | Affects | Known since | Status |
| :--- | :--- | :--- | :--- |
| [ESP bridge loses pairings with ESP-IDF 5.5.6](#esp-bridge-loses-pairings-with-esp-idf-556) | ESPHome 2026.10.0 (ESP-IDF 5.5.6) | 2026-10-25 | ⚠️ Workaround: ESP-IDF 5.5.5 |
| [Brushes with a rotating address stop reconnecting on newer ESP32 chips](#brushes-with-a-rotating-address-stop-reconnecting-on-newer-esp32-chips) | ESP32-S3, C3, C6 and other chips except the original ESP32 | 2026-09-12 | ⚠️ Workaround: build option |
| [Notification subscription limit (default too low)](#notification-subscription-limit-default-too-low) | ESPHome setups without this bridge | 2026-04-01 | ✅ Solved in the bridge |
| [Unnecessary pair() calls in other ESPHome projects](#unnecessary-pair-calls-in-other-esphome-projects) | Other ESPHome projects | 2026-04-01 | ℹ️ Informational |
| [Bluedroid crash with bluetooth_proxy](#bluedroid-crash-with-bluetooth_proxy-fixed-in-esphome-202671) | `bluetooth_proxy` built with ESPHome before 2026.7.1 | 2026-04-01 | ✅ Resolved in ESPHome 2026.7.1 |

---

## ESP bridge loses pairings with ESP-IDF 5.5.6

**Known since:** 2026-10-25

**Status:** Bug in ESP-IDF 5.5.6 ([espressif/esp-idf#19286](https://github.com/espressif/esp-idf/issues/19286)), not fixed in any release yet. Workaround: build the bridge with ESP-IDF 5.5.5.

ESPHome 2026.10.0 builds with ESP-IDF 5.5.6 by default. In that version, the Bluetooth stack drops the stored pairing from memory when a paired toothbrush reconnects, and deletes the device record when the connection ends. The key is still in flash, so everything works again for one session after each restart of the ESP32.

### Symptoms

- A paired toothbrush (for example ExpertClean, HX991M, DiamondClean Prestige, Series 7100) connects once after the bridge restarts, and after the first disconnect it does not reconnect anymore.
- The bridge log shows `Deleting unpaired device` when the connection ends, and with local privacy enabled `opcode=0x2028, status=0c: Cmd Disallowed` during encryption.
- Restarting the ESP32 makes it work again until the next disconnect.
- Home Assistant shows a repair issue "Philips Sonicare: ESP bridge … loses pairings (ESP-IDF v5.5.6)". The bridge reports its ESP-IDF version, and the integration raises the issue whenever it is 5.5.6.

Models that connect without pairing (DiamondClean Smart, Sonicare For Kids HX63xx) are not affected.

### Fix

Pin ESP-IDF 5.5.5 in the bridge's YAML, then rebuild and flash it:

```yaml
esp32:
  framework:
    type: esp-idf
    version: 5.5.5
```

ESPHome prints a warning that this is not the recommended framework version; that is expected. The existing pairing stays valid. If the toothbrush already stopped reconnecting, restart the bridge once after flashing.

Remove the line again once an ESPHome release ships an ESP-IDF version with the fix.

### Bluetooth proxy

A standard ESPHome `bluetooth_proxy` built with ESPHome 2026.10.0 has the same bug for any device it holds a pairing for. The integration cannot see the ESP-IDF version of a proxy, so there is no repair issue for that case. The same YAML pin applies.

---

## Brushes with a rotating address stop reconnecting on newer ESP32 chips

**Known since:** 2026-09-12

**Status:** Behaviour of ESP-IDF on every chip but the original ESP32. Workaround: one build option in the bridge's YAML, see below.

Brushes that rotate their Bluetooth address (a resolvable private address), confirmed on the Series 7100 (HX742X) and the Sonicare For Kids HX6600, pair fine through a bridge on an ESP32-S3, but stop reconnecting afterwards. Every ESP32 variant except the original ESP32 is built the same way (ESP32-S3, ESP32-C3, ESP32-C6 and the other C and H chips), so the same applies there.

### Symptoms

- Pairing works, but the next reconnect ends in a loop of `status=133` in the bridge log, each attempt timing out after about 20 seconds.
- The bridge keeps reporting the address from the pairing, while the brush already advertises under a new one.
- Unpairing and pairing again helps for exactly one session.
- The same brush reconnects reliably through a bridge on a classic ESP32, such as an M5Stack Atom Lite.
- From bridge firmware 1.14.2 and integration 0.30.0, the bridge logs an error right after such a pairing, the setup dialog shows a warning, and Home Assistant lists the problem under Repairs.

### Cause

On these chips ESP-IDF hands the resolution of rotating addresses to the Bluetooth controller, which only follows them with local privacy switched on. ESPHome does not switch it on, so the bridge connects to the address from the pairing, which the brush no longer uses. On the classic ESP32 the host resolves the address and connects to the current one. Up to ESP-IDF 5.0 this could be switched in the build configuration; since ESP-IDF 5.1 (ESPHome 2024.12.0 and later) the option is hidden on these chips.

### Workaround

Move address resolution back to the host, as on the classic ESP32, by adding this to the bridge's YAML (ESPHome 2026.7.1 or later):

```yaml
esphome:
  build_flags:
    - -DCONFIG_BT_BLE_RPA_SUPPORTED=0
```

Then rebuild and flash the bridge. An existing pairing stays valid, and new pairings work without a restart. Other devices on the same bridge are not affected, because the bridge keeps its own address. On ESPHome 2026.10.0, also pin ESP-IDF 5.5.5, see [above](#esp-bridge-loses-pairings-with-esp-idf-556).

A bridge on a classic ESP32 (ESP32-WROOM, ESP32-PICO, M5Stack Atom Lite and similar) needs no change.

### Pairing the brush again

Each new pairing files the bond under a different address of the same brush. Up to integration 0.30.x the Home Assistant entry was keyed by that address, so after a new pairing it ignored the events of its own slot and showed no data. From integration 0.31.0 with bridge firmware 1.15.0 the entry is keyed by the identity the brush hands over during pairing, which stays the same, so a new pairing on the same slot keeps the entry working. See [Brush identity](README.md#brush-identity-integration-0310-and-later) for how existing entries switch.

---

## Notification subscription limit (default too low)

**Known since:** 2026-04-01

**Status:** Solved in our ESP32 Bridge component

The ESP32 Bluedroid BLE stack limits the number of concurrent GATT notification
subscriptions. The Sonicare requires 11+ subscriptions for full live data.

### Symptoms

- Some sensors never update (silently fail to subscribe)
- `E BT_APPL: Max Notification Reached, registration failed` in ESP32 logs
- Battery or basic status works but brushing data / pressure / brush head missing

### Root cause

| Config option | ESP-IDF default | What you actually get | Recommended |
|---------------|---------|---------|----------|
| `CONFIG_BT_GATTC_NOTIF_REG_MAX` | 5 | **12** | 20 |
| `CONFIG_BT_GATTC_MAX_CACHE_CHAR` | 40 | **40** | 80 |

The two columns differ because ESPHome sets one of them and not the other.
`esp32_ble` writes `CONFIG_BT_GATTC_NOTIF_REG_MAX` from its `max_notifications`
option, which defaults to 12 — and `esp32_ble_tracker` auto-loads that
component, so the default applies even to a config with no `esp32_ble:` block
in it. `MAX_CACHE_CHAR` has no such option and stays at the ESP-IDF default.

A config that sets neither is therefore not automatically broken: 12 slots
cover the 11 subscriptions. What it has is a single slot of headroom. The
moment anything else on the node subscribes — a `bluetooth_proxy`, a second
`ble_client`, a second brush — the limit is reached, and it is reached
silently: no build error, just sensors that never update. The limit is per
GATT client interface, not per connection, so every consumer on the node
draws from the same pool.

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

Our [ESP32 Bridge](SETUP.md) component requires ESP-IDF and documents
these settings.

---

## Unnecessary pair() calls in other ESPHome projects

**Known since:** 2026-04-01

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

---

## Bluedroid crash with bluetooth_proxy (fixed in ESPHome 2026.7.1)

**Known since:** 2026-04-01

**Status: resolved.** ESP-IDF 5.5.5 ships the Bluedroid NULL-pointer fix and
**ESPHome 2026.7.1 bundles it** — verified on the exact hardware that used to
crash. On 2026.7.1 or newer, `bluetooth_proxy` coexists with our component
without any workaround. When building with an **older** ESPHome, the component
detects the affected combination at compile time and aborts with instructions
(bridge firmware v1.10.0+) — the historical details and the workaround for old
builders are below.

<details>
<summary>Historical details and the workaround for ESPHome &lt; 2026.7.1</summary>

On older builders: without the workaround below, enabling `bluetooth_proxy` on the ESP32 — either
alone or alongside our `ble_client` component — crashes the ESP32 during GATT
service discovery. The crash occurs on every connection attempt, regardless
of framework, even if the proxy itself does not connect to the toothbrush.

With the compile-time patch described under [Workaround for older ESPHome](#workaround-for-older-esphome), the
proxy path works. See [Option C: Bluetooth Proxy](../README.md#option-c-bluetooth-proxy)
in the main README for scope and limitations of the proxy path compared to
the dedicated [ESP32 BLE Bridge](SETUP.md).

The same patch also unlocks an optional bridge performance setting
([`CONFIG_BT_GATTC_CACHE_NVS_FLASH`](SETUP.md#persisted-gatt-cache-optional))
that depends on the same NULL guards. The flag is off in a stock build
and only relevant if you enable it explicitly.

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
HX992B (`AA:BB:CC:DD:EE:FF`).

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

### ESP-IDF fix (released in v5.5.5)

The root cause is a missing NULL pointer check in `bta_gattc_disc_cmpl()`.
This was fixed in ESP-IDF commit [`d4f3517`](https://github.com/espressif/esp-idf/commit/d4f3517da4a81144eaa3f091848e61ec68ab3700)
(2026-02-27) which adds `if (p_clcb->p_srcb == NULL)` checks in several
functions including `bta_gattc_disc_cmpl`, `bta_gattc_conn`, `bta_gattc_close`,
and `bta_gattc_sm_execute`.

**The fix shipped in the ESP-IDF v5.5.5 tag (also in v6.0.2), and
ESPHome 2026.7.1 bundles ESP-IDF 5.5.5** — verified on the exact
hardware that used to crash. On 2026.7.1 or newer, `bluetooth_proxy`
coexists with our component without the workaround below.

Related issues:
- [esphome/esphome#15783](https://github.com/esphome/esphome/issues/15783) —
  ESPHome issue requesting cherry-pick of the fix (filed 2026-04-16)
- [espressif/esp-idf#4971](https://github.com/espressif/esp-idf/issues/4971) —
  `LoadProhibited` in `bta_gattc_co_cache_find_src_addr` (open since 2020,
  fixed in same commit)

### Workaround for older ESPHome

A pre-build patch script
([`bluedroid_null_fix.py`](bluedroid_null_fix.py)) is included in
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
Use the [ESP32 BLE Bridge](SETUP.md) component alone. If you need a
Bluetooth Proxy for other devices, run it on a **separate ESP32**.

</details>
