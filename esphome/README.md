# ESP Bridge

Everything needed to turn an ESP32 into a Bluetooth bridge for the Philips
Sonicare integration. The ESP handles the BLE connection to the toothbrush
(including LE Secure Connections bonding for models that require it) and
exposes it to Home Assistant via ESPHome service calls and events.

Use this when Home Assistant itself has no Bluetooth adapter in range of
the toothbrush, when you want to monitor multiple brushes from one bridge,
or when you prefer a dedicated, always-connected bridge over the less
stable `bluetooth_proxy` path.

Side by side with the Direct BLE path (Home Assistant's own Bluetooth
adapter), which the integration uses when no bridge is configured:

```text
  Direct BLE                            ESP bridge

  ┌─────────────────────┐              ┌─────────────────────┐
  │   Home Assistant    │              │   Home Assistant    │
  │  ┌───────────────┐  │              │  ┌───────────────┐  │
  │  │  integration  │  │              │  │  integration  │  │
  │  └───────┬───────┘  │              │  └───────┬───────┘  │
  │   bleak / BlueZ     │              │  ESPHome native API │
  │  ┌───────┴───────┐  │              └──────────┼──────────┘
  │  │  BT adapter   │  │                    Wi-Fi│ services ▼
  │  └───────┬───────┘  │                         │ events   ▲
  └──────────┼──────────┘              ┌──────────┴──────────┐
             │ BLE                     │        ESP32        │
             ▼                         │  philips_sonicare   │
      ┌────────────┐                   │  BLE client — owns  │
      │ toothbrush │                   │  connection + bond  │
      └────────────┘                   └─────┬──────────┬────┘
                                          BLE│          │BLE
                                             ▼          ▼
                                       ┌──────────┐ ┌──────────┐
                                       │ brush #1 │ │ brush #2 │
                                       └──────────┘ └──────────┘
```

- **Where the BLE link lives:** Direct BLE ties the connection to the HA
  host's adapter — the brush must be in radio range of your server. With
  the bridge, the BLE link terminates on the ESP32, which you can place
  anywhere with Wi-Fi coverage (e.g. in the bathroom).
- **Who holds the bond:** on the bridge path the ESP stores the LE bond
  in its own flash (NVS), independent of Home Assistant restarts,
  container rebuilds, or the host's BlueZ state.
- **What travels over Wi-Fi:** plain ESPHome traffic — HA calls services
  (`ble_read_char_<bridge_id>`, …) and the bridge answers with events.
  No Bluetooth stack is involved on the HA side at all.
- **Multi-device:** one bridge serves several toothbrushes, each as its
  own slot with its own service set.

For end-to-end setup instructions (flashing, integration configuration,
multi-device setups) see [`SETUP.md`](SETUP.md).

## Contents

| File | Description |
|------|-------------|
| [`components/philips_sonicare/`](components/philips_sonicare/) | The C++ ESPHome external component. This is the actual bridge implementation — BLE client, GATT read/write/subscribe, bonding, and the HA event/service interface. |
| [`atom-lite.yaml`](atom-lite.yaml) | Ready-to-flash config for the M5Stack Atom Lite, one toothbrush. `bluetooth_proxy` is disabled by default but prepared — uncomment the `bluetooth_proxy:` block and the `extra_scripts:` entry (both clearly marked in the file) to run the bridge and a proxy for other BLE devices in parallel. |
| [`atom-lite-dual.yaml`](atom-lite-dual.yaml) | Same board, but configured for **two** Sonicares via one bridge. Raises `BTA_GATTC_NOTIF_REG_MAX` and `BTA_GATTC_MAX_CACHE_CHAR` accordingly. `bluetooth_proxy` also off by default, same uncomment recipe as above. |
| [`esp32-generic.yaml`](esp32-generic.yaml) | Generic ESP32 dev-board config (`esp32dev`). Use as a starting point for other boards. Same proxy opt-in pattern. |
| [`bluedroid_null_fix.py`](bluedroid_null_fix.py) | Compile-time patch — see next section. |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history for the external component. |

## Per-slot defaults: `friendly_name` and `area`

Each `philips_sonicare:` slot accepts two optional fields that pre-fill the
Home Assistant setup form for that brush:

```yaml
philips_sonicare:
  - id: sonicare_prestige
    bridge_id: prestige
    friendly_name: "Master Bath"
    area: "Master Bathroom"
```

- `friendly_name` becomes the default in the HA "Name" prompt during setup.
  It also appears as the slot label in the multi-brush picker, so you can
  tell which slot is which before installing.
- `area` auto-assigns the new device to that HA area on first install.

Both fields are **one-shot defaults**: they only apply when the brush is
first added through the HA UI. Editing them in YAML afterward does not
rename existing devices or move them between areas — use Settings →
Devices in Home Assistant for that.

One caveat for `area:` — the integration also re-applies the YAML area on
setup if the device's area is currently *unset*. So if you manually clear
the device's area in HA and reload the integration, the YAML default will
fill it in again. To opt out permanently, remove the `area:` line from
the slot.

## Bluedroid NULL-check patch (`bluedroid_null_fix.py`)

