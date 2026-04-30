#include "philips_sonicare.h"
#include "esphome/core/log.h"
#include "esphome/core/helpers.h"
#include "esphome/components/esp32_ble/ble.h"

namespace espbt = esphome::esp32_ble_tracker;

namespace esphome {
namespace philips_sonicare {

static const char *const TAG = "philips_sonicare";

// Service UUIDs for auto-discovery (no mac_address configured)
static const espbt::ESPBTUUID LEGACY_SERVICE_UUID =
    espbt::ESPBTUUID::from_raw("477ea600-a260-11e4-ae37-0002a5d50001");
static const espbt::ESPBTUUID CONDOR_SERVICE_UUID =
    espbt::ESPBTUUID::from_raw("e50ba3c0-af04-4564-92ad-fef019489de6");

// ── PhilipsSonicare (Mode A wrapper, BLEClientNode) ──────────────────────────
// Compiled only when the user has a ble_client: block in YAML (USE_BLE_CLIENT
// is defined by ESPHome's loader). Mode B users skip this entire class.

#ifdef USE_BLE_CLIENT

void PhilipsSonicare::setup() {
  if (this->coord_ == nullptr) {
    ESP_LOGE(TAG, "Coordinator not wired — Worker disabled");
    this->mark_failed();
    return;
  }
  this->coord_->set_parent(this->parent());
  this->coord_->set_set_enabled_cb([this](bool enabled) {
    if (this->parent()) this->parent()->set_enabled(enabled);
  });
  this->coord_->set_mode(MODE_EXTERNAL);
  // Mode A always has a fixed MAC from the ble_client: block.
  if (this->parent()) {
    auto *bda = this->parent()->get_remote_bda();
    char mac[18];
    snprintf(mac, sizeof(mac), "%02X:%02X:%02X:%02X:%02X:%02X",
             bda[0], bda[1], bda[2], bda[3], bda[4], bda[5]);
    this->coord_->set_identity_address(mac);
  }
}

void PhilipsSonicare::loop() {
  if (this->coord_)
    this->coord_->on_loop(millis());
}

void PhilipsSonicare::dump_config() {
  ESP_LOGCONFIG(TAG, "Philips Sonicare worker v%s", PHILIPS_SONICARE_VERSION);
}

#endif  // USE_BLE_CLIENT

// ── PhilipsSonicareStandalone (Mode B, extends BLEClientBase) ────────────────

void PhilipsSonicareStandalone::setup() {
  // Restore identity address (if any) before tracker logic kicks in
  this->pref_ = global_preferences->make_preference<uint64_t>(this->pref_ns_);
  if (this->address_ != 0) {
    ESP_LOGI(TAG, "Using configured MAC address — MAC mode");
    this->uuid_scan_mode_ = false;
  } else {
    uint64_t stored = 0;
    if (this->pref_.load(&stored) && stored != 0) {
      ESP_LOGI(TAG, "Loaded identity address from flash — MAC mode");
      this->set_address(stored);
      this->uuid_scan_mode_ = false;
    } else {
      ESP_LOGI(TAG, "No identity in flash — UUID scan mode (waiting for pair-mode)");
    }
  }

  // Wire ourselves into the coordinator as parent + set_enabled callback
  if (this->coord_ != nullptr) {
    this->coord_->set_parent(this);
    this->coord_->set_set_enabled_cb(
        [this](bool enabled) { this->set_enabled(enabled); });
    this->coord_->set_mode(MODE_STANDALONE);
    if (!this->uuid_scan_mode_ && this->address_ != 0) {
      // We have a known identity (YAML-configured or NVS-restored). Format
      // for the identity_address field so HA can detect "already bound".
      uint64_t a = this->address_;
      char mac[18];
      snprintf(mac, sizeof(mac), "%02X:%02X:%02X:%02X:%02X:%02X",
               (uint8_t)(a >> 40), (uint8_t)(a >> 32), (uint8_t)(a >> 24),
               (uint8_t)(a >> 16), (uint8_t)(a >> 8), (uint8_t)(a));
      this->coord_->set_identity_address(mac);
    }
    // Wipe NVS + reset to UUID-scan when HA requests unpair.
    this->coord_->set_unpair_cb([this]() {
      uint64_t prev = this->address_;
      uint64_t zero = 0;
      this->pref_.save(&zero);
      this->uuid_scan_mode_ = true;
      this->set_address(0);
      if (prev != 0) {
        ESP_LOGW(TAG,
                 "Identity cleared (was %02X:%02X:%02X:%02X:%02X:%02X) — back to UUID scan mode",
                 (uint8_t)(prev >> 40), (uint8_t)(prev >> 32),
                 (uint8_t)(prev >> 24), (uint8_t)(prev >> 16),
                 (uint8_t)(prev >> 8),  (uint8_t)(prev));
      } else {
        ESP_LOGW(TAG, "Identity cleared — back to UUID scan mode");
      }
    });
    // Open-GATT pair complete: Coordinator detected success without SMP.
    // Persist the currently-connected MAC as identity (mirrors the AUTH_CMPL
    // path in gap_event_handler for bonded brushes).
    this->coord_->set_save_identity_cb([this]() {
      auto *bda = this->get_remote_bda();
      uint64_t identity = esp32_ble::ble_addr_to_uint64(bda);
      ESP_LOGI(TAG, "Open-GATT pair complete — saving identity %02X:%02X:%02X:%02X:%02X:%02X",
               bda[0], bda[1], bda[2], bda[3], bda[4], bda[5]);
      this->pref_.save(&identity);
      this->set_address(identity);
      this->set_auto_connect(true);  // identity persisted → enable background reconnect
      this->uuid_scan_mode_ = false;
    });
  }

  BLEClientBase::setup();
  // Stay disabled in pure UUID-scan mode (no YAML MAC, no NVS identity) until
  // HA arms pair-mode. With a known identity, behave like before.
  this->enabled_ = !this->uuid_scan_mode_;
  // BLEClientBase::parse_device returns early when auto_connect_ is false,
  // so a bridge with a known target (YAML MAC or NVS-restored identity)
  // would never reconnect on adverts otherwise. Force it on whenever we
  // have an identity. UUID-scan-only mode keeps it off so we don't
  // direct-connect to whatever happens to show up during boot.
  if (!this->uuid_scan_mode_)
    this->set_auto_connect(true);
}

void PhilipsSonicareStandalone::loop() {
  // Coordinator's on_loop drives pair-mode timeout + auth-backoff timers, so
  // it must run regardless of enabled_. The BLEClientBase loop is only
  // skipped while we're explicitly disabled (auth backoff) and not idle.
  if (this->enabled_ || this->state() == espbt::ClientState::IDLE)
    BLEClientBase::loop();
  if (this->coord_)
    this->coord_->on_loop(millis());
}

void PhilipsSonicareStandalone::set_enabled(bool enabled) {
  if (enabled == this->enabled_)
    return;
  if (!enabled && this->state() != espbt::ClientState::IDLE) {
    ESP_LOGI(TAG, "Disabling BLE client.");
    auto err = esp_ble_gattc_close(this->gattc_if_, this->conn_id_);
    if (err != ESP_OK) {
      ESP_LOGW(TAG, "esp_ble_gattc_close error, status=%d", err);
    }
  }
  this->enabled_ = enabled;
}

bool PhilipsSonicareStandalone::parse_device(const espbt::ESPBTDevice &device) {
  if (!this->enabled_)
    return false;

  if (!this->uuid_scan_mode_)
    return BLEClientBase::parse_device(device);

  // No coordinator → cannot decide; stay passive.
  if (this->coord_ == nullptr)
    return false;

  // Match against Sonicare service UUIDs. Returns "" (no match), "legacy" or
  // "condor" so callers can label scan_result events.
  std::string matched_service;
  for (const auto &uuid : device.get_service_uuids()) {
    if (uuid == LEGACY_SERVICE_UUID) { matched_service = "legacy"; break; }
    if (uuid == CONDOR_SERVICE_UUID) { matched_service = "condor"; break; }
  }

  // Scan-only: emit one event per unique MAC, never connect.
  if (this->coord_->is_scan_mode_active()) {
    if (!matched_service.empty()) {
      const char *addr_type =
          device.get_address_type() == BLE_ADDR_TYPE_PUBLIC ? "public" : "random";
      std::string mfr_hex;
      const auto &mfr_datas = device.get_manufacturer_datas();
      if (!mfr_datas.empty()) {
        const auto &m = mfr_datas[0];
        if (m.uuid.get_uuid().len == ESP_UUID_LEN_16) {
          uint16_t cid = m.uuid.get_uuid().uuid.uuid16;
          char buf[5];
          // Company ID is little-endian on wire — preserve that here.
          snprintf(buf, sizeof(buf), "%02X%02X",
                   (uint8_t)(cid & 0xFF), (uint8_t)((cid >> 8) & 0xFF));
          mfr_hex = buf;
        }
        if (!m.data.empty())
          mfr_hex += format_hex(m.data.data(), m.data.size());
      }
      this->coord_->emit_scan_result(device.address_str(), addr_type,
                                       device.get_name(), mfr_hex,
                                       device.get_rssi(), matched_service);
    }
    return false;
  }

  // Pair-mode: connect to first match (or to target_mac_ if set).
  if (!this->coord_->is_pair_mode_active())
    return false;
  if (this->state() != espbt::ClientState::IDLE)
    return false;

  const std::string &target = this->coord_->get_target_mac();
  if (!target.empty()) {
    // Targeted: ble_pair_mac flow. Match exactly this MAC, no UUID filter
    // (the brush may not advertise its service UUID in some adverts).
    if (device.address_str() != target)
      return false;
    ESP_LOGI(TAG, "Pair-mode targeted match: %s", target.c_str());
  } else {
    if (matched_service.empty())
      return false;
    ESP_LOGI(TAG, "Found Sonicare via UUID at %s (pair-mode, %s)",
             device.address_str().c_str(), matched_service.c_str());
  }

  this->set_address(device.address_uint64());
  this->remote_addr_type_ = device.get_address_type();
  this->set_state(espbt::ClientState::DISCOVERED);
  return true;
}

bool PhilipsSonicareStandalone::gattc_event_handler(
    esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
    esp_ble_gattc_cb_param_t *param) {
  // BLEClientBase returns true only when this event is for our own GATT
  // connection (matched conn_id / gattc_if). The ESP-BLE stack dispatches
  // every event to every registered client, so on multi-bridge boards this
  // gate is what keeps us from reacting to other bridges' events.
  bool result = BLEClientBase::gattc_event_handler(event, gattc_if, param);
  if (this->coord_ && result)
    this->coord_->on_gattc_event(event, gattc_if, param);
  return result;
}

void PhilipsSonicareStandalone::gap_event_handler(esp_gap_ble_cb_event_t event,
                                                    esp_ble_gap_cb_param_t *param) {
  BLEClientBase::gap_event_handler(event, param);

  // Identity address persistence: after first successful bonding while in
  // UUID-scan mode, save the (now stable) identity address to flash so future
  // boots can target it directly. GAP events are global (every registered
  // client sees them), so on multi-bridge boards we must filter to events
  // for *our* connection — otherwise a parallel bond on bridge A would
  // overwrite bridge B's NVS with bridge A's identity.
  if (event == ESP_GAP_BLE_AUTH_CMPL_EVT
      && param->ble_security.auth_cmpl.success
      && this->uuid_scan_mode_
      && memcmp(this->remote_bda_,
                param->ble_security.auth_cmpl.bd_addr, 6) == 0) {
    uint64_t identity = esp32_ble::ble_addr_to_uint64(
        param->ble_security.auth_cmpl.bd_addr);
    const auto *bda = param->ble_security.auth_cmpl.bd_addr;
    ESP_LOGI(TAG,
             "Bonded — saving identity %02X:%02X:%02X:%02X:%02X:%02X, "
             "switching to MAC mode",
             bda[0], bda[1], bda[2], bda[3], bda[4], bda[5]);
    this->pref_.save(&identity);
    this->set_address(identity);
    this->set_auto_connect(true);  // identity persisted → enable background reconnect
    this->remote_addr_type_ = param->ble_security.auth_cmpl.addr_type;
    this->uuid_scan_mode_ = false;
  }

  if (this->coord_)
    this->coord_->on_gap_event(event, param);
}

}  // namespace philips_sonicare
}  // namespace esphome
