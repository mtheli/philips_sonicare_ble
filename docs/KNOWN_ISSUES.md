# Known Issues

## Contents

- [Notification subscription limit (default too low)](#notification-subscription-limit-default-too-low)
- [Brushing Mode Select not available on BrushSync models](#brushing-mode-select-not-available-on-brushsync-models)
- [Budget BT adapters can't scan while a GATT connection is active](#budget-bt-adapters-cant-scan-while-a-gatt-connection-is-active)
- [Some USB dongles cannot complete SMP bonding](#some-usb-dongles-cannot-complete-smp-bonding)
- [Toothbrush not reachable on charger](#toothbrush-not-reachable-on-charger)
- [Unnecessary pair() calls in other ESPHome projects](#unnecessary-pair-calls-in-other-esphome-projects)
- ✅ [Bluedroid crash with bluetooth_proxy](#bluedroid-crash-with-bluetooth_proxy-fixed-in-esphome-202671) — **resolved**, fixed in ESPHome 2026.7.1

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

Our [ESP32 Bridge](../esphome/SETUP.md) component requires ESP-IDF and documents
these settings.

---

## Brushing Mode Select not available on BrushSync models

**Status:** Confirmed, firmware limitation — entity model-gated

On BrushSync-enabled models (DiamondClean Smart HX992X, Sonicare For Kids
HX63xx, ExpertClean HX962X, DiamondClean 9000 HX991X), the toothbrush
accepts BLE mode writes at the GATT level but ignores them on the firmware
level — the mode is determined by the attached brush head (BrushSync) or
the physical button.

The integration therefore only creates the **Brushing Mode** and
**Intensity** Select entities for models that support BLE mode writes:
DiamondClean Prestige (HX999X), HX9996, and HX74xx. If you have a
non-BrushSync model where mode writes work, please open an issue — we
can add it to `MODE_WRITE_MODELS` in `const.py`.

---

## Budget BT adapters can't scan while a GATT connection is active

**Status:** Hardware / host-BT-stack limitation, no integration-side fix

USB Bluetooth dongles built around CSR, Realtek and similar consumer-grade
chipsets often cannot run **active BLE scanning and a GATT connection in
parallel**. On such adapters, the scanner stops delivering advertisements
to the host as soon as the first toothbrush is connected. Wake-ups for
other toothbrushes are delayed until the next scanner restart.

### Symptoms

- First toothbrush connects normally.
- Second / third toothbrush stays in `Waiting for advertisement from …`
  for up to ~2 minutes after it has actually started advertising
  (power button pressed, on/off the charger).
- HA log shows `Scanner watchdog time_since_last_detection` climbing
  steadily after the first connect, e.g. `1.6s → 25.9s → 55.9s → 85.9s`.
- Wake-ups for the idle toothbrushes only fire right after an entry
  like `Bluetooth scanner has gone quiet for 115.9s, restarting` —
  the watchdog runs every ~120 seconds and briefly frees the radio
  for scanning, which catches any queued advertisements.

### Root cause

The adapter only has one physical radio and cannot time-slice it cleanly
between an active connection interval (30-50 ms) and an active-scan
window. Once a connection is established, scanning either stalls or
falls back to passive, producing no advertisement callbacks. `habluetooth`
detects the starvation via its watchdog and force-restarts the scanner
every 120 seconds, which is the mechanism that eventually unblocks
wake-ups for the other toothbrushes.

### Confirmed affected adapter

- `00:0A:CD:46:B2:2D` (USB, IDs reported as generic CSR-class) — three
  toothbrushes: first connect succeeds immediately, second/third require
  a watchdog cycle.

### Mitigations

In rough order of effectiveness:

1. **Better host adapter.** Intel AX200 / AX210 (M.2 WLAN+BT combos, often
   already present on modern mainboards) and onboard BT5.2 on recent NUCs
   / mini-PCs reliably do 3-7 concurrent BLE connections with parallel
   scanning.
2. **[ESP32 BLE Bridge](../esphome/SETUP.md) for the toothbrushes that wake
   rarely.** Each bridge owns its GATT link end-to-end, so the host's
   hci0 never has to multiplex. Scales linearly with ESPs.
3. **Power-cycle workaround.** If you want to force a wake-up right now
   rather than wait for the 120 s watchdog, briefly disable and re-enable
   HA's Bluetooth integration — this restarts scanning immediately.

Nothing in this repository can work around this in code. The integration
sees only what `habluetooth` delivers; the starvation sits one layer down.

---

## Some USB dongles cannot complete SMP bonding

**Status:** Hardware limitation of the adapter's SMP implementation, no integration-side fix

The bonding-required models (ExpertClean HX962X, DiamondClean 9000
HX991X/HX991M, DiamondClean Prestige HX999X, Series 7100 HX742X) need a
successful **SMP bonding handshake** during setup. Some cheap USB Bluetooth
dongles connect fine but never complete that handshake — the pairing runs
into a timeout on every attempt, while the very same brush bonds immediately
on a different adapter on the same host.

### Symptoms

- Config flow ends with **"Pairing timed out after 30s"**, repeatably — the
  connection itself is established and held, it is the pairing step that
  never finishes.
- Manual `bluetoothctl` pairing fails with `org.bluez.Error.AuthenticationCanceled`.
- Moving the *same* setup to another adapter (e.g. the Raspberry Pi's
  built-in radio) bonds within one or two attempts.

### Confirmed affected adapter

- Generic "100 m long-range" BT 5.3 USB dongle with an **Actions**
  chipset (Bluetooth company ID `0x03E0`), sold under UGREEN and many
  other brand names — reported in
  [issue #27](https://github.com/mtheli/philips_sonicare_ble/issues/27)
  against a Series 7100 (HX742X). The Pi's built-in Cypress radio bonded
  the same brush on the second try.

Not every pairing failure is the adapter, though. If pairing fails,
update to the latest release and try the
[manual pairing script](../README.md#option-a-direct-bluetooth) first —
only when the handshake times out consistently on one adapter and
succeeds on another is the dongle the culprit.

### Mitigations

1. **Use a bonding-capable adapter** — the Raspberry Pi's built-in radio,
   Intel AX200/AX210-class M.2 combos, or a genuine CSR8510A10 dongle.
2. **Use the [ESP32 BLE Bridge](../esphome/SETUP.md)** — it performs the
   bonding on the ESP32 itself, taking the host adapter out of the
   equation entirely.

Note that the cheap dongles affected here overlap heavily with the
adapters described in
[the scanning limitation above](#budget-bt-adapters-cant-scan-while-a-gatt-connection-is-active) —
if a dongle fails at bonding, it is usually also a poor scanner.

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

---

## Bluedroid crash with bluetooth_proxy (fixed in ESPHome 2026.7.1)

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

With the compile-time patch described under [Workaround](#workaround), the
proxy path works. See [Option C: Bluetooth Proxy](../README.md#option-c-bluetooth-proxy)
in the main README for scope and limitations of the proxy path compared to
the dedicated [ESP32 BLE Bridge](../esphome/SETUP.md).

The same patch also unlocks an optional bridge performance setting
([`CONFIG_BT_GATTC_CACHE_NVS_FLASH`](../esphome/SETUP.md#persisted-gatt-cache-optional))
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
Use the [ESP32 BLE Bridge](../esphome/SETUP.md) component alone. If you need a
Bluetooth Proxy for other devices, run it on a **separate ESP32**.

</details>
