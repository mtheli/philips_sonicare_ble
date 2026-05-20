# ESP Bridge Changelog

## v1.5.0 — 2026-05-20

- **Per-slot `friendly_name:` and `area:` fields** for each `philips_sonicare:`
  slot. Both ship in the `ble_get_info` payload alongside `bridge_id` etc.
  Contributed by @jjsmackay via PR #16.

  - `friendly_name` pre-fills the HA "Name" prompt during setup and serves
    as the slot label in the multi-brush picker, so users can tell which
    slot is which physical brush before installing.
  - `area` auto-assigns the new device to that HA area on first install
    via `DeviceInfo.suggested_area`, with a setup-time backfill that
    fills the area if currently unset (never overwrites manual area).

  Both fields are one-shot defaults — YAML edits after install do not
  rename existing devices or move them between areas. Backward-compatible
  payload: bridges that omit the fields emit empty strings; older HA
  integrations ignore unknown payload keys. No `MIN_BRIDGE_VERSION` bump
  required to keep older bridges working, but the feature only takes
  effect after reflashing to v1.5.0+.

  See [esphome/README.md#per-slot-defaults](README.md#per-slot-defaults-friendly_name-and-area)
  for usage.

## v1.4.3 — 2026-05-19

- **Python-component fix, no firmware change.** Reflash optional — the
  fix ships via `external_components` on the next ESPHome rebuild.

- Contributed by @jjsmackay via PR #14: Mode B (Auto-Discovery) configs
  that include the `connected:` binary sensor failed validation with
  `'ble_client_id' is a required option`, even though the config has no
  `ble_client_id` and clearly targets Mode B. The deferred ID generation
  inside `binary_sensor.binary_sensor_schema()` fired during Mode A's
  validation attempt and polluted `cv.Any`'s backtracking, so Mode B
  could no longer be entered.

  Fix replaces `cv.Any(_EXTERNAL_SCHEMA, ...)` with an explicit
  key-based dispatcher (`_validate_config`) that routes by the presence
  of `ble_client_id` in the raw config — no backtracking, no
  deferred-lookup pollution. `to_code` and `_internal_set_defaults` are
  untouched, Mode A behaviour is byte-identical.

  HA-side `MIN_BRIDGE_VERSION` stays at `"1.4.0"` — the bump from
  `1.4.2` is a tracking marker only, no Bridge feature became
  conditional on it.

## v1.4.2 — 2026-05-11

- Fixes Condor-protocol (HX742X / Series 7100) via the ESP bridge — the
  V4 handshake stalled after Phase 1 and no port data ever flowed.
  Reported by @itchensen on issue #13.

  Root cause: the bridge's per-handle notification throttle (default 500 ms)
  silently dropped notifications that arrived within the throttle window
  on the same characteristic. Condor is built on three notify channels
  that all violate this assumption:

  - **SERVER_CFG (`e50b0006`)** — the V4 handshake delivers two
    responses on this handle ~100 ms apart (v-negotiation reply, then
    channel-config reply after the `FFFFFFFF` write). The throttle let
    the first through and dropped the second, so the Python side timed
    out waiting on `_await_server_cfg(6)` and tore the session down.
  - **TX (`e50b0003`)** — framed protocol; a single JSON-port update
    spans several back-to-back notifications. Throttling fragments
    frames, leaving the Python reassembler with permanently incomplete
    buffers.
  - **RX_ACK (`e50b0002`)** — per-frame flow-control acks from the
    device; dropping any stalls the send window.

  Fix: skip the throttle entirely for these three UUIDs. Classic-protocol
  CCCD streams still throttle (the dampening was added for those in the
  first place), and Condor already coalesces at the protocol layer via
  ChangeIndication deltas, so the bridge-side rate-limit is redundant
  for it.

  No HA-side change required. `MIN_BRIDGE_VERSION` stays at `"1.4.0"` —
  users with Classic brushes (HX9992 / HX6340 / Prestige) are unaffected
  and don't need to reflash. Condor users on the bridge **must** update
  to v1.4.2 — the bug makes their integration non-functional otherwise.

## v1.4.1 — 2026-05-10

- Fixes Condor-protocol (HX742X / Series 7100) writes through the ESP bridge.
  Two issues that had stayed invisible because Condor had only been validated
  via the direct-BLE probe script, not through the bridge:

  1. **Write-type mismatch:** `write_characteristic` used
     `ESP_GATT_WRITE_TYPE_RSP` for every char. Condor's `e50b0007`
     (Client Config — the channel-negotiation char) is declared
     write-without-response only. The brush replied with ATT
     `WRITE_NOT_PERMIT` (status=3), the channel never opened, and no
     port data flowed. Fix: read the declared `properties` from the
     `BLECharacteristic` and pick `WRITE_TYPE_NO_RSP` for write-NR-only
     chars. Legacy chars (Sonicare service `477ea6xx-…4020/4022/4420`)
     have the `WRITE` bit set and stay on `WRITE_TYPE_RSP`.

  2. **Encryption not restored on RPA-rotated reconnect:** writes used
     `ESP_GATT_AUTH_REQ_NONE`, which never asks Bluedroid to re-encrypt
     the link from a stored bond. When the brush reconnected under a
     fresh resolvable private address, encrypted Condor writes failed
     with ATT `INSUFF_ENCRYPTION` (status=15) even though the bond was
     intact in NVS. Fix: cache bond status per-peer (`peer_is_bonded_`,
     refreshed on `OPEN_EVT`, on `AUTH_CMPL` success and on `unpair()`)
     and request `ESP_GATT_AUTH_REQ_NO_MITM` for bonded peers. Unbonded
     peers (open-GATT brushes like HX6340 Kids) keep `AUTH_REQ_NONE` —
     forcing encryption on them would break writes that work fine
     without it.

  No protocol/state-machine changes; existing HX9992 / HX6340 / Prestige
  flows are unaffected (Legacy chars keep `WRITE_TYPE_RSP`; HX6340 stays
  on `AUTH_REQ_NONE`). HA-side `MIN_BRIDGE_VERSION` stays at `"1.4.0"` —
  users with non-Condor brushes don't need to reflash.

## v1.4.0 — 2026-05-04

- Adds `identity_source` to `ble_get_info` and `pair_complete` event payloads.
  Three values: `"yaml"` (Mode A or Mode B with `mac_address:` — identity is
  pinned by YAML and re-applied on every boot), `"nvs"` (Mode B auto-discovery —
  identity persisted to flash via `ble_pair_mode`), `"none"` (Mode B unpaired,
  waiting for the next `ble_pair_mode`).

  HA's in-place reconfigure flow needs this to decide whether a bridge can be
  retargeted at runtime: only `"nvs"` is reconfigurable, since `ble_unpair`
  wipes the NVS slot. YAML-pinned identities (Mode A and Mode B with a fixed
  MAC) require a YAML rebuild + reflash to retarget — the integration aborts
  the reconfigure flow with a clear "pinned by YAML" error in those cases.

  State transitions during runtime: `none → nvs` on successful pair_complete
  in Mode B auto-discovery; `nvs → none` on `ble_unpair` in Mode B
  auto-discovery; `yaml` never transitions.

  Cosmetic only for users on a single bridge — no behaviour change today.
  The field is purely additive and existing flows ignore it, so HA-side
  `MIN_BRIDGE_VERSION` stays at `"1.3.2"` for the remaining v0.10.x betas.
  The bump to `"1.4.0"` will land with the next main HA release so that
  the reconfigure flow and any other consumer can rely on the field being
  present without an `if "identity_source" in info:` guard.

## v1.3.2 — 2026-05-01

- Robust unpair: failed `ble_unpair` no longer wedges the BLE stack until
  reboot. Use the persistent `identity_address_` as the source of truth for
  `esp_ble_remove_bond_device()` (the live `parent_->get_remote_bda()` was
  stale or zero during teardown), check the return code instead of silently
  proceeding, drain queued GATT calls, and defer the BLE-client re-enable +
  `unpaired` event by 2 s so the GAP disconnect and any in-flight
  notifications can settle. The `unpaired` event now reliably fires after
  the bridge is actually back in UUID-scan mode.
- Safety-net bond-list sweep — filtered to entries matching the brush's
  identity only. The ESP NVS bond list is global across all BLE clients on
  the chip (multiple `philips_sonicare:` bridges, `philips_shaver:`, etc.),
  so unfiltered iteration would silently un-bond unrelated devices.
- Bumps `MIN_BRIDGE_VERSION` to `1.3.2` on the HA side — the unpair-wedge
  has bricked entry-removal flows for affected users; the integration warns
  if a bridge older than 1.3.2 is in use.

## v1.3.1 — 2026-05-01

- Per-instance log tag for multi-bridge setups: every `ESP_LOG` call routes
  through `philips_sonicare` (single-bridge) or
  `philips_sonicare.<bridge_id>` (multi-bridge), so each bridge's lines are
  identifiable in the log stream. `logger:` filters can now target a single
  bridge by suffix:
  ```yaml
  logger:
    logs:
      philips_sonicare.kids: WARN
      philips_sonicare.prestige: DEBUG
  ```
  Cosmetic only — no `MIN_BRIDGE_VERSION` bump; v1.3.0 keeps working.

## v1.3.0 — 2026-04-28

- Mode B (standalone): `philips_sonicare:` no longer needs an external
  `ble_client:` block. `PhilipsSonicareStandalone` extends `BLEClientBase`
  directly; identity address persists in NVS so RPA brushes (e.g. Series
  7100 / HX742X) reconnect across reboots without YAML-pinned MAC.
- New HA services for Mode B: `ble_pair_mode(enabled, timeout_s)`,
  `ble_unpair`, `ble_scan(timeout_s)`, `ble_pair_mac(mac, timeout_s)`,
  `ble_list_services` — see `docs/ESP32_PROTOCOL.md`.
- `ble_get_info` extended with `mode`, `pair_capable`, `pair_mode_active`,
  `identity_address` so HA's config flow can decide whether to show the
  pair dialog or go straight to capability detection.
- Architecture refactor: `SonicareCoordinator` (BLE/GATT logic, mode-agnostic)
  separated from `SonicareBridge` (HA service registration, events,
  heartbeat). HA-side service calls that arrive between OPEN and
  SEARCH_CMPL are queued (max 64) and replayed once service discovery
  completes — fixes "Initializing"-hang on reconnect.
- Multi-bridge: `bridge_id` exposed alongside `mac` in `on_connect` /
  `on_disconnect` triggers; service-name suffix per bridge so HA can
  address each slot separately.
- Open-GATT brushes (HX6340 Kids, HX992X DiamondClean Smart) now emit
  `pair_complete` via the unified probe-read path — no SMP / AUTH_CMPL
  needed, model + ble_name come along on the event.
- Pair-mode bypasses the auth-failure backoff (60 s lockout previously
  killed pair-mode windows on first SMP retry).

## v1.2.3 — 2026-04-22

- Include `uptime_s` in `heartbeat` and `ready` events (previously only
  in `info`). Enables HA to detect bridge restarts via uptime
  regression and clear stale subscription state, so auto-resubscribe
  triggers when the API reconnects after an ESP reboot — even without
  HA actively requesting `ble_get_info`.

## v1.2.2 — 2026-04-06

- Skip duplicate subscribe when subscriptions are already restored after
  reconnect. Avoids redundant CCCD writes and speeds up reconnection.
- Keep desired_subscriptions across BLE disconnects so they can be
  restored immediately on reconnect.

## v1.2.1 — 2026-04-06

- Fix: Don't fire "ready" event before GATT service discovery completes.
  HA was reading characteristics before the service table was populated,
  causing "not found" warnings and missed initial data reads.

## v1.2.0 — 2026-04-01

- CCCD fix: Use `esp_ble_gattc_get_descr_by_char_handle()` instead of
  ESPHome's internal cache (which had a bug causing subscribe loops).
- SMP pairing stack with auth backoff and stale bond detection.
- Notification throttle support (configurable via HA).
- Bridge version reporting and HA repair issue for outdated firmware.

## v1.1.0 — 2026-03-19

- Auto-detect indicate vs notify characteristics, log CCCD value.
- Log write characteristic response status.

## v1.0.0 — 2026-03-18

- Initial ESP32 Bridge release.
- BLE client for Philips Sonicare toothbrushes.
- Read, write, subscribe/unsubscribe via ESPHome service calls.
- Status events (heartbeat, connected, disconnected, ready).
- Connected binary sensor and status LED support.
