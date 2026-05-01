#pragma once

#include "esphome/components/esp32_ble_client/ble_client_base.h"
#include "esphome/components/esp32_ble_tracker/esp32_ble_tracker.h"

#include <esp_gap_ble_api.h>
#include <esp_gattc_api.h>

#include <deque>
#include <functional>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace esphome {
namespace philips_sonicare {

class SonicareBridge;  // forward — defined in bridge.h

// HA event names — used by both Worker and Bridge
static const char *const PHILIPS_SONICARE_VERSION = "1.3.2";
static const char *const EVENT_STATUS = "esphome.philips_sonicare_ble_status";
static const char *const EVENT_DATA = "esphome.philips_sonicare_ble_data";
static const char *const EVENT_SERVICES = "esphome.philips_sonicare_ble_services";

// Bridge mode (reported in collect_info_data + heartbeat)
static const char *const MODE_EXTERNAL = "external";    // Mode A
static const char *const MODE_STANDALONE = "standalone"; // Mode B

// BLE/GATT logic for a single Philips Sonicare brush. Mode-agnostic: works
// with any esp32_ble_client::BLEClientBase parent (external ble_client::BLEClient
// in Mode A, or our own BLEClientBase subclass in Mode B).
//
// Receives raw GAP/GATT events from the wrapper, manages subscription state,
// auth tracking and throttling. Emits HA events through SonicareBridge.
class SonicareCoordinator {
 public:
  // ── Lifecycle ─────────────────────────────────────────────────────────────
  void set_parent(esp32_ble_client::BLEClientBase *parent) { this->parent_ = parent; }
  void set_bridge(SonicareBridge *bridge) { this->bridge_ = bridge; }
  void set_set_enabled_cb(std::function<void(bool)> cb) {
    this->set_enabled_cb_ = std::move(cb);
  }
  void set_on_ready_cb(
      std::function<void(const std::string &, const std::string &)> cb) {
    this->on_ready_cb_ = std::move(cb);
  }
  void set_on_disconnect_cb(
      std::function<void(const std::string &, const std::string &)> cb) {
    this->on_disconnect_cb_ = std::move(cb);
  }
  void set_notify_throttle(uint32_t ms) { this->notify_throttle_ms_ = ms; }
  // Per-instance log tag, set in to_code(): "philips_sonicare" (single-bridge)
  // or "philips_sonicare.<bridge_id>" (multi-bridge). Used by all ESP_LOG calls
  // in this class so each bridge's lines are distinguishable in the log stream
  // and the logger: filter can target a single bridge by suffix.
  void set_log_tag(const std::string &tag) { this->log_tag_ = tag; }
  // Mode + bound MAC are reported in collect_info_data / heartbeat so HA can
  // detect whether this Bridge supports the new pair-mode flow.
  void set_mode(const std::string &mode) { this->mode_ = mode; }
  void set_identity_address(const std::string &mac) { this->identity_address_ = mac; }
  // Called by HA service ble_pair_mode. enable=true arms UUID-scan for
  // timeout_s seconds; enable=false cancels. Only meaningful in Mode B.
  void set_pair_mode(bool enable, uint32_t timeout_s);
  // Worker (Mode B) calls this to register the pair-mode-active getter so
  // parse_device() can gate the UUID-scan branch on it.
  bool is_pair_mode_active() const { return pair_mode_active_; }
  // Called by HA service ble_unpair. Removes the BLE bond, clears any cached
  // identity address (Worker-side via callback) and disconnects.
  void unpair();
  // Worker registers a callback that wipes its own NVS-persisted identity and
  // resets uuid_scan_mode_ when the user requests unpair.
  void set_unpair_cb(std::function<void()> cb) {
    this->unpair_cb_ = std::move(cb);
  }
  // Worker registers a callback that persists the *current* remote address
  // as identity. Called for open-GATT brushes (HX6340/HX992X) where there's
  // no AUTH_CMPL — the bonded path saves NVS in its own gap_event_handler.
  void set_save_identity_cb(std::function<void()> cb) {
    this->save_identity_cb_ = std::move(cb);
  }
  // Discovery-only: arm UUID-scan for timeout_s seconds, emit one scan_result
  // event per unique MAC observed, then scan_complete. Does NOT connect.
  void set_scan_mode(uint32_t timeout_s);
  bool is_scan_mode_active() const { return scan_mode_active_; }
  // Worker calls this for each UUID-matching advert during scan-mode.
  // Internally deduplicates by MAC.
  void emit_scan_result(const std::string &mac,
                        const std::string &addr_type,
                        const std::string &local_name,
                        const std::string &mfr_data,
                        int rssi,
                        const std::string &service_uuid);
  // Targeted pairing: arm pair-mode but only connect to one specific MAC
  // (not the first UUID-match). MAC is normalized to "AA:BB:CC:DD:EE:FF".
  void set_pair_mac(const std::string &mac, uint32_t timeout_s);
  const std::string &get_target_mac() const { return target_mac_; }

  // ── Event entry points (from Worker) ──────────────────────────────────────
  void on_gattc_event(esp_gattc_cb_event_t event,
                       esp_gatt_if_t gattc_if,
                       esp_ble_gattc_cb_param_t *param);
  void on_gap_event(esp_gap_ble_cb_event_t event,
                     esp_ble_gap_cb_param_t *param);
  void on_loop(uint32_t now_ms);

