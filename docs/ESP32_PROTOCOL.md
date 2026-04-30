# ESP32 Bridge — HA-Service Protocol

This document describes the Home Assistant–facing protocol of the
`philips_sonicare` ESPHome component: which services it registers, what
arguments they take, and which events the bridge fires in response.

The bridge is a thin façade — every action HA wants the ESP to perform is an
`esphome.<device>_<service>` call, every reply comes back as a Home Assistant
event the integration listens for. There is no direct return value.

> Component version: **1.3.0**

## Conventions

### Service names and `bridge_id`

Service names are constructed as:

```
esphome.<esphome_device_name>_<service>[_<bridge_id>]
```

The `bridge_id` suffix is **only present** when the YAML sets a non-empty
`bridge_id` for the `philips_sonicare:` block. Single-bridge setups omit it:

```yaml
philips_sonicare:
  - id: brush
    bridge_id: ""        # default — no suffix
```

Multi-bridge setups (multiple Sonicares on the same ESP) add a unique suffix:

```yaml
philips_sonicare:
  - id: brush_mom
    bridge_id: mom       # services become …_ble_read_char_mom etc.
  - id: brush_dad
    bridge_id: dad
```

### Events and `bridge_id`

Every event payload includes a `bridge_id` field. For single-bridge setups it
is an empty string. Multi-bridge setups must filter by it on the HA side, the
ESP fires the same event names regardless of which slot triggered them.

The bridge emits three event types only:

| Event name | Used for |
|---|---|
| `esphome.philips_sonicare_ble_status` | Lifecycle, info, pair-mode, errors |
| `esphome.philips_sonicare_ble_data` | Read responses + notifications |
| `esphome.philips_sonicare_ble_services` | Output of `ble_list_services` |

The `status` field of `_ble_status` events disambiguates which lifecycle
condition fired — see the per-service tables below.

### UUID parsing

UUIDs accept three forms:

- **4 hex chars** → 16-bit BLE UUID, e.g. `"180a"`
- **8 hex chars** → 32-bit BLE UUID
- **Anything else** → treated as a 128-bit raw UUID (`"477ea600-a260-11e4-ae37-0002a5d50001"`)

Garbage input is rejected with a warning and falls back to raw, which usually
fails downstream as `error="not_found"`.

---

## Operation modes

The bridge runs in one of two modes, chosen at build time by the YAML schema.
HA detects which is active by reading `ble_get_info` and checking the `mode`
field.

| Mode | Trigger in YAML | `mode` field | Pair-flow |
|---|---|---|---|
| **A — external** | uses an external `ble_client:` block (`ble_client_id` set) | `"external"` | MAC is fixed in YAML, no pair-mode |
| **B — standalone** | no `ble_client:` block, optional `mac_address:` | `"standalone"` | If no MAC and no NVS identity: `pair_capable=true`, HA must call `ble_pair_mode` to bond a brush |

A standalone bridge that has either a YAML MAC or a previously bonded brush
(persisted in NVS) reports `pair_capable=false` — same as Mode A from HA's
point of view.

### Mode A — external `ble_client`

Use this when you want the classic ESPHome BLE-client behavior with a fixed
MAC, or when migrating from older versions of this component.

```yaml
ble_client:
  - id: sonicare_main
    mac_address: "24:E5:AA:14:9B:86"
    auto_connect: true

philips_sonicare:
  - ble_client_id: sonicare_main
    bridge_id: main          # optional, suffixes HA service names
```

`ble_client_id` is required for Mode A. If omitted, the schema falls
through to Mode B.

### Mode B — standalone, fixed MAC

You know your brush's MAC and don't want a pair-flow. The bridge connects
the same way as Mode A but without the dummy `ble_client:` block.

```yaml
philips_sonicare:
  - mac_address: "24:E5:AA:14:9B:86"
    bridge_id: main
    auto_connect: true       # default true when mac_address is set
```

### Mode B — standalone, pair-flow (recommended for fresh setups)