> [!IMPORTANT]
> **If you enable `bluetooth_proxy:` in the same ESP config as this bridge,
> the ESP will crash on boot** with a `LoadProhibited` / `bta_gattc_cache_save`
> exception. This is an ESP-IDF bug (not specific to this integration) —
> tracked in [esphome#15783](https://github.com/esphome/esphome/issues/15783).

Until ESP-IDF **v5.5.5** is released (still pending as of July 2026; it will
contain the fix from ESP-IDF commit [`d4f3517`](https://github.com/espressif/esp-idf/commit/d4f3517)),
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
required** — unless you also opt into the persisted GATT cache (next
section), which depends on the same NULL guards.

## Persisted GATT cache (optional)

Adding `CONFIG_BT_GATTC_CACHE_NVS_FLASH: "y"` under `sdkconfig_options`
persists the discovered GATT table to NVS keyed by the bonded peer's
identity. With it, the first connect to a bonded brush still does a
full ~4 s service discovery; subsequent reconnects hit the cached
service table in tens of ms. On a brush you reconnect to often this is
the biggest single contributor to reconnect latency.

The cache-save path crashes on the same NULL deref as the proxy bug, so
this flag depends on the patch above. To opt in, uncomment the
`CONFIG_BT_GATTC_CACHE_NVS_FLASH` line in your YAML (and the patch
wiring at the top of `esphome:` if not already enabled).

## Pipelined GATT reads (bridge ≥ 1.7.0)

How the integration polls characteristics depends on the bridge firmware
version, which the bridge reports in its status events:

- **Bridge ≥ 1.7.0 (pipelined):** Home Assistant fires the whole poll
  batch at once. The firmware serialises everything through a single
  ATT-operation scheduler — only one GATT read/write/subscribe is in
  flight on the BLE link at any time; the rest wait in the bridge's
  pending-calls queue and are drained back-to-back as each operation
  completes. Reads deferred behind connection setup (service discovery,
  subscription writes, the SMP handshake on bonded brushes) simply wait
  in the queue instead of timing out individually, so a poll batch
  completes without lost values. A 10-second ATT watchdog recovers the
  queue if the BLE stack ever drops a completion event, so a lost read
  costs one 10 s stall instead of a stuck connection.

  ```text
  HA                ESP                 Brush
  │── N reads ─────►│ queue [████ N]      │
  │                 │── request #1 ──────►│
  │◄──── event ─────│◄─── response #1 ────│
  │◄──── event ─────│── request #2 ──────►│  ◄─ back-to-back at radio
  │◄──── event ─────│── request #3 ──────►│     pace, no HA round-trip
  ⋮                 ⋮                     ⋮     in between

  • one timeout budgets the whole batch (15 s + 1 s per read)
  • waiting behind connection setup is safe: the queue holds the reads
    instead of letting them time out
  ```

- **Bridge < 1.7.0 (sequential):** older firmware has a single response
  slot, so overlapping reads would silently drop all but the last reply.
  The integration detects this from the reported version and falls back
  to reading one characteristic at a time, waiting for each reply —
  exactly the previous behavior. Everything keeps working, just with a
  slower read phase, and a read fired during connection setup can time
  out on the HA side before the bridge executes it.

  ```text
  HA                ESP                 Brush
  │── read #1 ─────►│                     │
  │                 │── ATT request ─────►│ ╮
  │                 │◄──── ATT response ──│ ╯ a few conn events
  │◄──── event ─────│                     │
  │── read #2 ─────►│                     │  ◄─ next read only after a
  │                 │──────►              │     full HA↔ESP round-trip
  ⋮                 ⋮                     ⋮     (× N reads)

  • per read: radio round-trip + HA↔ESP round-trip
  • each read has its own 5 s timeout → a read fired while the bridge
    is still subscribing (or mid-SMP on a bonded brush) can expire
    before it ever executes
  ```

The "Pipelined GATT reads" toggle in the integration options is a
runtime opt-out: switch it off to force sequential reads without
reflashing if a setup misbehaves. It has no effect on bridges older
than 1.7.0.

How fast a batch completes is bounded by the BLE **connection
interval**, which the device may renegotiate depending on its state.
Each GATT read costs a few connection events, so the same batch can
take a different time on a fresh connection than on a long-idle one —
in both modes. Pipelining removes the per-read HA→ESP round-trip and
the timeout risk; it cannot make the radio tick faster. The firmware
logs `Conn params initial/now: interval=… ms` lines (INFO) whenever
the parameters change, which makes this directly visible.

## Connection-parameter boost (not implemented)

Unlike the bridge firmware of [philips_shaver](https://github.com/mtheli/philips_shaver)
(the sister integration for Philips shavers, ≥ 1.11.0), this firmware
implements no connection-parameter boost. The toothbrushes measured so far have not
shown a profile that would need one: a Prestige 9900 holds a constant
15 ms interval for the whole connection, and a Kids HX6340 settles on
a mild power-save profile (70 ms, slave latency 3) that still
completes a full poll batch in under 3 s and delivers its
once-per-second brushing notifications without loss. Should a model
turn up whose idle profile genuinely slows polling down, the
`Conn params` log lines above are the data to collect first.

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
