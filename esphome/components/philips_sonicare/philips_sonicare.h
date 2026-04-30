#pragma once

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/core/defines.h"
#include "esphome/core/preferences.h"
#include "esphome/components/esp32_ble_client/ble_client_base.h"
#include "esphome/components/esp32_ble_tracker/esp32_ble_tracker.h"

#ifdef USE_BLE_CLIENT
#include "esphome/components/ble_client/ble_client.h"
#endif

#include "coordinator.h"

namespace esphome {
namespace philips_sonicare {

#ifdef USE_BLE_CLIENT
// Mode A wrapper: thin BLEClientNode adapter that forwards GATT/GAP events
// to the SonicareCoordinator. Used when an external `ble_client:` block is
// referenced via `ble_client_id`. Only compiled when the user includes the
// ble_client component in their YAML.
class PhilipsSonicare : public ble_client::BLEClientNode, public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override {
    return setup_priority::AFTER_BLUETOOTH;
  }

  void gattc_event_handler(esp_gattc_cb_event_t event,
                            esp_gatt_if_t gattc_if,
                            esp_ble_gattc_cb_param_t *param) override {
    if (this->coord_) this->coord_->on_gattc_event(event, gattc_if, param);
  }
  void gap_event_handler(esp_gap_ble_cb_event_t event,
                          esp_ble_gap_cb_param_t *param) override {
    if (this->coord_) this->coord_->on_gap_event(event, param);
  }

  void set_coordinator(SonicareCoordinator *coord) { this->coord_ = coord; }

 protected:
  SonicareCoordinator *coord_{nullptr};
};
#endif  // USE_BLE_CLIENT

// Mode B standalone client: extends BLEClientBase directly so we don't depend
// on the `ble_client` component (no dummy `ble_client:` YAML block needed).
// Combines what was PhilipsSonicareBLEClient (BLE infrastructure + UUID scan +
// NVS persistence) and PhilipsSonicare (event forwarding to Coordinator)
// into one class.
class PhilipsSonicareStandalone : public esp32_ble_client::BLEClientBase {
 public:
  void setup() override;
  void loop() override;
  bool parse_device(const esp32_ble_tracker::ESPBTDevice &device) override;
  bool gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                            esp_ble_gattc_cb_param_t *param) override;
  void gap_event_handler(esp_gap_ble_cb_event_t event,
                          esp_ble_gap_cb_param_t *param) override;

  void set_coordinator(SonicareCoordinator *coord) { this->coord_ = coord; }
  void set_pref_namespace(uint32_t ns) { this->pref_ns_ = ns; }
  void set_enabled(bool enabled);

 protected:
  SonicareCoordinator *coord_{nullptr};
  bool uuid_scan_mode_{true};
  bool enabled_{true};
  uint32_t pref_ns_{0};
  ESPPreferenceObject pref_;
};

// Triggers usable in both modes — fire on the SonicareCoordinator's
// ready / disconnect callbacks, so the user's YAML on_connect / on_disconnect
// works regardless of whether Mode A or Mode B is active.
//
// Both triggers expose two strings to the user's automation — useful when
// several Sonicares share one automation file:
//   - `mac` — the brush's BLE address (identity post-bond)
//   - `bridge_id` — the YAML `bridge_id` of the slot that fired (empty
//                   string in single-bridge setups)
class SonicareConnectTrigger : public Trigger<std::string, std::string> {
 public:
  explicit SonicareConnectTrigger(SonicareCoordinator *coord) {
    coord->set_on_ready_cb(
        [this](const std::string &mac, const std::string &bridge_id) {
          this->trigger(mac, bridge_id);
        });
  }
};

class SonicareDisconnectTrigger : public Trigger<std::string, std::string> {
 public:
  explicit SonicareDisconnectTrigger(SonicareCoordinator *coord) {
    coord->set_on_disconnect_cb(
        [this](const std::string &mac, const std::string &bridge_id) {
          this->trigger(mac, bridge_id);
        });
  }
};

}  // namespace philips_sonicare
}  // namespace esphome