No MAC, no NVS identity yet. The bridge stays passive on boot and only
scans for Sonicares when HA arms `ble_pair_mode` (e.g. via the integration's
config flow). After successful pairing, the identity address is persisted
to flash and subsequent boots auto-reconnect.

```yaml
philips_sonicare:
  - bridge_id: main
    # No mac_address — bridge waits for HA to arm pair-mode
    on_connect:
      - logger.log: "Sonicare connected"
    on_disconnect:
      - logger.log: "Sonicare disconnected"
```

To bond multiple brushes on a single ESP32, repeat the block with distinct
`bridge_id`s — each gets its own service-name suffix:

```yaml
philips_sonicare:
  - bridge_id: mom
  - bridge_id: dad
  - bridge_id: kids
```

---

## Services

| # | Service | Args | Available since |
|---|---|---|---|
| 1 | [`ble_read_char`](#ble_read_char) | `service_uuid`, `char_uuid` | 1.2.0 |
| 2 | [`ble_subscribe`](#ble_subscribe) | `service_uuid`, `char_uuid` | 1.2.0 |
| 3 | [`ble_unsubscribe`](#ble_unsubscribe) | `service_uuid`, `char_uuid` | 1.2.0 |
| 4 | [`ble_write_char`](#ble_write_char) | `service_uuid`, `char_uuid`, `data` | 1.2.0 |
| 5 | [`ble_set_throttle`](#ble_set_throttle) | `throttle_ms` | 1.2.0 |
| 6 | [`ble_get_info`](#ble_get_info) | — | 1.2.0 (extended in 1.3.0) |
| 7 | [`ble_list_services`](#ble_list_services) | — | 1.3.0 |
| 8 | [`ble_pair_mode`](#ble_pair_mode) | `enabled`, `timeout_s` | 1.3.0 |
| 9 | [`ble_unpair`](#ble_unpair) | — | 1.3.0 |
| 10 | [`ble_scan`](#ble_scan) | `timeout_s` | 1.3.0 |
| 11 | [`ble_pair_mac`](#ble_pair_mac) | `mac`, `timeout_s` | 1.3.0 |

Services 8–11 are meaningful only in **Mode B**. Calling them on a Mode-A
bridge emits a warning to the log and is otherwise a no-op.

### `ble_read_char`

*Available since 1.2.0.*

Read a GATT characteristic on the bonded brush.

| | |
|---|---|
| **Args** | `service_uuid: string`, `char_uuid: string` |
| **Side-effect** | Issues an `esp_ble_gattc_read_char`. On `INSUF_AUTH/ENCR` it transparently triggers SMP and retries the read once `AUTH_CMPL` succeeds (auto-retry behavior added in 1.3.0). |
| **Reply** | `_ble_data` event |

**`_ble_data` (success):**

| Field | Type | Note |
|---|---|---|
| `uuid` | string | The `char_uuid` requested |
| `payload` | string (hex) | Raw bytes, lowercase hex |
| `mac` | string | Brush MAC |
| `bridge_id` | string | Possibly empty |

**`_ble_data` (failure):**

| Field | Value |
|---|---|
| `payload` | `""` |
| `error` | `not_connected` \| `not_found` \| `read_failed` \| `auth_failed` \| `gatt_err_<n>` \| `queue_full` |

If service discovery hasn't completed yet, the call is **queued** (max 64
entries) and replayed once `SEARCH_CMPL_EVT` fires. Overflowing the queue
yields `error=queue_full`.

### `ble_subscribe`

*Available since 1.2.0.*

Enable notifications/indications on a characteristic.

| | |
|---|---|
| **Args** | `service_uuid: string`, `char_uuid: string` |
| **Side-effect** | `esp_ble_gattc_register_for_notify` + CCCD write. Idempotent — duplicate subscribes are silently ignored. |
| **Reply** | None directly. Each notification triggers a `_ble_data` event with `uuid`, `payload`, `mac`, `bridge_id`. |
| **Throttling** | Per-characteristic minimum interval, default 500 ms. Tunable via `ble_set_throttle`. |

Subscriptions are tracked in `desired_subscriptions_` and **automatically
restored** on reconnect.

### `ble_unsubscribe`

*Available since 1.2.0.*

Disable notifications and remove from auto-resubscribe list.

| | |
|---|---|
| **Args** | `service_uuid: string`, `char_uuid: string` |
| **Reply** | None |

### `ble_write_char`

*Available since 1.2.0.*

Write a characteristic with response.

| | |
|---|---|
| **Args** | `service_uuid: string`, `char_uuid: string`, `data: string` (hex, no separators) |
| **Side-effect** | `esp_ble_gattc_write_char` with `WRITE_TYPE_RSP`. |
| **Reply** | None — success/failure only in the ESP log |

Hex parsing rejects malformed input silently (warning in log, no event).

### `ble_set_throttle`

*Available since 1.2.0.*

Adjust the minimum interval between notification events forwarded to HA.

| | |
|---|---|
| **Args** | `throttle_ms: string` (uint, in ms) |
| **Side-effect** | `notify_throttle_ms_` is updated globally for this bridge. |
| **Reply** | None |

Invalid values (non-numeric, trailing junk) are rejected with a log warning;
the previous value is kept.

### `ble_get_info`

*Available since 1.2.0. Extended with `mode`, `pair_capable`, `pair_mode_active`, `identity_address` in 1.3.0.*

Snapshot of bridge + brush state. **Primary capability-detection call** for HA
during config flow.

| | |
|---|---|
| **Args** | — |
| **Reply** | `_ble_status` event with `status="info"` |

**Event fields:**

| Field | Type | Note |
|---|---|---|
| `status` | `"info"` | |
| `mode` | `"external"` \| `"standalone"` | |
| `pair_capable` | `"true"` \| `"false"` | True only when standalone + no MAC + not currently connected |
| `pair_mode_active` | `"true"` \| `"false"` | Currently in pair-mode window |
| `identity_address` | string | Persistent BLE identity (post-bond). Empty if no identity persisted. Same value as in `pair_complete`. |
| `ble_connected` | `"true"` \| `"false"` | |
| `paired` | `"true"` \| `"false"` | True if BD addr appears in `esp_ble_get_bond_device_list` |
| `mac` | string | Currently used remote MAC (may be RPA pre-bond) |
| `ble_name` | string (optional) | GAP 0x2A00 |
| `model` | string (optional) | DeviceInfo 0x2A24 |
| `uptime_s`, `free_heap`, `subscriptions`, `notify_throttle_ms`, `version`, `bridge_id` | misc | Diagnostic |

### `ble_list_services`

*Available since 1.3.0.*

Enumerate the GATT database of the bonded brush.

| | |
|---|---|
| **Args** | — |
| **Reply** | One `_ble_services` event **per service** |

**Event fields per service:**

| Field | Note |
|---|---|
| `mac`, `bridge_id` | Identity |
| `service_count` | Total services in the GATT DB |
| `service_index` | 0-based index of this entry |
| `service_uuid` | UUID of the service |
| `service_chars` | CSV of `<char_uuid>/<props>` pairs, e.g. `00002a19-…/RN,…/W` (R=read, W=write-rsp, w=write-no-rsp, N=notify, I=indicate) |

HA aggregates all events with matching `mac` + `bridge_id` until
`service_index == service_count - 1`.

---

### `ble_pair_mode`

*Available since 1.3.0.*

Arm or cancel the UUID-scan + auto-pair window.

| | |
|---|---|
| **Args** | `enabled: bool`, `timeout_s: string` (default 60, max 600) |
| **Side-effect** (enable) | Worker switches to UUID-scan, the first Sonicare service it sees triggers connect → SMP. Auto-disables after `timeout_s`. |
| **Side-effect** (disable) | Cancels the timer and disables the worker. |
| **Replies** | One `pair_mode_started`, then exactly one of `pair_complete` / `pair_timeout` / `pair_mode_stopped` |

**Reply events** (`_ble_status`):

| `status` | When | Extra fields |
|---|---|---|
| `pair_mode_started` | Pair-mode armed | `timeout_s` |
| `pair_complete` | Pairing succeeded — identity persisted | `identity_address`, `model` (if read), `ble_name` (if read) |
| `pair_timeout` | Window expired without successful pairing | — |
| `pair_mode_stopped` | Cancelled via `enabled=false` | — |

### `ble_unpair`

*Available since 1.3.0.*

Remove the BLE bond and clear any persisted identity. Bridge ends up with
`pair_capable=true` again.

| | |
|---|---|
| **Args** | — |
| **Side-effect** | `esp_ble_remove_bond_device`, NVS identity wiped (Mode B), `uuid_scan_mode_` re-armed, BLE client cycled to drop the current connection. |
| **Reply** | `_ble_status` event with `status="unpaired"` |

**Event fields:**

| Field | Note |
|---|---|
| `status` | `"unpaired"` |
| `previous_mac` | The MAC that was bonded |

In Mode A, only the BLE bond is removed — the YAML MAC is unaffected and the
bridge will attempt to re-pair on the next connection.

### `ble_scan`

*Available since 1.3.0.*

Discovery-only — list all Sonicares in range without connecting.

| | |
|---|---|
| **Args** | `timeout_s: string` (default 30, max 300) |
| **Side-effect** | Worker observes UUID-matching adverts for `timeout_s` and emits one event per unique MAC. **Does not connect**. Mode B only. Refused while pair-mode is active. |
| **Replies** | One `scan_started`, then multiple `scan_result` (one per unique MAC observed), then one `scan_complete` |

**`scan_started` fields:** `status="scan_started"`, `timeout_s`, `mac`, `bridge_id`

**`scan_result` fields:**

| Field | Note |
|---|---|
| `status` | `"scan_result"` |
| `result_mac` | MAC currently advertising (`AA:BB:CC:DD:EE:FF`) |
| `addr_type` | `"public"` \| `"random"` |
| `local_name` | Possibly empty |
| `mfr_data` | Hex (Company-ID little-endian + payload), possibly empty |
| `rssi` | Signed int as string |
| `service_uuid` | `"legacy"` (`477ea600-…-0001`) or `"condor"` (`e50ba3c0-…`) |

**`scan_complete` fields:** `status="scan_complete"`, `count` (number of unique MACs)

### `ble_pair_mac`

*Available since 1.3.0.*

Targeted pairing — bond a specific MAC instead of the first UUID-match.

| | |
|---|---|
| **Args** | `mac: string` (accepts `AA:BB:CC:DD:EE:FF`, `AABBCCDDEEFF`, or with dashes), `timeout_s: string` (default 60) |
| **Side-effect** | Worker sets internal `target_mac_=normalized(mac)`; `parse_device` matches on that MAC instead of UUID. Otherwise reuses `ble_pair_mode`'s plumbing. Mode B only. |
| **Replies** | `pair_mode_started`, then one of `pair_complete` / `pair_timeout` |
| **Validation** | Invalid MAC (≠ 12 hex chars after stripping separators) → fires `pair_timeout` with extra `error="invalid_mac"` field, no other side-effects |

Use cases this enables:

- HA shows a `ble_scan` result list and lets the user pick which Sonicare to bond
- Power-user enters a MAC manually (from another tool, prior bond, etc.)
- Multi-brush setups where the convenience pair-mode would race

---

## Status events without an explicit trigger

These fire on GAP/GATT lifecycle changes regardless of any service call:

| `status` | Trigger | Extra fields |
|---|---|---|
| `connected` | `OPEN_EVT` succeeded | `mac` |
| `disconnected` | `DISCONNECT_EVT` | `reason` (hex), `mac` |
| `ready` | `SEARCH_CMPL_EVT` (services discovered) | `version`, `mac` |
| `auth_failed` | 3× consecutive `AUTH_CMPL.success=false` | `fail_count`, `backoff_s`, `mac` |
| `heartbeat` | every 15 s, unconditional | `ble_connected`, `version`, `uptime_s`, `mac` |

After OTA, if the bridge is connected with services discovered but has no
active subscriptions, it will re-fire `ready` once on the next heartbeat — so
HA can re-subscribe even if it missed the original event during reboot.