  // ── HA service forwarding (from Bridge) ───────────────────────────────────
  void read_characteristic(const std::string &service_uuid,
                            const std::string &char_uuid);
  void subscribe(const std::string &service_uuid,
                  const std::string &char_uuid);
  void unsubscribe(const std::string &service_uuid,
                    const std::string &char_uuid);
  void write_characteristic(const std::string &service_uuid,
                             const std::string &char_uuid,
                             const std::string &hex_data);
  void list_services();  // enumerate GATT services + characteristics → HA event
  std::map<std::string, std::string> collect_info_data();

  // ── State queries (from Bridge) ───────────────────────────────────────────
  bool is_connected() const { return connected_; }
  bool are_services_discovered() const { return services_discovered_; }
  size_t subscription_count() const { return notify_map_.size(); }
  std::string get_device_mac();
  const std::string &get_remote_name() const { return remote_name_; }
  const std::string &get_model_number() const { return model_number_; }
  uint32_t get_notify_throttle() const { return notify_throttle_ms_; }

 protected:
  esp32_ble_client::BLEClientBase *parent_{nullptr};
  SonicareBridge *bridge_{nullptr};
  std::string log_tag_;  // see set_log_tag() — fallback to file-scope TAG until set
  std::function<void(bool)> set_enabled_cb_;
  std::function<void(const std::string &, const std::string &)> on_ready_cb_;
  std::function<void(const std::string &, const std::string &)> on_disconnect_cb_;
  std::function<void()> unpair_cb_;
  std::function<void()> save_identity_cb_;

  // Mode B exposes its uuid_scan/identity state via these strings; Mode A
  // sets mode_=external and identity_address_=YAML-MAC at setup. collect_info_data
  // forwards them so HA can decide pair_capable.
  std::string mode_;       // MODE_EXTERNAL or MODE_STANDALONE
  std::string identity_address_;  // empty if no identity persisted

  // Pair-mode (Mode B only): UUID-scan only happens while this is true.
  bool pair_mode_active_{false};
  uint32_t pair_mode_until_ms_{0};
  // Unpair drain window: after unpair() force-disables the BLE client and wipes
  // the bond, on_loop() waits this long before re-enabling and emitting the
  // `unpaired` status — so the GAP_DISCONNECT and any in-flight notifications
  // have time to settle. Without the wait, a fast re-enable raced the disconnect
  // and could wedge the BLE stack until reboot.
  bool unpair_pending_{false};
  uint32_t unpair_until_ms_{0};
  std::string unpair_previous_mac_;
  static const uint32_t UNPAIR_DRAIN_MS = 2000;
  // Scan-only mode (Mode B only): observe but never connect.
  bool scan_mode_active_{false};
  uint32_t scan_mode_until_ms_{0};
  std::set<std::string> scan_seen_macs_;
  // Targeted pair: connect to this exact MAC instead of first UUID-match.
  // Only honored while pair_mode_active_ is true.
  std::string target_mac_;

  bool connected_{false};
  bool services_discovered_{false};
  uint16_t pending_handle_{0};
  std::string pending_char_uuid_;
  std::string pending_service_uuid_;
  // Read deferred until pairing completes (INSUF_AUTH/ENCR → encryption →
  // retry on AUTH_CMPL success). Only one read can be in-flight at a time.
  bool retry_read_after_auth_{false};
  // Cached BLE device name (read from GAP 0x2A00 after service discovery)
  uint16_t name_handle_{0};
  // Pairing probe handle (Sonicare 0x4010, read after service discovery)
  uint16_t probe_handle_{0};
  // Model number handle (Device Info 0x180A → 0x2A24, read after discovery)
  uint16_t model_handle_{0};
  std::string remote_name_;
  std::string model_number_;
  // handle -> char_uuid for active notification subscriptions
  std::map<uint16_t, std::string> notify_map_;
  // char_handle -> cccd_handle for writing notification enable
  std::map<uint16_t, uint16_t> cccd_map_;
  // char_handle -> characteristic properties (to distinguish notify vs indicate)
  std::map<uint16_t, uint8_t> char_props_map_;
  // Subscriptions that should be restored after reconnect (service_uuid, char_uuid)
  std::vector<std::pair<std::string, std::string>> desired_subscriptions_;

  // Notification throttle: min interval between events per characteristic
  uint32_t notify_throttle_ms_{500};
  std::map<uint16_t, uint32_t> last_notify_ms_;

  // HA service calls that arrived between OPEN_EVT and SEARCH_CMPL_EVT.
  // Drained in order after service discovery completes.
  std::deque<std::function<void()>> pending_calls_;
  static const size_t MAX_PENDING_CALLS = 64;

  // Encryption: only request after INSUF_AUTH on read (not unconditionally)
  bool encryption_requested_{false};

  // Auth tracking
  bool auth_completed_{false};
  uint32_t connect_time_ms_{0};
  uint8_t rapid_disconnect_count_{0};
  static const uint8_t MAX_RAPID_DISCONNECTS = 3;
  static const uint32_t RAPID_DISCONNECT_THRESHOLD_MS = 5000;

  // Auth failure backoff: disable reconnection after repeated failures
  uint8_t auth_fail_count_{0};
  uint32_t backoff_until_ms_{0};
  static const uint8_t MAX_AUTH_FAILURES = 3;
  static const uint32_t AUTH_BACKOFF_MS = 60000;  // 60 seconds

  // Helpers
  void resubscribe_all_();
  void apply_smp_params_();
  uint16_t find_cccd_handle_(uint16_t char_handle);
  void emit_(const std::string &event_type,
              const std::map<std::string, std::string> &data);
  void emit_status_(const std::string &status,
                     std::map<std::string, std::string> extra = {});
  void emit_data_(const std::string &uuid,
                   const std::string &payload,
                   const std::string &error = "");
};

}  // namespace philips_sonicare
}  // namespace esphome
