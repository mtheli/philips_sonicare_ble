# ESP Bridge Changelog

## v1.2.2

- Skip duplicate subscribe when subscriptions are already restored after
  reconnect. Avoids redundant CCCD writes and speeds up reconnection.
- Keep desired_subscriptions across BLE disconnects so they can be
  restored immediately on reconnect.

## v1.2.1

- Fix: Don't fire "ready" event before GATT service discovery completes.
  HA was reading characteristics before the service table was populated,
  causing "not found" warnings and missed initial data reads.

## v1.2.0

- CCCD fix: Use `esp_ble_gattc_get_descr_by_char_handle()` instead of
  ESPHome's internal cache (which had a bug causing subscribe loops).
- SMP pairing stack with auth backoff and stale bond detection.
- Notification throttle support (configurable via HA).
- Bridge version reporting and HA repair issue for outdated firmware.

## v1.1.0

- Initial ESP32 Bridge release.
- BLE client for Philips Sonicare toothbrushes.
- Read, write, subscribe/unsubscribe via ESPHome service calls.
- Status events (heartbeat, connected, disconnected, ready).
- Connected binary sensor and status LED support.
