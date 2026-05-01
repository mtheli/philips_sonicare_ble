# ESP Bridge Changelog

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
