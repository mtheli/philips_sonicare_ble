#pragma once

#include "esphome/core/component.h"
#include "esphome/components/ble_client/ble_client.h"
#include "esphome/components/esp32_ble_tracker/esp32_ble_tracker.h"
#include "esphome/components/api/custom_api_device.h"
#include "esphome/components/binary_sensor/binary_sensor.h"

#include <map>
#include <string>
#include <utility>
#include <vector>

namespace esphome {
namespace philips_sonicare {

static const char *const PHILIPS_SONICARE_VERSION = "1.2.3";

class PhilipsSonicare : public ble_client::BLEClientNode,
                        public Component,
                        public api::CustomAPIDevice {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override {
    return setup_priority::AFTER_BLUETOOTH;
  }

  void gattc_event_handler(esp_gattc_cb_event_t event,
                            esp_gatt_if_t gattc_if,
                            esp_ble_gattc_cb_param_t *param) override;
  void gap_event_handler(esp_gap_ble_cb_event_t event,
                          esp_ble_gap_cb_param_t *param) override;

  void on_read_characteristic(std::string service_uuid,
                               std::string characteristic_uuid);
  void on_subscribe(std::string service_uuid,
                     std::string characteristic_uuid);
  void on_unsubscribe(std::string service_uuid,
                       std::string characteristic_uuid);
  void on_write_characteristic(std::string service_uuid,
                                std::string characteristic_uuid,
                                std::string hex_data);

  void on_set_throttle(std::string throttle_ms);
  void on_get_info();

  void set_connected_sensor(binary_sensor::BinarySensor *sensor) {
    this->connected_sensor_ = sensor;
  }
  void set_notify_throttle(uint32_t ms) { this->notify_throttle_ms_ = ms; }
  void set_bridge_id(const std::string &id) { this->bridge_id_ = id; }

 protected:
  std::string get_device_mac_();
  std::string svc_name_(const std::string &action);

  std::string bridge_id_;

  binary_sensor::BinarySensor *connected_sensor_{nullptr};
  bool connected_{false};
  bool services_discovered_{false};
  uint16_t pending_handle_{0};
  std::string pending_char_uuid_;
  // Cached BLE device name (read from GAP 0x2A00 after service discovery)
  uint16_t name_handle_{0};
  // Pairing probe handle (Sonicare 0x4010, read after service discovery)
  uint16_t probe_handle_{0};
  std::string remote_name_;
  // handle -> char_uuid for active notification subscriptions
  std::map<uint16_t, std::string> notify_map_;
  // char_handle -> cccd_handle for writing notification enable
  std::map<uint16_t, uint16_t> cccd_map_;
  // char_handle -> characteristic properties (to distinguish notify vs indicate)
  std::map<uint16_t, uint8_t> char_props_map_;
  // Subscriptions that should be restored after reconnect (service_uuid, char_uuid)
  std::vector<std::pair<std::string, std::string>> desired_subscriptions_;

  void resubscribe_all_();
  void apply_smp_params_();
  uint16_t find_cccd_handle_(uint16_t char_handle);

  // Notification throttle: min interval between events per characteristic
  uint32_t notify_throttle_ms_{500};
  std::map<uint16_t, uint32_t> last_notify_ms_;

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

  // Heartbeat: periodic status event to HA
  static const uint32_t HEARTBEAT_INTERVAL_MS = 15000;
  uint32_t last_heartbeat_ms_{0};
};

}  // namespace philips_sonicare
}  // namespace esphome
