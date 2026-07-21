# ESP Bridge Changelog

## v1.10.0 — 2026-07-21

- **Bridge reports its build environment.** `ble_get_info` now includes
  `esphome_version` and `idf_version` — which ESPHome/ESP-IDF the running
  firmware was compiled with. The same bridge version behaves differently
  depending on the underlying stack (Bluedroid fixes ship via ESP-IDF), so
  this answers the first support question of any bug report automatically.
  Surfaced in Home Assistant as the new "ESP Build" diagnostic
  sensor. `MIN_BRIDGE_VERSION` stays 1.4.0 (fields are optional).
- **Warning-free builds under the ESPHome 2026.7 native toolchain.** Fixed
  `-Wformat` warnings (uint32_t is `long unsigned int` on the newer
  toolchain) and `-Wempty-body` warnings in `dump_config()`. No behavior
  change.
- **Compile-time guard against the Bluedroid GATT-cache crash.** Building
  with the persistent GATT service cache enabled (`bluetooth_proxy:` with its
  default `cache_services: true`, or `CONFIG_BT_GATTC_CACHE_NVS_FLASH: "y"`)
  on an ESP-IDF version affected by [esphome#15783](https://github.com/esphome/esphome/issues/15783)
  (< 5.5.5, or 6.0.0–6.0.1) now aborts validation with instructions instead
  of producing a firmware that boot-loops during service discovery. Builds
  that wire up the `bluedroid_null_fix.py` pre-build patch are allowed
  through with a reminder that the patch is obsolete from ESPHome 2026.7.1
  (which bundles the fixed ESP-IDF 5.5.5). Build-time change only — no
  firmware behavior change, no version bump.

## v1.9.0 — 2026-07-19

- **New `ble_unpair_mac` service.** Clears a bond by MAC address from the
  controller's shared NVS bond store, independent of any bridge slot's
  identity — useful for an orphaned bond that no slot can target through the
  existing `ble_unpair`. Because the bond store is node-wide, the service is
  registered once per node (not per `bridge_id`), so a multi-slot board
  exposes a single `<node>_ble_unpair_mac` instead of one copy per slot. No
  integration change and no re-configuration needed; `MIN_BRIDGE_VERSION`
  stays 1.4.0.

## v1.8.0 — 2026-07-09

- **Bridge answers the newer-protocol change-indication acknowledgment
  itself.** On the Series 7100 (newer) protocol the brush pushes a change
  indication for every state change and expects the acknowledgment back on
  its sequenced control channel within ~250 ms. Over the bridge that
  acknowledgment made the full Wi-Fi + Home Assistant round-trip, which under
  load occasionally exceeded the window — the brush then tore the link down
  mid-brushing (`reason=0x13`), every 30–90 s during an active session. The
  bridge now owns that channel's outbound sequence: it rewrites the sequence
  of every frame Home Assistant sends, answers each change indication on its
  own BLE thread (~10 ms), and drops Home Assistant's now-redundant
  acknowledgment so exactly one reaches the brush and always inside the
  window. Completes the mid-session-disconnect work started with the
  transport-layer auto-ack in v1.6.0. No integration change and no
  re-configuration needed; older integration versions benefit automatically.
  `MIN_BRIDGE_VERSION` stays 1.4.0.

## v1.7.0 — 2026-07-05

- **Pipelined GATT reads behind a single ATT-op scheduler.** Concurrent
  `ble_read_char` calls used to clobber the single-slot read state,
  silently dropping the earlier read's response. All coordinator-issued
  GATT operations now share one in-flight gate: reads and characteristic
  writes defer through the pending-calls queue while a read, the pairing
  probe, a write, the subscribe burst (notify registrations + CCCD
  descriptor writes) or an SMP handshake is outstanding, and the queue
  drains as a loop on every completion path so synchronously-failing
  calls (`not_found`, `gatt_err_*`) cannot strand the entries behind
  them. Ported from the shaver bridge v1.10.0, where Bluedroid was
  observed three times to lose the response of an operation racing
  another one; hardening carried over:
  - the subscribe-burst gate is armed synchronously at registration so
    the very first queued read cannot race the CCCD writes; new
    `WRITE_DESCR_EVT` handler drains the queue as the burst completes
  - one ATT watchdog: no progress for 10 s force-clears all markers,
    resolves a stuck read to HA as `error=read_timeout` and keeps the
    queue draining instead of wedging until disconnect
  - late `READ_CHAR_EVT`s are matched against the issued handle and
    ignored as stray, so a post-watchdog completion cannot be
    misattributed to the next queued read
  - `DISCONNECT`/unpair cleanup resets all scheduler state

  Sonicare-specific integration: reads parked on the lazy-encryption
  retry (`INSUF_AUTH` → SMP → retry on `AUTH_CMPL`) and the deferred
  eager-SMP window on bonded Classic peers hold the same gate, so the
  queue waits out the handshake instead of bouncing reads off the
  half-encrypted link. The Condor auto-TX_ACK fast path stays ungated —
  it is a no-response write inside the brush's ~250 ms ack window.

  With this scheduler the HA integration (v0.16.0+) fires its poll
  cycle concurrently against bridges reporting v1.7.0 or newer.

- **Connection-parameter visibility.** The bridge now logs the link's
  connection parameters: `Conn params initial: interval=… ms latency=…`
  on connect, and `Conn params now: …` whenever they change (polled on
  read/notify completions, since ESPHome drops the GAP update event
  before it reaches components). Devices may renegotiate these
  parameters by state — the shaver drops to a power-save interval with
  slave latency once idle, which directly bounds how fast a poll batch
  can run; whether the toothbrushes do the same is exactly what these
  INFO lines will show.

## v1.6.1 — 2026-06-25

- **Firmware version is now a single source of truth.** The bridge version
  lives in `esphome/components/philips_sonicare/VERSION` and is baked into
  the firmware at build time (injected as a compile define by `__init__.py`,
  read via the `PHILIPS_SONICARE_BRIDGE_VERSION` macro in `coordinator.h`)
  instead of a hard-coded constant. The Home Assistant integration reads the
  same file from GitHub to power a passive firmware-update notification, so
  new bridge firmware is surfaced without shipping an integration release.
  No behavioural change to the bridge itself — the bump from v1.6.0 only
  reflects the new version plumbing.

  > The firmware-update notification in Home Assistant is supported from
  > **integration version 0.14.0** onwards. Older integration versions
  > ignore this and keep working unchanged.

## v1.6.0 — 2026-05-25

- **Bridge-side Condor TX_ACK** — fixes mid-brushing disconnects reported
  by @itchensen in [issue
  #13](https://github.com/mtheli/philips_sonicare_ble/issues/13). The
  Condor protocol expects a 1-byte ack on `e50b0004` within ~250 ms of
  every `e50b0003` notify; bouncing that ack through the
  Bridge→Wi-Fi→HA→asyncio→Wi-Fi→Bridge round-trip takes 30–300 ms with
  occasional spikes past the patience window, at which point the brush
  tears the link down with `reason=0x13` mid-session. The bridge now
  echoes `data[0] & 0x3F` straight to `e50b0004` on its BLE thread
  (~10 ms, deterministic), and the HA integration gates its own
  `_send_tx_ack` on `bridge_version >= 1.6.0` so the ack runs once,
  on the fast path.

  Triggered only when the inbound UUID is `e50b0003` and at least one
  byte of payload was delivered — legacy brushes and non-Condor chars
  are not touched. Auth/write-type follow the same logic the
  Bridge already uses for outbound writes (`AUTH_REQ_NO_MITM` for
  bonded peers, `WRITE_TYPE_NO_RSP`). The `e50b0004` handle is resolved
  once per connection and cached; an `INFO` log on first resolution
  marks the fast path active. A `WARN` log fires only when the write
  itself errors (`status != ESP_OK`).

- **`MIN_BRIDGE_VERSION` stays at 1.4.0** — fully backwards-compatible.
  Old HA + new bridge yields a harmless duplicate 1-byte ack (HA still
  sends its ack, bridge also auto-acks, brush sees the same seq
  twice). New HA + old bridge behaves exactly like before. Direct-BLE
  users get no behaviour change — `auto_tx_ack` defaults to `False`
  on the abstract transport.

## v1.5.3 — 2026-05-22

- **Reconnect-lag improvements (PR #17 Improvements 1+2)**, contributed
  by @jjsmackay. Together with the v1.5.1 NVS cache (Improvement 3),
  this completes the PR #17 reconnect-time optimization series:

  - **Pipelined GATT reads** (Improvement 1a) — fire multiple outstanding
    reads instead of strictly sequential, shaving the post-connect
    read-storm.
  - **Race-rescue for reads racing the SMP handshake** (Improvement 1b)
    — a read that fires before encryption is up gets re-queued instead
    of dropped, no longer losing the first poll cycle on bonded brushes.
  - **Eager SMP on `SEARCH_CMPL` for bonded Classic peers** (Improvement
    2a) — kick the auth exchange the instant service discovery
    completes, rather than waiting for the first auth-required read to
    trigger it lazily.

  Two maintainer-side fixes layered on top of jjsmackay's submission,
  needed to make eager SMP reliable on bonded ExpertClean models:

  - **Defer eager SMP until `ESP_GATTC_CFG_MTU_EVT`** — kicking SMP
    immediately after `SEARCH_CMPL` was too early on HX960V; the brush
    dropped the SMP request and we'd hit a 6 s supervision-timeout
    loop. Defering to MTU_EVT (~100 ms later) lands cleanly.
  - **`ESP_BLE_SEC_ENCRYPT_MITM` instead of `ENCRYPT`** — HX960V's
    existing bond was MITM-derived; the weaker auth level caused
    `SMP_UNKNOWN_ERR` reason 0x61. Using ENCRYPT_MITM matches the
    bond's key derivation and the re-encryption succeeds.

  Live-tested 2026-05-20 across all four brushes (HX9992 Prestige /
  HX992X Sonicare 9000 / HX6340 Kids / HX960V ExpertClean 7300). All
  4 reconnect with 0 stale reads (baseline had 4–5 stale reads per
  reconnect). Stable with 3 brushes connected simultaneously; the 4th
  is cleanly refused by the v1.5.2 heap backpressure (RAM hardware
  limit on M5Stack Atom Lite, not a PR #17 issue — see
  [`esphome/SETUP.md`](SETUP.md#multi-device-setup)).

- **`MIN_BRIDGE_VERSION` stays at 1.4.0** — backwards-compatible with
  older bridges. Improvement 1a includes one HA-side change
  (`transport.py::read_chars` switches from serial to `asyncio.gather`),
  but the parallel-read path would silently drop reads on bridges
  before v1.5.3 (single-slot `pending_handle_` clobbering). HA now
  gates the gather on the reported bridge version: bridges ≥1.5.3 get
  the full pipelining speedup (~250–500 ms saved on the post-connect
  read phase), older bridges keep the original serial behaviour with no
  regression. Improvements 1b and 2a are bridge-only and need no
  HA-side gating.

## v1.5.2 — 2026-05-22

- **Heap monitoring + pre-connect backpressure** for multi-brush setups.

  The bridge now logs a periodic warning when free heap drops below 35 KB
  and refuses new BLE connection attempts when below 25 KB. Together,
  the two prevent the watchdog-reboot we measured on M5Stack Atom Lite
  with 4 brushes (heap exhaustion → blocking `tcp_write` in
  `ListEntitiesServicesResponse` → loopTask hung → `task_wdt: Aborting`).

  Behavior is invisible until the bridge approaches its RAM limit. New
  log lines:

  ```
  [W][philips_sonicare.heap]: Heap low: free=14816 min_ever=6248 largest_block=12800
  [W][philips_sonicare.<id>]: Refusing new connection attempt: free heap 22080 below safety threshold 25000
  ```

  Live-tested on Atom Lite with 4 brushes (HX9992 / HX992X / HX6340 /
  HX960V). 3 brushes connect cleanly; the 4th is refused with the heap
  at ~22 KB free instead of crashing the bridge. See the new "RAM limit
  on single-core ESP32 boards" callout in
  [`esphome/SETUP.md`](SETUP.md#multi-device-setup)
  for board guidance.

- **New sample config: [`atom-lite-triple.yaml`](atom-lite-triple.yaml)**
  for 3-brush setups on Atom Lite. Bumps `CONFIG_BT_GATTC_NOTIF_REG_MAX`
  to 50 (3 × 11 subs + headroom), trims `CONFIG_BT_ACL_CONNECTIONS` and
  `CONFIG_BTDM_CTRL_BLE_MAX_CONN` to 5 (4 wasted ~8 KB heap in our
  prior 7-slot config), and sets `max_notifications: 50` to match.

- **`MIN_BRIDGE_VERSION` stays at 1.4.0** — these changes are additive,
  no HA-side schema change.

## v1.5.1 — 2026-05-20

- **Optional GATT cache persistence to NVS** for fast reconnects.
  Contributed by @jjsmackay via PR #17 (Improvement 3 of 3).

  Off by default — opt in via `CONFIG_BT_GATTC_CACHE_NVS_FLASH: "y"` in
  your `sdkconfig_options`. First connect to a peer pays the full ~4 s
  service discovery (cache miss); subsequent reconnects to the same
  peer hit the NVS cache in single-digit milliseconds.

  Live-tested 2026-05-20 across four brush models (HX9992 bonded /
  HX992X unbonded / HX6340 Kids unbonded / HX960V bonded), all PUBLIC
  BLE addresses. Measured SDP-phase reduction after one warmup cycle:

  | Brush | SDP baseline | SDP cache-hit | Total reconnect win |
  |---|---|---|---|
  | HX9992 (Prestige 9900) | 3.4 s | 8 ms | 5.7 s → 2.8 s |
  | HX992X (Sonicare 9000) | 3.9 s | 1 ms | 6.1 s → 2.4 s |
  | HX6340 (Sonicare for Kids) | 5.8 s | 1 ms | 9.1 s → 1.0 s |
  | HX960V (ExpertClean 7300) | 3.7 s | 2 ms | 6.7 s → 3.3 s |

  Cache works for both bonded and unbonded peers when the BLE address
  is stable (PUBLIC). The "keyed by bonded peer identity" claim in the
  PR description turns out to be more permissive than expected — any
  stable BDA gets cached. RPA peers (rotating identity) would not
  benefit; none of the test brushes use RPA.

  Requires the `bluedroid_null_fix.py` pre-build script (already in
  this repo's `esphome/` directory) to dodge an ESP-IDF 5.5 crash
  inside `bta_gattc_cache_save` when the flag is enabled. The same
  script covers the `bluetooth_proxy` coexistence crash
  ([esphome#15783](https://github.com/esphome/esphome/issues/15783)),
  so the `bluetooth_proxy:` comment block in the reference YAMLs is
  simplified to a single-line opt-in.

  Users opting in need to drop `bluedroid_null_fix.py` next to their
  YAML (HAOS: `/config/esphome/`) and re-flash. No HA-side change
  required; `MIN_BRIDGE_VERSION` stays at `1.4.0` since this is a
  purely additive bridge feature.

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

- **Boot log shows `friendly_name` and `area`** alongside `Bridge ID`
  in `dump_config`. Quick verification that the YAML config you wrote
  is actually what got flashed.

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
  `ble_list_services` — see `esphome/PROTOCOL.md`.
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
