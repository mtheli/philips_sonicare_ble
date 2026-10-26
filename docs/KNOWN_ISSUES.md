# Known issues

> **Using an ESP32 bridge or an ESPHome Bluetooth proxy?** Issues with the bridge, ESPHome and ESP-IDF are in the [ESP bridge known issues](../esphome/KNOWN_ISSUES.md).

## Contents

| Issue | Affects | Known since | Status |
| :--- | :--- | :--- | :--- |
| [Some USB dongles cannot complete SMP bonding](#some-usb-dongles-cannot-complete-smp-bonding) | Some USB Bluetooth dongles | 2026-07-20 | ❌ Adapter limitation |
| [Budget BT adapters can't scan while a GATT connection is active](#budget-bt-adapters-cant-scan-while-a-gatt-connection-is-active) | Budget USB Bluetooth adapters | 2026-04-24 | ❌ Adapter limitation |
| [Toothbrush not reachable on charger](#toothbrush-not-reachable-on-charger) | All models | 2026-04-01 | ℹ️ By design |
| [Brushing Mode Select not available on BrushSync models](#brushing-mode-select-not-available-on-brushsync-models) | BrushSync models | 2026-03-23 | ❌ Brush firmware limitation |

---

## Some USB dongles cannot complete SMP bonding

**Known since:** 2026-07-20

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

## Budget BT adapters can't scan while a GATT connection is active

**Known since:** 2026-04-24

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

## Toothbrush not reachable on charger

**Known since:** 2026-04-01

**Status:** By design (hardware behavior)

The Sonicare enters deep sleep while on the charging stand and does not
advertise via BLE. It only advertises for ~20 seconds after being picked up
from the charger or turned on/off.

This is not a bug — it is the toothbrush's power management behavior. The
integration reconnects automatically when the toothbrush wakes up.

---

## Brushing Mode Select not available on BrushSync models

**Known since:** 2026-03-23

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

## Moved sections

These sections now live in the [ESP bridge known issues](../esphome/KNOWN_ISSUES.md). The headings stay here so that older links keep working.

### ESP bridge loses pairings with ESP-IDF 5.5.6

Moved to the [ESP bridge known issues](../esphome/KNOWN_ISSUES.md#esp-bridge-loses-pairings-with-esp-idf-556).

### Brushes with a rotating address stop reconnecting on newer ESP32 chips

Moved to the [ESP bridge known issues](../esphome/KNOWN_ISSUES.md#brushes-with-a-rotating-address-stop-reconnecting-on-newer-esp32-chips).

### Notification subscription limit (default too low)

Moved to the [ESP bridge known issues](../esphome/KNOWN_ISSUES.md#notification-subscription-limit-default-too-low).

### Unnecessary pair() calls in other ESPHome projects

Moved to the [ESP bridge known issues](../esphome/KNOWN_ISSUES.md#unnecessary-pair-calls-in-other-esphome-projects).

### Bluedroid crash with bluetooth_proxy (fixed in ESPHome 2026.7.1)

Moved to the [ESP bridge known issues](../esphome/KNOWN_ISSUES.md#bluedroid-crash-with-bluetooth_proxy-fixed-in-esphome-202671).
