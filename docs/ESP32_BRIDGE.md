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
| [M5Stack Atom Lite](https://docs.m5stack.com/en/core/ATOM%20Lite) (ESP32-PICO) | Confirmed (maintainer) |
| [M5Stack AtomS3R](https://docs.m5stack.com/en/core/AtomS3R) (ESP32-S3) | Confirmed (maintainer) |
| Generic ESP32-DevKit | Should work (same SoC) |
| ESP32-S3 DevKitC-1 | Should work — BLE stack is compatible |
| ESP32-C3 / ESP32-C6 | Untested — BLE stack should be compatible |

## Prerequisites

- **ESP32 board** — see [Tested Hardware](#tested-hardware) above
- **ESPHome** — installed as Home Assistant add-on or standalone
- **Philips Sonicare toothbrush** — see [Tested Models](../README.md#tested-models)
- **No `bluetooth_proxy:` enabled** on this ESP — running both crashes
  Bluedroid during service discovery; see
  [ESP32 crashes/reboots when connecting](#esp32-crashesreboots-when-connecting)
  in Troubleshooting for the patch you need if both have to coexist.

## Setup overview

The bridge supports two configuration paths. Both run on the same standalone
component — they only differ in how the brush gets associated with the bridge:

- **Auto-Discovery** ⭐ — no MAC in YAML. The ESP scans for the
  Sonicare service UUID and bonds with the brush you put into pair-mode via
  the HA setup dialog. The bridge then resolves rotating BLE addresses (RPA)
  back to the same brush automatically. Recommended for all new setups —
  works for every supported model, including those with bonding.
- **Fixed MAC** — set `mac_address:` directly in the `philips_sonicare:`
  entry. The bridge connects to that MAC automatically on boot, no HA
  pair-dialog needed. Useful when the brush's BLE address is stable
  (no RPA rotation) and you want deterministic slot assignment from YAML.

<sub>⭐ marks the preferred path for new setups.</sub>

> [!IMPORTANT]
> The most recent Sonicares (e.g. Series 7100 / HX742X) use RPA — their
> advertised MAC changes on every power cycle. Pinning a MAC for those models
> will work until the brush rotates its address, after which the bridge can't
> reconnect. Auto-Discovery handles RPA transparently via the bond's identity
> resolving key, which is why it's the recommended default.

The walkthrough below uses Auto-Discovery as the primary flow, with the Fixed
MAC variant called out where the steps differ.

> [!NOTE]
> A third configuration via an external `ble_client:` block exists for
> backwards compatibility with older configs.
> See [Legacy: external `ble_client:`](#legacy-external-ble_client) at the
> bottom — new setups should not use it; it offers no advantage over Fixed
> MAC.

## Step 1: Create the ESPHome Configuration

Use the template [`esphome/atom-lite.yaml`](../esphome/atom-lite.yaml) (single brush)
or [`esphome/atom-lite-dual.yaml`](../esphome/atom-lite-dual.yaml) (multi-bridge)
as a starting point. Copy it to your ESPHome configuration directory and customize.

If you already have an ESP32 running other components (e.g. `ble_adv_proxy`,
`bluetooth_proxy`), you can add the Sonicare component to your existing config
instead of using the template.

### Required changes

1. **Board type** — the template targets the M5Stack Atom Lite. Change to your
   board if different:
   ```yaml
   esp32:
     board: m5stack-atom   # or m5stack-atoms3, esp32dev, esp32-s3-devkitc-1, …
   ```

2. **Secrets** — create or update your `secrets.yaml` with:
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

### Minimal config snippets

If you're adding this to an existing ESPHome config, these are the blocks you
need. The shared infrastructure (framework, api, ble) is identical — only the
`philips_sonicare:` entry differs between the two paths.

#### Shared infrastructure

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
```

#### Auto-Discovery (preferred)

No MAC in YAML — the brush is paired through the HA setup dialog and the
identity is persisted to NVS:

```yaml
philips_sonicare:
  - id: philips_sonicare_ble
    connected:
      name: "Sonicare Connected"
    on_connect:
      then:
        - logger.log: "Connected to Sonicare"
    on_disconnect:
      then:
        - logger.log: "Disconnected from Sonicare"
```

#### Fixed MAC

Set `mac_address:` to pin the bridge to a specific brush. The bridge connects
on boot without going through the HA pair-dialog:

```yaml
philips_sonicare:
  - id: philips_sonicare_ble
    mac_address: "XX:XX:XX:XX:XX:XX"   # <-- your toothbrush's MAC
    connected:
      name: "Sonicare Connected"
    on_connect:
      then:
        - logger.log: "Connected to Sonicare"
    on_disconnect:
      then:
        - logger.log: "Disconnected from Sonicare"
```

To find the MAC, the brush advertises as "Philips Sonicare" for ~20 seconds
after being picked up from the charger or turned on/off:

- **Home Assistant Bluetooth**: Settings → Devices & Services → Bluetooth →
  look for "Philips Sonicare"
- **nRF Connect** ([Android](https://play.google.com/store/apps/details?id=no.nordicsemi.android.mcp) / [iOS](https://apps.apple.com/app/nrf-connect-for-mobile/id1054362403)):
  scan and filter for "Philips"
- **ESPHome logs**: any ESP32 with `esp32_ble_tracker` enabled will print
  `Found device … Name: 'Philips Sonicare'`

## Step 2: Flash the ESP32

1. Open the **ESPHome Dashboard** in Home Assistant
2. Add a new device or upload your customized YAML
3. Click **Install** and choose your flashing method:
   - USB for first-time flash
   - OTA for subsequent updates
4. Wait for the build and flash to complete

> [!NOTE]
> Switching between Arduino and ESP-IDF framework requires a full clean build
> ("Clean Build Files" in the ESPHome dashboard before flashing).

## Step 3: Verify the Bridge Boots

After flashing, the ESP32 boots and starts scanning. The expected boot log
depends on which configuration path you chose.

**Auto-Discovery (no MAC, no stored bond)** — the bridge does **not** initiate
any connection on its own; it waits for the HA setup flow to arm pair-mode:

```
[I][philips_sonicare.bridge:065]: Services registered (suffix: 'default')
[I][philips_sonicare:072]: No identity in flash — UUID scan mode (waiting for pair-mode)
[C][philips_sonicare.bridge:110]: Philips Sonicare Bridge v1.3.0
[C][philips_sonicare.bridge:112]:   Bridge ID: default
```

The `No identity in flash — UUID scan mode` line confirms the bridge is up and
waiting for pair-mode. (`suffix: 'default'` becomes the actual `bridge_id` you
configured — `'kids'` / `'prestige'` etc. — for multi-bridge setups.)

**Fixed MAC** — the bridge logs the configured MAC and starts connecting
immediately:

```
[I][philips_sonicare.bridge:065]: Services registered (suffix: 'default')
[I][philips_sonicare:063]: Using configured MAC address — MAC mode
[C][philips_sonicare.bridge:110]: Philips Sonicare Bridge v1.3.0
[C][philips_sonicare.bridge:112]:   Bridge ID: default
```

After a successful pair, subsequent boots show the brush reconnecting on its own:

```
[D][esp32_ble_client:207]: [1] [24:E5:AA:xx:xx:xx] Found device
[I][esp32_ble_client:125]: [1] [24:E5:AA:xx:xx:xx] 0x00 Connecting
[I][esp32_ble_client:570]: [1] [24:E5:AA:xx:xx:xx] auth complete addr: …
[I][philips_sonicare:670]: Pairing successful — device bonded (auth_mode=…)
[I][philips_sonicare:363]: Service discovery complete
```

> [!NOTE]
> Most Sonicare models (DiamondClean Smart HX992X) use **open GATT without
> bonding** — no `auth success` line is expected. Models with bonding
> (Prestige HX999X, ExpertClean HX962X, HX991M, HX742X Condor) emit
> `auth success` after a successful pair-mode flow.

## Step 4: Add the Integration in Home Assistant

1. Install the **Philips Sonicare** integration in Home Assistant
   (via [HACS](../README.md#installation) or manually)
2. Go to **Settings > Devices & Services > + Add Integration** and search for **Philips Sonicare**
3. Select **"ESP32 Bridge"** → pick your ESP32 from the list
4. If the ESP exposes multiple bridge slots (multi-device setup), pick the one
   you want to bond

What happens next depends on the configuration path:

**Auto-Discovery** — the slot is empty (`pair_capable=true`), so the
integration goes straight to **"Pair toothbrush with bridge"**:

1. Switch on the brush you want to bond (press the power button)
2. Hold it within ~1 m of the ESP
3. Click **Start pairing** — the bridge scans for ~60 s and bonds with the
   first Sonicare it finds

> [!IMPORTANT]
> If you have multiple toothbrushes nearby, only switch on the one you want to
> bond — the bridge bonds to the first matching brush, period. The pair dialog
> in HA reminds you of this.

**Fixed MAC** — the slot already has an identity from YAML
(`pair_capable=false`), so the pair dialog is skipped and the integration
goes straight to capability detection. Make sure the brush is reachable
(within range, not on the charger) on first setup so the bridge can complete
GATT discovery; bonded models will run the SMP exchange on this first
connection.

After both paths, the integration reads the toothbrush capabilities and shows
the **Connection via ESP32 Bridge** page (v1.3.0, BLE Connected, bond status,
MAC, etc.). Click **Read capabilities** to finish setup.

### Expected pair-mode logs

A successful pair-mode run on the ESP looks like this:

```
[I][philips_sonicare:146]: Pair-mode enabled for 60s
[I][philips_sonicare:213]: Found Sonicare via UUID at AA:BB:CC:DD:EE:FF (pair-mode, classic)
[I][esp32_ble_client:125]: [1] [AA:BB:CC:DD:EE:FF] 0x00 Connecting
[I][esp32_ble_client:343]: [1] [AA:BB:CC:DD:EE:FF] Connection open
[I][philips_sonicare:301]: Connected to Sonicare (AA:BB:CC:DD:EE:FF, bridge=prestige)
[I][esp32_ble_client:570]: [1] [AA:BB:CC:DD:EE:FF] auth complete addr: AA:BB:CC:DD:EE:FF
[D][esp32_ble_client:575]: [1] [AA:BB:CC:DD:EE:FF] auth success type = 0 mode = 9
[I][philips_sonicare:253]: Bonded — saving identity address, switching to MAC mode
[I][philips_sonicare:670]: Pairing successful — device bonded (auth_mode=9)
```

The `(pair-mode, classic)` token identifies the brush family — `classic` for
the per-feature service-based protocol (HX9XXX etc.), `condor` for the
HX742X / Series 7100+ framed transport. After `Bonded — saving identity address`
the bridge writes the bond to NVS and the brush will reconnect on its own
across reboots (no further pair-mode needed).

> [!NOTE]
> Models with **open GATT** (DiamondClean Smart HX992X) skip the SMP / auth
> dance — you'll see `Connected to Sonicare …` and `Service discovery complete`
> but no `auth success` line. Bond persistence still works via the saved
> identity, so reconnect on next boot is the same.

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

### Pair-mode times out without finding the brush

- The brush only advertises for ~20 seconds after waking up. Press the power
  button or pick it up from the charger immediately before clicking
  **Start pairing**, then keep it on
- The brush is **not reachable** via BLE while on the charging stand
- Make sure no phone has an active connection to the brush — close or uninstall
  the Sonicare app
- Distance: keep the brush within ~1 m of the ESP during pair-mode

### "No philips_sonicare services found"

Make sure your ESPHome config includes `custom_services: true` and
`homeassistant_services: true` under the `api:` section. These flags are required
since ESPHome 2025.7.0.

### "No ESPHome devices found" in HA config flow

- The ESP32 must be fully set up and connected to Home Assistant via the ESPHome
  integration first
- Check **Settings > Devices & Services > ESPHome** — your device should be listed there
- If using a fresh ESPHome install, wait for the device to come online after flashing

### No data after OTA update

After an OTA flash, the ESP32 reboots and reconnects to the toothbrush via BLE before
Home Assistant re-establishes the API stream (~5-10 seconds). The bridge automatically
re-fires the "ready" event every 15 seconds until HA subscribes to notifications.
If data still doesn't flow:

- **Reload the integration** in HA (Settings > Devices & Services > Philips Sonicare > ... > Reload)
- Check ESPHome logs for `BLE connected, no subscriptions — re-firing ready`
- Check HA logs for `ESP bridge rebooted — forcing re-setup`

## Service & event reference

The bridge exposes its functionality to Home Assistant as a small set of
ESPHome services (`ble_read_char`, `ble_subscribe`, `ble_pair_mode`, …) and
fires events back (`esphome.philips_sonicare_ble_data`,
`esphome.philips_sonicare_ble_status`, `esphome.philips_sonicare_ble_services`).
For the architecture diagram, the full per-service signatures, and the event
field reference, see **[ESP32_PROTOCOL.md](ESP32_PROTOCOL.md)**.

---

## Multi-Device Setup

A single ESP32 can bridge **multiple** Sonicare toothbrushes. Each gets its
own `philips_sonicare:` entry with a unique `bridge_id`. Each slot can use
either path independently — Auto-Discovery, Fixed MAC, or a mix:

```yaml
philips_sonicare:
  - id: sonicare_prestige
    bridge_id: prestige
    # Auto-Discovery — paired via HA dialog

  - id: sonicare_kids
    bridge_id: kids
    mac_address: "XX:XX:XX:XX:XX:XX"   # Fixed MAC — pinned from YAML
```

The `bridge_id` is **required** when using multiple instances — the ESP will
refuse to compile without it. It serves as a suffix for service names
(e.g., `ble_pair_mode_prestige`, `ble_read_char_prestige`) so HA can address
each slot separately. The same label appears in the HA bridge picker.

Each slot has its own bond storage in NVS, so the two brushes are completely
independent — pair, unpair, or re-pair one without affecting the other.

A full example is available in
[`esphome/atom-lite-dual.yaml`](../esphome/atom-lite-dual.yaml).

> [!NOTE]
> Each toothbrush should only be connected via **one** path — either Direct BLE
> or ESP Bridge, not both simultaneously.

---

<details>
<summary><strong>Legacy: external <code>ble_client:</code></strong> (kept for backwards compatibility)</summary>

Earlier versions of this component required an external `ble_client:` block
that the `philips_sonicare:` entry then referenced via `ble_client_id:`. This
configuration is still accepted so existing YAMLs keep working, but it offers
no advantage over [Fixed MAC](#fixed-mac) above — both pin the brush by MAC
and connect on boot — while requiring an extra block. **New setups should use
Auto-Discovery or Fixed MAC.**

```yaml
ble_client:
  - mac_address: "XX:XX:XX:XX:XX:XX"   # <-- your toothbrush's MAC
    id: my_brush
    auto_connect: true

philips_sonicare:
  - ble_client_id: my_brush
    connected:
      name: "Sonicare Connected"
```

To migrate to Fixed MAC, drop the `ble_client:` block, replace
`ble_client_id: my_brush` with `mac_address: "XX:XX:XX:XX:XX:XX"`, and
re-flash. The bond persists across the migration since it lives on the
ESP's NVS, not in the YAML.

> [!NOTE]
> The `on_connect` / `on_disconnect` automations on the external `ble_client:`
> fire on raw GAP-connect, before service discovery completes. Use the
> equivalent triggers on the `philips_sonicare:` entry instead — they fire
> after the bridge is `ready`, when reads and writes are actually safe.

</details>
