#include "bridge.h"
#include "coordinator.h"
#include "esphome/core/log.h"
#include "esphome/core/helpers.h"

namespace esphome {
namespace philips_sonicare {

static const char *const TAG = "philips_sonicare.bridge";

static uint32_t parse_timeout_s(const std::string &s, uint32_t fallback,
                                const char *field) {
  if (s.empty())
    return fallback;
  char *endp = nullptr;
  unsigned long parsed = strtoul(s.c_str(), &endp, 10);
  if (endp == s.c_str() || *endp != '\0') {
    // File-scope function — falls back to the static TAG since there's no
    // SonicareBridge instance to read log_tag_ from here.
    ESP_LOGW(TAG, "Invalid %s '%s' — using %us",
             field, s.c_str(), (unsigned) fallback);
    return fallback;
  }
  return static_cast<uint32_t>(parsed);
}

std::string SonicareBridge::svc_name_(const std::string &action) {
  if (this->bridge_id_.empty())
    return action;
  return action + "_" + this->bridge_id_;
}

void SonicareBridge::setup() {
  this->register_service(&SonicareBridge::on_read_characteristic,
                          this->svc_name_("ble_read_char"),
                          {"service_uuid", "char_uuid"});
  this->register_service(&SonicareBridge::on_subscribe,
                          this->svc_name_("ble_subscribe"),
                          {"service_uuid", "char_uuid"});
  this->register_service(&SonicareBridge::on_unsubscribe,
                          this->svc_name_("ble_unsubscribe"),
                          {"service_uuid", "char_uuid"});
  this->register_service(&SonicareBridge::on_write_characteristic,
                          this->svc_name_("ble_write_char"),
                          {"service_uuid", "char_uuid", "data"});
  this->register_service(&SonicareBridge::on_set_throttle,
                          this->svc_name_("ble_set_throttle"),
                          {"throttle_ms"});
  this->register_service(&SonicareBridge::on_get_info,
                          this->svc_name_("ble_get_info"), {});
  this->register_service(&SonicareBridge::on_list_services,
                          this->svc_name_("ble_list_services"), {});
  this->register_service(&SonicareBridge::on_pair_mode,
                          this->svc_name_("ble_pair_mode"),
                          {"enabled", "timeout_s"});
  this->register_service(&SonicareBridge::on_unpair,
                          this->svc_name_("ble_unpair"), {});
  this->register_service(&SonicareBridge::on_scan,
                          this->svc_name_("ble_scan"),
                          {"timeout_s"});
  this->register_service(&SonicareBridge::on_pair_mac,
                          this->svc_name_("ble_pair_mac"),
                          {"mac", "timeout_s"});
  if (this->bridge_id_.empty())
    ESP_LOGI(this->log_tag_.c_str(), "Services registered");
  else
    ESP_LOGI(this->log_tag_.c_str(), "Services registered (suffix: '%s')", this->bridge_id_.c_str());
}

void SonicareBridge::loop() {
  if (this->coord_ == nullptr)
    return;

  // Drive the Coordinator's timer state machine from here too — the Worker's
  // own loop() can starve briefly during BLE teardown (close+disconnect+bond
  // remove sequence), and the unpair-drain timer must fire on time so the
  // `unpaired` status event reaches HA before its 4 s timeout. on_loop is
  // idempotent (each timer check toggles state once), so duplicate ticks
  // from Worker + Bridge are harmless.
  uint32_t now = millis();
  this->coord_->on_loop(now);

  if ((now - this->last_heartbeat_ms_) < HEARTBEAT_INTERVAL_MS)
    return;

  this->last_heartbeat_ms_ = now;
  char uptime_str[16];
  snprintf(uptime_str, sizeof(uptime_str), "%u", now / 1000);

  // Heartbeat is HA-driven (timer-based), not tied to a BLE state change,
  // so we fire the event directly instead of going through the Coordinator's
  // emit_status_ helper (which is for events triggered by BLE state changes).
  this->fire_event(EVENT_STATUS,
                    {
                        {"status", "heartbeat"},
                        {"ble_connected", this->coord_->is_connected() ? "true" : "false"},
                        {"mac", this->coord_->get_device_mac()},
                        {"version", PHILIPS_SONICARE_VERSION},
                        {"uptime_s", std::string(uptime_str)},
                    });

  // After OTA, the initial "ready" event can be lost (BLE connects before
  // the HA API stream is up). If we're connected with services discovered
  // but no active subscriptions, re-fire so HA can resubscribe.
  if (this->coord_->is_connected() &&
      this->coord_->are_services_discovered() &&
      this->coord_->subscription_count() == 0) {
    ESP_LOGI(this->log_tag_.c_str(), "BLE connected, no subscriptions — re-firing ready");
    this->fire_event(EVENT_STATUS,
                      {
                          {"status", "ready"},
                          {"mac", this->coord_->get_device_mac()},
                          {"version", PHILIPS_SONICARE_VERSION},
                          {"uptime_s", std::string(uptime_str)},
                      });
  }
}

void SonicareBridge::dump_config() {
  ESP_LOGCONFIG(this->log_tag_.c_str(), "Philips Sonicare Bridge v%s", PHILIPS_SONICARE_VERSION);
  if (!this->bridge_id_.empty())
    ESP_LOGCONFIG(this->log_tag_.c_str(), "  Bridge ID: %s", this->bridge_id_.c_str());
}

void SonicareBridge::fire_event(const std::string &event_type,
                                 const std::map<std::string, std::string> &data) {
  // Always tag with bridge_id so HA can filter events when multiple bridges
  // exist on the same HA instance. Empty bridge_id (single-bridge default)
  // still gets emitted as "" — listeners just don't filter on it.
  std::map<std::string, std::string> enriched = data;
  enriched["bridge_id"] = this->bridge_id_;
  this->fire_homeassistant_event(event_type, enriched);
}

void SonicareBridge::publish_connected(bool connected) {
  if (this->connected_sensor_ != nullptr)
    this->connected_sensor_->publish_state(connected);
}

// ── HA service shims — forward to coord_ ─────────────────────────────────────

void SonicareBridge::on_read_characteristic(std::string service_uuid,
                                              std::string char_uuid) {
  if (this->coord_)
    this->coord_->read_characteristic(service_uuid, char_uuid);
}

void SonicareBridge::on_subscribe(std::string service_uuid,
                                   std::string char_uuid) {
  if (this->coord_)
    this->coord_->subscribe(service_uuid, char_uuid);
}

void SonicareBridge::on_unsubscribe(std::string service_uuid,
                                     std::string char_uuid) {
  if (this->coord_)
    this->coord_->unsubscribe(service_uuid, char_uuid);
}

void SonicareBridge::on_write_characteristic(std::string service_uuid,
                                              std::string char_uuid,
                                              std::string hex_data) {
  if (this->coord_)
    this->coord_->write_characteristic(service_uuid, char_uuid, hex_data);
}

void SonicareBridge::on_set_throttle(std::string throttle_ms) {
  if (this->coord_ == nullptr)
    return;
  char *endp = nullptr;
  unsigned long ms = strtoul(throttle_ms.c_str(), &endp, 10);
  if (endp == throttle_ms.c_str() || *endp != '\0') {
    ESP_LOGW(this->log_tag_.c_str(), "Invalid throttle_ms value: '%s'", throttle_ms.c_str());
    return;
  }
  this->coord_->set_notify_throttle(static_cast<uint32_t>(ms));
  ESP_LOGI(this->log_tag_.c_str(), "Notification throttle set to %lu ms", ms);
}

void SonicareBridge::on_get_info() {
  if (this->coord_ == nullptr)
    return;

  auto info = this->coord_->collect_info_data();
  info["status"] = "info";
  info["version"] = PHILIPS_SONICARE_VERSION;
  info["bridge_id"] = this->bridge_id_;
  this->fire_event(EVENT_STATUS, info);
}

void SonicareBridge::on_list_services() {
  if (this->coord_)
    this->coord_->list_services();
}

void SonicareBridge::on_pair_mode(bool enabled, std::string timeout_s) {
  if (this->coord_ == nullptr)
    return;
  this->coord_->set_pair_mode(enabled, parse_timeout_s(timeout_s, 60, "timeout_s"));
}

void SonicareBridge::on_unpair() {
  if (this->coord_)
    this->coord_->unpair();
}

void SonicareBridge::on_scan(std::string timeout_s) {
  if (this->coord_ == nullptr)
    return;
  this->coord_->set_scan_mode(parse_timeout_s(timeout_s, 30, "timeout_s"));
}

void SonicareBridge::on_pair_mac(std::string mac, std::string timeout_s) {
  if (this->coord_ == nullptr)
    return;
  this->coord_->set_pair_mac(mac, parse_timeout_s(timeout_s, 60, "timeout_s"));
}

}  // namespace philips_sonicare
}  // namespace esphome
