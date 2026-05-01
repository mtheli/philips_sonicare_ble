#pragma once

#include "esphome/core/component.h"
#include "esphome/components/api/custom_api_device.h"
#include "esphome/components/binary_sensor/binary_sensor.h"

#include <map>
#include <string>

namespace esphome {
namespace philips_sonicare {

class SonicareCoordinator;  // forward — defined in coordinator.h

// HA-side glue: registers services, fires events, manages connected sensor.
// Holds a pointer to the SonicareCoordinator and forwards service calls to it.
// The Coordinator pushes events back through this class.
class SonicareBridge : public Component, public api::CustomAPIDevice {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override {
    return setup_priority::AFTER_BLUETOOTH;
  }

  void set_coordinator(SonicareCoordinator *coord) { this->coord_ = coord; }
  void set_bridge_id(const std::string &id) { this->bridge_id_ = id; }
  // Per-instance log tag, set in to_code(): "philips_sonicare" (single-bridge)
  // or "philips_sonicare.<bridge_id>" (multi-bridge). Used by all ESP_LOG calls
  // in this class so each bridge's lines are distinguishable in the log stream
  // and the logger: filter can target a single bridge by suffix.
  void set_log_tag(const std::string &tag) { this->log_tag_ = tag; }
  void set_connected_sensor(binary_sensor::BinarySensor *s) {
    this->connected_sensor_ = s;
  }

  const std::string &get_bridge_id() const { return bridge_id_; }

  // Called by Coordinator to publish events / sensor states
  void fire_event(const std::string &event_type,
                   const std::map<std::string, std::string> &data);
  void publish_connected(bool connected);

 protected:
  SonicareCoordinator *coord_{nullptr};
  std::string bridge_id_;
  std::string log_tag_;  // see set_log_tag() — fallback to file-scope TAG until set
  binary_sensor::BinarySensor *connected_sensor_{nullptr};
  uint32_t last_heartbeat_ms_{0};
  static const uint32_t HEARTBEAT_INTERVAL_MS = 15000;

  std::string svc_name_(const std::string &action);

  // HA service callbacks — thin shims that forward to coord_
  void on_read_characteristic(std::string service_uuid, std::string char_uuid);
  void on_subscribe(std::string service_uuid, std::string char_uuid);
  void on_unsubscribe(std::string service_uuid, std::string char_uuid);
  void on_write_characteristic(std::string service_uuid,
                                std::string char_uuid,
                                std::string hex_data);
  void on_set_throttle(std::string throttle_ms);
  void on_get_info();
  void on_list_services();
  void on_pair_mode(bool enabled, std::string timeout_s);
  void on_unpair();
  void on_scan(std::string timeout_s);
  void on_pair_mac(std::string mac, std::string timeout_s);
};

}  // namespace philips_sonicare
}  // namespace esphome
