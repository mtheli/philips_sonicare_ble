#include "coordinator.h"
#include "bridge.h"
#include "esphome/core/log.h"
#include "esphome/core/helpers.h"
#include "esp_system.h"

namespace espbt = esphome::esp32_ble_tracker;

namespace esphome {
namespace philips_sonicare {

static const char *const TAG = "philips_sonicare";

// ── helpers ──────────────────────────────────────────────────────────────────

static espbt::ESPBTUUID parse_uuid(const std::string &uuid_str) {
  // Short form: 4 hex chars → 16-bit UUID, 8 hex chars → 32-bit UUID.
  // Longer strings (e.g. "477ea600-a260-11e4-ae37-0002a5d50001") go to from_raw.
  if (uuid_str.length() == 4 || uuid_str.length() == 8) {
    char *endp = nullptr;
    unsigned long val = strtoul(uuid_str.c_str(), &endp, 16);
    if (endp == uuid_str.c_str() || *endp != '\0') {
      ESP_LOGW(TAG, "Invalid hex UUID '%s' — falling back to raw", uuid_str.c_str());
      return espbt::ESPBTUUID::from_raw(uuid_str);
    }
    return uuid_str.length() == 4
        ? espbt::ESPBTUUID::from_uint16(static_cast<uint16_t>(val))
        : espbt::ESPBTUUID::from_uint32(static_cast<uint32_t>(val));
  }
  return espbt::ESPBTUUID::from_raw(uuid_str);
}

void SonicareCoordinator::emit_(const std::string &event_type,
                                  const std::map<std::string, std::string> &data) {
  if (this->bridge_ != nullptr)
    this->bridge_->fire_event(event_type, data);
}

void SonicareCoordinator::emit_status_(const std::string &status,
                                        std::map<std::string, std::string> extra) {
  extra["status"] = status;
  extra["mac"] = this->get_device_mac();
  this->emit_(EVENT_STATUS, extra);
}

void SonicareCoordinator::emit_data_(const std::string &uuid,
                                      const std::string &payload,
                                      const std::string &error) {
  std::map<std::string, std::string> data = {
      {"uuid", uuid},
      {"payload", payload},
      {"mac", this->get_device_mac()},
  };
  if (!error.empty())
    data["error"] = error;
  this->emit_(EVENT_DATA, data);
}

void SonicareCoordinator::apply_smp_params_() {
  // LE Secure Connections pairing parameters.
  // Models that don't need bonding (e.g. DiamondClean) will simply
  // skip the pairing handshake.  Models that require bonding (e.g.
  // ExpertClean, HX991M) negotiate the best supported level.
  uint8_t auth_req = 0x2D;  // Bond(1) | MITM(4) | SC(8) | CT2(0x20)
  esp_ble_io_cap_t io_cap = ESP_IO_CAP_IO;  // DisplayYesNo
  uint8_t key_size = 16;
  uint8_t init_key = ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK;
  uint8_t rsp_key = ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK;

  esp_ble_gap_set_security_param(ESP_BLE_SM_AUTHEN_REQ_MODE,
                                  &auth_req, sizeof(auth_req));
  esp_ble_gap_set_security_param(ESP_BLE_SM_IOCAP_MODE,
                                  &io_cap, sizeof(io_cap));
  esp_ble_gap_set_security_param(ESP_BLE_SM_MAX_KEY_SIZE,
                                  &key_size, sizeof(key_size));
  esp_ble_gap_set_security_param(ESP_BLE_SM_SET_INIT_KEY,
                                  &init_key, sizeof(init_key));
  esp_ble_gap_set_security_param(ESP_BLE_SM_SET_RSP_KEY,
                                  &rsp_key, sizeof(rsp_key));
  ESP_LOGD(TAG, "SMP parameters applied (auth=0x%02X, io_cap=%d)", auth_req, io_cap);
}

void SonicareCoordinator::on_loop(uint32_t now_ms) {
  // Auth failure backoff recovery
  if (this->backoff_until_ms_ != 0 && now_ms >= this->backoff_until_ms_) {
    ESP_LOGI(TAG, "Auth backoff expired — re-enabling BLE");
    this->backoff_until_ms_ = 0;
    this->auth_fail_count_ = 0;
    if (this->set_enabled_cb_)
      this->set_enabled_cb_(true);
  }
  // Pair-mode timeout
  if (this->pair_mode_active_ && now_ms >= this->pair_mode_until_ms_) {
    bool had_auth = this->auth_completed_;
    this->pair_mode_active_ = false;
    this->pair_mode_until_ms_ = 0;
    this->target_mac_.clear();
    if (this->set_enabled_cb_)
      this->set_enabled_cb_(false);
    // Defensive fallback: if SMP succeeded but the probe-read never came back
    // (brush walked away mid-flow), the bond is already in NVS via the Worker.
    // Emit pair_complete anyway so HA can finish the config flow — marked
    // post_auth_no_probe so HA knows model/ble_name aren't filled and may
    // need a follow-up ble_get_info.
    if (had_auth && !this->identity_address_.empty()) {
      ESP_LOGW(TAG, "Pair-mode timed out after AUTH_CMPL — emitting pair_complete anyway");
      this->emit_status_("pair_complete", {
          {"identity_address", this->identity_address_},
          {"bonding", "bonded"},
          {"note", "post_auth_no_probe"},
      });
    } else {
      ESP_LOGW(TAG, "Pair-mode timed out without successful pairing");
      this->emit_status_("pair_timeout");
    }
  }
  // Scan-mode timeout: report aggregate count and disable.
  if (this->scan_mode_active_ && now_ms >= this->scan_mode_until_ms_) {
    ESP_LOGI(TAG, "Scan-mode finished (%u unique MACs)",
             (unsigned) this->scan_seen_macs_.size());
    char count_str[8];
    snprintf(count_str, sizeof(count_str), "%u",
             (unsigned) this->scan_seen_macs_.size());
    this->scan_mode_active_ = false;
    this->scan_mode_until_ms_ = 0;
    this->scan_seen_macs_.clear();
    if (this->set_enabled_cb_)
      this->set_enabled_cb_(false);
    this->emit_status_("scan_complete", {{"count", count_str}});
  }
}

void SonicareCoordinator::set_pair_mode(bool enable, uint32_t timeout_s) {
  if (this->mode_ != MODE_STANDALONE) {
    ESP_LOGW(TAG, "ble_pair_mode requested on non-standalone bridge — ignored "
                  "(Mode A pairs via the configured ble_client MAC)");
    return;
  }
  if (enable) {
    if (timeout_s == 0)
      timeout_s = 60;
    if (timeout_s > 600)
      timeout_s = 600;  // hard cap — pair-mode shouldn't linger
    this->pair_mode_active_ = true;
    this->pair_mode_until_ms_ = millis() + timeout_s * 1000;
    ESP_LOGI(TAG, "Pair-mode enabled for %us", (unsigned) timeout_s);
    if (this->set_enabled_cb_)
      this->set_enabled_cb_(true);
    char timeout_str[8];
    snprintf(timeout_str, sizeof(timeout_str), "%u", (unsigned) timeout_s);
    this->emit_status_("pair_mode_started", {{"timeout_s", timeout_str}});
  } else {
    if (!this->pair_mode_active_)
      return;
    ESP_LOGI(TAG, "Pair-mode disabled by request");
    this->pair_mode_active_ = false;
    this->pair_mode_until_ms_ = 0;
    this->target_mac_.clear();
    if (this->set_enabled_cb_)
      this->set_enabled_cb_(false);
    this->emit_status_("pair_mode_stopped");
  }
}

void SonicareCoordinator::set_scan_mode(uint32_t timeout_s) {
  if (this->mode_ != MODE_STANDALONE) {
    ESP_LOGW(TAG, "ble_scan requested on non-standalone bridge — ignored");
    return;
  }
  if (this->pair_mode_active_) {
    ESP_LOGW(TAG, "ble_scan requested while pair-mode active — ignored");
    return;
  }
  if (timeout_s == 0)
    timeout_s = 30;
  if (timeout_s > 300)
    timeout_s = 300;
  this->scan_mode_active_ = true;
  this->scan_mode_until_ms_ = millis() + timeout_s * 1000;
  this->scan_seen_macs_.clear();
  ESP_LOGI(TAG, "Scan-mode enabled for %us (discovery-only, no connect)",
           (unsigned) timeout_s);
  if (this->set_enabled_cb_)
    this->set_enabled_cb_(true);
  char timeout_str[8];
  snprintf(timeout_str, sizeof(timeout_str), "%u", (unsigned) timeout_s);
  this->emit_status_("scan_started", {{"timeout_s", timeout_str}});
}

void SonicareCoordinator::emit_scan_result(const std::string &mac,
                                            const std::string &addr_type,
                                            const std::string &local_name,
                                            const std::string &mfr_data,
                                            int rssi,
                                            const std::string &service_uuid) {
  if (!this->scan_mode_active_)
    return;
  // Dedup: only the first sighting of each MAC fires an event.
  if (!this->scan_seen_macs_.insert(mac).second)
    return;
  char rssi_str[8];
  snprintf(rssi_str, sizeof(rssi_str), "%d", rssi);
  this->emit_status_("scan_result", {
      {"result_mac", mac},
      {"addr_type", addr_type},
      {"local_name", local_name},
      {"mfr_data", mfr_data},
      {"rssi", std::string(rssi_str)},
      {"service_uuid", service_uuid},
  });
}

void SonicareCoordinator::set_pair_mac(const std::string &mac,
                                        uint32_t timeout_s) {
  if (this->mode_ != MODE_STANDALONE) {
    ESP_LOGW(TAG, "ble_pair_mac requested on non-standalone bridge — ignored");
    return;
  }
  // Normalize MAC: strip dashes/colons, uppercase, then re-insert colons.
  std::string normalized;
  normalized.reserve(17);
  for (char c : mac) {
    if (c == ':' || c == '-' || c == ' ')
      continue;
    if (c >= 'a' && c <= 'f')
      c = c - 'a' + 'A';
    normalized.push_back(c);
  }
  if (normalized.length() != 12) {
    ESP_LOGW(TAG, "Invalid MAC '%s' (need 12 hex chars after stripping) — ignored",
             mac.c_str());
    this->emit_status_("pair_timeout", {{"error", "invalid_mac"}});
    return;
  }
  std::string colon_mac;
  colon_mac.reserve(17);
  for (size_t i = 0; i < 12; i += 2) {
    if (i > 0)
      colon_mac.push_back(':');
    colon_mac.push_back(normalized[i]);
    colon_mac.push_back(normalized[i + 1]);
  }
  this->target_mac_ = colon_mac;
  ESP_LOGI(TAG, "Pair-mode (targeted) armed for %s", colon_mac.c_str());
  // Reuse pair-mode plumbing: this turns on enabled_, starts the timer, fires
  // pair_mode_started — but parse_device on the Worker will only match this
  // exact MAC because target_mac_ is set.
  this->set_pair_mode(true, timeout_s);
}

void SonicareCoordinator::unpair() {
  std::string previous_mac = this->identity_address_;
  ESP_LOGW(TAG, "Unpair requested — clearing bond and identity (was: %s)",
           previous_mac.empty() ? "<none>" : previous_mac.c_str());
  if (this->parent_ != nullptr) {
    auto *bda = this->parent_->get_remote_bda();
    esp_ble_remove_bond_device(bda);
  }
  if (this->unpair_cb_)
    this->unpair_cb_();  // Worker wipes NVS + resets uuid_scan_mode_
  this->identity_address_.clear();
  // Synchronously clear cached connection state so a probe between
  // unpair and the actual GAP_DISCONNECT_EVT doesn't report a half-
  // alive bridge (stale connected_ + model + name with mac=00:00).
  this->connected_ = false;
  this->services_discovered_ = false;
  this->model_number_.clear();
  this->remote_name_.clear();
  // Subscriptions are tied to the previous brush's char handles (HA
  // registered them against an entry that's now gone). After a brush
  // swap they'd resubscribe against stale UUIDs and emit a flurry of
  // "characteristic not found" warnings — clear them here so the new
  // brush starts clean.
  this->desired_subscriptions_.clear();
  // Force a disconnect so the next reconnect goes through the (now empty)
  // identity → UUID-scan path.
  if (this->set_enabled_cb_) {
    this->set_enabled_cb_(false);
    this->set_enabled_cb_(true);
  }
  this->emit_status_("unpaired", {{"previous_mac", previous_mac}});
}

std::string SonicareCoordinator::get_device_mac() {
  if (this->parent_ == nullptr)
    return "00:00:00:00:00:00";
  char mac[18];
  auto *bda = this->parent_->get_remote_bda();
  snprintf(mac, sizeof(mac), "%02X:%02X:%02X:%02X:%02X:%02X",
           bda[0], bda[1], bda[2], bda[3], bda[4], bda[5]);
  return std::string(mac);
}

void SonicareCoordinator::on_gattc_event(esp_gattc_cb_event_t event,
                                           esp_gatt_if_t gattc_if,
                                           esp_ble_gattc_cb_param_t *param) {
  // parent_ is guaranteed non-null by the ESPHome setup ordering, but events
  // can in theory be queued during early init — bail out defensively.
  if (this->parent_ == nullptr)
    return;
  switch (event) {
    case ESP_GATTC_OPEN_EVT: {
      if (param->open.status == ESP_GATT_OK) {
        this->auth_completed_ = false;
        this->connect_time_ms_ = millis();
        const std::string &bid = this->bridge_ ? this->bridge_->get_bridge_id()
                                                : std::string();
        ESP_LOGI(TAG, "Connected to Sonicare (%s%s%s)",
                 this->get_device_mac().c_str(),
                 bid.empty() ? "" : ", bridge=",
                 bid.empty() ? "" : bid.c_str());
        this->connected_ = true;
        if (this->bridge_)
          this->bridge_->publish_connected(true);
        this->emit_status_("connected");
      } else {
        ESP_LOGW(TAG, "Connection failed, status=%d", param->open.status);
      }
      break;
    }

    case ESP_GATTC_DISCONNECT_EVT: {
      // Stale bond detection: if auth never completed and disconnect
      // was rapid, the bond keys may be out of sync.
      if (this->auth_completed_ ||
          this->connect_time_ms_ == 0 ||
          (millis() - this->connect_time_ms_) > RAPID_DISCONNECT_THRESHOLD_MS) {
        this->rapid_disconnect_count_ = 0;
      } else {
        this->rapid_disconnect_count_++;
        ESP_LOGW(TAG, "Rapid disconnect without auth (%d/%d)",
                 this->rapid_disconnect_count_, MAX_RAPID_DISCONNECTS);
        if (this->rapid_disconnect_count_ >= MAX_RAPID_DISCONNECTS) {
          ESP_LOGW(TAG, "Removing stale bond for %s", this->get_device_mac().c_str());
          esp_ble_remove_bond_device(this->parent_->get_remote_bda());
          this->rapid_disconnect_count_ = 0;
        }
      }
      ESP_LOGW(TAG, "Disconnected from Sonicare (reason=0x%02X). "
               "%d subscription(s) will be restored on reconnect.",
               param->disconnect.reason,
               this->desired_subscriptions_.size());
      this->connected_ = false;
      this->services_discovered_ = false;
      if (this->bridge_)
        this->bridge_->publish_connected(false);
      this->pending_handle_ = 0;
      this->pending_char_uuid_.clear();
      this->pending_service_uuid_.clear();
      this->retry_read_after_auth_ = false;
      this->encryption_requested_ = false;
      this->name_handle_ = 0;
      this->notify_map_.clear();
      this->cccd_map_.clear();
      this->char_props_map_.clear();
      this->last_notify_ms_.clear();
      this->pending_calls_.clear();
      char reason_str[5];
      snprintf(reason_str, sizeof(reason_str), "0x%02X", param->disconnect.reason);
      this->emit_status_("disconnected", {{"reason", reason_str}});
      if (this->on_disconnect_cb_) {
        const std::string &bid = this->bridge_ ? this->bridge_->get_bridge_id()
                                                : std::string();
        this->on_disconnect_cb_(this->get_device_mac(), bid);
      }
      break;
    }

    case ESP_GATTC_SEARCH_CMPL_EVT: {
      ESP_LOGI(TAG, "Service discovery complete");
      this->services_discovered_ = true;
      this->encryption_requested_ = false;
      if (!this->desired_subscriptions_.empty()) {
        ESP_LOGI(TAG, "Restoring %d notification subscription(s)...",
                 this->desired_subscriptions_.size());
        this->resubscribe_all_();
      }
      // Read GAP Device Name (0x2A00) for display in HA config flow
      if (this->remote_name_.empty()) {
        auto gap_svc = espbt::ESPBTUUID::from_uint16(0x1800);
        auto name_chr = espbt::ESPBTUUID::from_uint16(0x2A00);
        auto *chr = this->parent_->get_characteristic(gap_svc, name_chr);
        if (chr) {
          this->name_handle_ = chr->handle;
          auto status = esp_ble_gattc_read_char(
              this->parent_->get_gattc_if(),
              this->parent_->get_conn_id(),
              chr->handle, ESP_GATT_AUTH_REQ_NONE);
          if (status != ESP_GATT_OK) {
            ESP_LOGD(TAG, "Failed to initiate device name read: %d", status);
            this->name_handle_ = 0;
          }
        }
      }
      // Read Model Number (Device Info 0x180A → 0x2A24) — used in
      // SonicareBridge selection UI in the HA config flow so the user can
      // tell which physical brush is on which bridge.
      if (this->model_number_.empty()) {
        auto info_svc = espbt::ESPBTUUID::from_uint16(0x180A);
        auto model_chr = espbt::ESPBTUUID::from_uint16(0x2A24);
        auto *chr = this->parent_->get_characteristic(info_svc, model_chr);
        if (chr) {
          this->model_handle_ = chr->handle;
          auto status = esp_ble_gattc_read_char(
              this->parent_->get_gattc_if(),
              this->parent_->get_conn_id(),
              chr->handle, ESP_GATT_AUTH_REQ_NONE);
          if (status != ESP_GATT_OK) {
            ESP_LOGD(TAG, "Failed to initiate model number read: %d", status);
            this->model_handle_ = 0;
          }
        }
      }
      // Pairing probe.
      //
      // Classic (Sonicare service 477ea600-…): read Handle State (0x4010).
      // The read either succeeds (open-GATT brushes like HX6340 Kids) or
      // returns INSUF_AUTHENTICATION (bonded brushes like HX9992) which
      // we use as the SMP trigger.
      //
      // Condor (newer protocol, e50ba3c0-…): no equivalent read-probe
      // char exists on V4 (HX742X) — Protocol Config (e50b0005) is
      // absent there, and the other Condor chars are notify-/write-only.
      // All known Condor brushes require bonding (HX742X confirmed by
      // multiple users), so we trigger SMP directly via
      // esp_ble_set_encryption() once we see the Condor service in the
      // discovered tree. If a future Condor brush turns out to be open-
      // GATT, the SMP request will fail and we'll need a per-model
      // fallback.
      {
        auto sonicare_svc = espbt::ESPBTUUID::from_raw(
            "477ea600-a260-11e4-ae37-0002a5d50001");
        auto handle_state = espbt::ESPBTUUID::from_raw(
            "477ea600-a260-11e4-ae37-0002a5d54010");
        auto *chr = this->parent_->get_characteristic(sonicare_svc, handle_state);
        if (chr) {
          this->probe_handle_ = chr->handle;
          esp_ble_gattc_read_char(
              this->parent_->get_gattc_if(),
              this->parent_->get_conn_id(),
              chr->handle, ESP_GATT_AUTH_REQ_NONE);
        } else {
          auto condor_svc = espbt::ESPBTUUID::from_raw(
              "e50ba3c0-af04-4564-92ad-fef019489de6");
          if (this->parent_->get_service(condor_svc) != nullptr &&
              this->pair_mode_active_ && !this->auth_completed_) {
            ESP_LOGI(TAG,
                     "Condor service detected — initiating SMP encryption");
            this->encryption_requested_ = true;
            this->apply_smp_params_();
            esp_ble_set_encryption(this->parent_->get_remote_bda(),
                                    ESP_BLE_SEC_ENCRYPT);
          }
        }
      }
      this->emit_status_("ready", {{"version", PHILIPS_SONICARE_VERSION}});
      if (this->on_ready_cb_) {
        const std::string &bid = this->bridge_ ? this->bridge_->get_bridge_id()
                                                : std::string();
        this->on_ready_cb_(this->get_device_mac(), bid);
      }

      // Drain any HA service calls that arrived before service discovery
      // completed (HA's coordinator fires read/subscribe/unsubscribe on
      // 'connected', not waiting for 'ready'). resubscribe_all_ already
      // restored prior subscriptions; HA's intent supersedes via the queue.
      if (!this->pending_calls_.empty()) {
        ESP_LOGI(TAG, "Draining %u queued call(s) deferred until discovery",
                 (unsigned) this->pending_calls_.size());
        auto pending = std::move(this->pending_calls_);
        this->pending_calls_.clear();
        for (auto &fn : pending)
          fn();
      }
      break;
    }

    case ESP_GATTC_READ_CHAR_EVT: {
      // Handle device name read (GAP 0x2A00) — just for display
      if (this->name_handle_ != 0 && param->read.handle == this->name_handle_) {
        if (param->read.status == ESP_GATT_OK && param->read.value_len > 0) {
          this->remote_name_ = std::string(
              reinterpret_cast<const char *>(param->read.value),
              param->read.value_len);
          ESP_LOGI(TAG, "Device name: %s", this->remote_name_.c_str());
        } else {
          ESP_LOGD(TAG, "Device name read failed, status=%d", param->read.status);
        }
        this->name_handle_ = 0;
        break;
      }

      // Handle model number read (Device Info 0x2A24)
      if (this->model_handle_ != 0 && param->read.handle == this->model_handle_) {
        if (param->read.status == ESP_GATT_OK && param->read.value_len > 0) {
          std::string raw(reinterpret_cast<const char *>(param->read.value),
                           param->read.value_len);
          // Trim trailing spaces / nulls — Philips pads to fixed width
          while (!raw.empty() && (raw.back() == ' ' || raw.back() == '\0'))
            raw.pop_back();
          this->model_number_ = raw;
          ESP_LOGI(TAG, "Model number: %s", this->model_number_.c_str());
        } else {
          ESP_LOGD(TAG, "Model number read failed, status=%d", param->read.status);
        }
        this->model_handle_ = 0;
        break;
      }

      // Pairing probe: Handle State (0x4010) from Sonicare service
      if (this->probe_handle_ != 0 && param->read.handle == this->probe_handle_) {
        if (param->read.status == ESP_GATT_OK) {
          const char *state_names[] = {"Off", "Standby", "Running", "Charging", "Shutdown"};
          uint8_t state = (param->read.value_len > 0) ? param->read.value[0] : 0xFF;
          const char *state_name = (state <= 4) ? state_names[state] : "Unknown";
          ESP_LOGI(TAG, "Pairing probe: open GATT (no pairing required) — handle state: %s (%d)",
                   state_name, state);
          // Probe success is the unified pair_complete trigger. By the time
          // the probe response arrives, GAP-Name and DeviceInfo-Model reads
          // have already returned (they're issued earlier in the SEARCH_CMPL
          // handler and responses are sequential), so model + ble_name are
          // populated. auth_completed_ tells us whether the bond went through
          // SMP (bonded brushes) or skipped it (open-GATT brushes).
          if (this->pair_mode_active_) {
            char identity_str[18];
            auto *bda = this->parent_->get_remote_bda();
            snprintf(identity_str, sizeof(identity_str),
                     "%02X:%02X:%02X:%02X:%02X:%02X",
                     bda[0], bda[1], bda[2], bda[3], bda[4], bda[5]);
            this->identity_address_ = identity_str;
            this->pair_mode_active_ = false;
            this->pair_mode_until_ms_ = 0;
            this->target_mac_.clear();
            bool is_bonded = this->auth_completed_;
            // Bonded brushes: Worker already saved NVS in gap_event_handler
            // when AUTH_CMPL.success arrived. Open-GATT: there's no AUTH_CMPL,
            // call the save-identity callback explicitly.
            if (!is_bonded && this->save_identity_cb_)
              this->save_identity_cb_();
            std::map<std::string, std::string> extra = {
                {"identity_address", this->identity_address_},
                {"bonding", is_bonded ? "bonded" : "open_gatt"},
            };
            if (!this->model_number_.empty())
              extra["model"] = this->model_number_;
            if (!this->remote_name_.empty())
              extra["ble_name"] = this->remote_name_;
            this->emit_status_("pair_complete", extra);
          }
        } else if (param->read.status == ESP_GATT_INSUF_AUTHENTICATION ||
                   param->read.status == ESP_GATT_INSUF_ENCRYPTION) {
          ESP_LOGW(TAG, "Pairing probe: device requires BLE bonding (status=%d) — initiating pairing",
                   param->read.status);
          this->encryption_requested_ = true;
          this->apply_smp_params_();
          esp_ble_set_encryption(this->parent_->get_remote_bda(),
                                  ESP_BLE_SEC_ENCRYPT_MITM);
        } else {
          ESP_LOGD(TAG, "Pairing probe failed, status=%d", param->read.status);
        }
        this->probe_handle_ = 0;
        break;
      }

      if (param->read.status != ESP_GATT_OK) {
        // Insufficient Authentication / Encryption → initiate pairing
        // and retry the read after successful auth (don't report error yet)
        if ((param->read.status == ESP_GATT_INSUF_AUTHENTICATION ||
             param->read.status == ESP_GATT_INSUF_ENCRYPTION) &&
            !this->encryption_requested_) {
          ESP_LOGI(TAG, "Read requires authentication (status=%d) — initiating encryption, will retry on AUTH_CMPL",
                   param->read.status);
          this->encryption_requested_ = true;
          this->retry_read_after_auth_ = true;
          this->pending_handle_ = 0;
          this->apply_smp_params_();
          esp_ble_set_encryption(this->parent_->get_remote_bda(),
                                  ESP_BLE_SEC_ENCRYPT_MITM);
          break;
        }
        ESP_LOGW(TAG, "Read failed for %s, status=%d",
                 this->pending_char_uuid_.c_str(), param->read.status);
        this->emit_data_(this->pending_char_uuid_, "", "read_failed");
        this->pending_handle_ = 0;
        break;
      }

      if (param->read.handle == this->pending_handle_) {
        std::string hex_payload =
            format_hex(param->read.value, param->read.value_len);

        ESP_LOGI(TAG, "Read %s: %s (%d bytes)",
                 this->pending_char_uuid_.c_str(),
                 hex_payload.c_str(), param->read.value_len);

        this->emit_data_(this->pending_char_uuid_, hex_payload);

        this->pending_handle_ = 0;
      }
      break;
    }

    case ESP_GATTC_WRITE_CHAR_EVT: {
      if (param->write.status == ESP_GATT_OK) {
        ESP_LOGI(TAG, "Write confirmed for handle 0x%04X", param->write.handle);
      } else {
        ESP_LOGW(TAG, "Write FAILED for handle 0x%04X, status=%d",
                 param->write.handle, param->write.status);
      }
      break;
    }

    case ESP_GATTC_REG_FOR_NOTIFY_EVT: {
      if (param->reg_for_notify.status == ESP_GATT_OK) {
        ESP_LOGI(TAG, "Notify registered for handle 0x%04X",
                 param->reg_for_notify.handle);

        auto it = this->cccd_map_.find(param->reg_for_notify.handle);
        if (it != this->cccd_map_.end()) {
          // Use 0x0002 for indicate, 0x0001 for notify, 0x0003 for both
          uint16_t cccd_val = 0x0001;
          auto props_it = this->char_props_map_.find(param->reg_for_notify.handle);
          if (props_it != this->char_props_map_.end()) {
            bool has_notify = props_it->second & ESP_GATT_CHAR_PROP_BIT_NOTIFY;
            bool has_indicate = props_it->second & ESP_GATT_CHAR_PROP_BIT_INDICATE;
            if (has_indicate && has_notify)
              cccd_val = 0x0003;
            else if (has_indicate)
              cccd_val = 0x0002;
          }
          esp_ble_gattc_write_char_descr(
              gattc_if,
              this->parent_->get_conn_id(),
              it->second,
              sizeof(cccd_val),
              (uint8_t *) &cccd_val,
              ESP_GATT_WRITE_TYPE_RSP,
              ESP_GATT_AUTH_REQ_NONE);
          ESP_LOGI(TAG, "CCCD written for handle 0x%04X (descr 0x%04X, value 0x%04X)",
                   param->reg_for_notify.handle, it->second, cccd_val);
        }
      } else {
        ESP_LOGW(TAG, "Notify registration failed, status=%d",
                 param->reg_for_notify.status);
      }
      break;
    }

    case ESP_GATTC_NOTIFY_EVT: {
      auto it = this->notify_map_.find(param->notify.handle);
      if (it == this->notify_map_.end())
        break;

      // Throttle: max 1 event per notify_throttle_ms_ per characteristic
      uint32_t now = millis();
      auto last_it = this->last_notify_ms_.find(param->notify.handle);
      if (last_it != this->last_notify_ms_.end() &&
          (now - last_it->second) < this->notify_throttle_ms_) {
        break;
      }
      this->last_notify_ms_[param->notify.handle] = now;

      std::string hex_payload =
          format_hex(param->notify.value, param->notify.value_len);

      ESP_LOGD(TAG, "%s %s: %s (%d bytes)",
               param->notify.is_notify ? "Notify" : "Indicate",
               it->second.c_str(),
               hex_payload.c_str(), param->notify.value_len);

      this->emit_data_(it->second, hex_payload);
      break;
    }

    default:
      break;
  }
}

void SonicareCoordinator::on_gap_event(esp_gap_ble_cb_event_t event,
                                         esp_ble_gap_cb_param_t *param) {
  if (this->parent_ == nullptr)
    return;
  switch (event) {
    case ESP_GAP_BLE_NC_REQ_EVT: {
      ESP_LOGI(TAG, "Numeric Comparison request — auto-confirming (passkey %06lu)",
               (unsigned long) param->ble_security.key_notif.passkey);
      esp_ble_confirm_reply(param->ble_security.key_notif.bd_addr, true);
      break;
    }

    case ESP_GAP_BLE_AUTH_CMPL_EVT: {
      auto &auth = param->ble_security.auth_cmpl;
      // Only handle events for our device
      if (this->parent_ == nullptr)
        break;
      auto *our_bda = this->parent_->get_remote_bda();
      if (memcmp(auth.bd_addr, our_bda, 6) != 0)
        break;

      if (auth.success) {
        ESP_LOGI(TAG,
                 "Pairing successful — device bonded "
                 "(%02X:%02X:%02X:%02X:%02X:%02X, auth_mode=%d)",
                 auth.bd_addr[0], auth.bd_addr[1], auth.bd_addr[2],
                 auth.bd_addr[3], auth.bd_addr[4], auth.bd_addr[5],
                 auth.auth_mode);
        this->auth_completed_ = true;
        this->rapid_disconnect_count_ = 0;
        this->auth_fail_count_ = 0;
        // Pair-mode: bond is in place. Emit pair_complete immediately so HA
        // can finish the config flow even if the brush goes to sleep before
        // service discovery completes (we used to wait for the probe-read
        // success branch, but on slow brushes that can be 30-90 s away and
        // the HA-side timer fires first). model and ble_name will be empty
        // here — HA fetches them via ble_get_info right after pair_complete.
        if (this->pair_mode_active_) {
          char identity_str[18];
          snprintf(identity_str, sizeof(identity_str),
                   "%02X:%02X:%02X:%02X:%02X:%02X",
                   auth.bd_addr[0], auth.bd_addr[1], auth.bd_addr[2],
                   auth.bd_addr[3], auth.bd_addr[4], auth.bd_addr[5]);
          this->identity_address_ = identity_str;
          this->pair_mode_active_ = false;
          this->pair_mode_until_ms_ = 0;
          this->target_mac_.clear();
          this->emit_status_("pair_complete", {
              {"identity_address", this->identity_address_},
              {"bonding", "bonded"},
              {"note", "post_auth_no_probe"},
          });
        }
        if (this->retry_read_after_auth_ && !this->pending_char_uuid_.empty()) {
          ESP_LOGI(TAG, "Retrying read of %s after successful auth",
                   this->pending_char_uuid_.c_str());
          this->retry_read_after_auth_ = false;
          this->encryption_requested_ = false;  // allow re-arm if retry also fails
          std::string svc = this->pending_service_uuid_;
          std::string chr = this->pending_char_uuid_;
          this->read_characteristic(svc, chr);
        }
      } else {
        ESP_LOGW(TAG, "Authentication FAILED (reason=0x%X)", auth.fail_reason);
        if (this->retry_read_after_auth_ && !this->pending_char_uuid_.empty()) {
          this->emit_data_(this->pending_char_uuid_, "", "auth_failed");
          this->retry_read_after_auth_ = false;
          this->pending_char_uuid_.clear();
          this->pending_service_uuid_.clear();
        }
        esp_ble_remove_bond_device(auth.bd_addr);
        // Pair-mode: user just asked to bond. Don't enter the auth-backoff
        // path — that would freeze the bridge for 60 s and effectively kill
        // pair-mode. Reset the fail counter and let parse_device pick up the
        // next advert; the pair_mode_until_ms_ timer is the real bound.
        if (this->pair_mode_active_) {
          ESP_LOGI(TAG, "Auth-fail in pair-mode — bond wiped, scanning for next advert");
          this->auth_fail_count_ = 0;
          this->encryption_requested_ = false;
          break;
        }
        this->auth_fail_count_++;
        if (this->auth_fail_count_ >= MAX_AUTH_FAILURES) {
          ESP_LOGE(TAG, "Too many auth failures (%d) — backing off for %lus",
                   this->auth_fail_count_, AUTH_BACKOFF_MS / 1000);
          this->backoff_until_ms_ = millis() + AUTH_BACKOFF_MS;
          if (this->set_enabled_cb_)
            this->set_enabled_cb_(false);
          char fail_str[4], backoff_str[8];
          snprintf(fail_str, sizeof(fail_str), "%d", this->auth_fail_count_);
          snprintf(backoff_str, sizeof(backoff_str), "%lu", AUTH_BACKOFF_MS / 1000);
          this->emit_status_("auth_failed",
                              {
                                  {"fail_count", std::string(fail_str)},
                                  {"backoff_s", std::string(backoff_str)},
                              });
        }
      }
      break;
    }

    default:
      break;
  }
}

void SonicareCoordinator::read_characteristic(const std::string &service_uuid,
                                                const std::string &char_uuid) {
  if (!this->connected_) {
    ESP_LOGW(TAG, "Cannot read: not connected");
    this->emit_data_(char_uuid, "", "not_connected");
    return;
  }
  if (!this->services_discovered_) {
    if (this->pending_calls_.size() >= MAX_PENDING_CALLS) {
      ESP_LOGW(TAG, "Pending queue full — dropping read for %s", char_uuid.c_str());
      this->emit_data_(char_uuid, "", "queue_full");
      return;
    }
    ESP_LOGD(TAG, "Queueing read of %s until service discovery completes",
             char_uuid.c_str());
    this->pending_calls_.push_back(
        [this, service_uuid, char_uuid]() { this->read_characteristic(service_uuid, char_uuid); });
    return;
  }

  auto svc = parse_uuid(service_uuid);
  auto chr_uuid_obj = parse_uuid(char_uuid);

  auto *chr = this->parent_->get_characteristic(svc, chr_uuid_obj);
  if (chr == nullptr) {
    ESP_LOGW(TAG, "Characteristic %s not found in service %s",
             char_uuid.c_str(), service_uuid.c_str());
    this->emit_data_(char_uuid, "", "not_found");
    return;
  }

  this->pending_handle_ = chr->handle;
  this->pending_char_uuid_ = char_uuid;
  this->pending_service_uuid_ = service_uuid;

  ESP_LOGI(TAG, "Reading %s (handle 0x%04X)...",
           char_uuid.c_str(), chr->handle);

  auto status = esp_ble_gattc_read_char(
      this->parent_->get_gattc_if(),
      this->parent_->get_conn_id(),
      chr->handle,
      ESP_GATT_AUTH_REQ_NONE);

  if (status != ESP_OK) {
    ESP_LOGW(TAG, "Read request failed: %d", status);
    this->pending_handle_ = 0;
    char err_str[16];
    snprintf(err_str, sizeof(err_str), "gatt_err_%d", status);
    this->emit_data_(char_uuid, "", std::string(err_str));
  }
}

void SonicareCoordinator::subscribe(const std::string &service_uuid,
                                      const std::string &char_uuid) {
  if (!this->connected_) {
    ESP_LOGW(TAG, "Cannot subscribe: not connected");
    return;
  }
  if (!this->services_discovered_) {
    if (this->pending_calls_.size() >= MAX_PENDING_CALLS) {
      ESP_LOGW(TAG, "Pending queue full — dropping subscribe for %s", char_uuid.c_str());
      return;
    }
    ESP_LOGD(TAG, "Queueing subscribe of %s until service discovery completes",
             char_uuid.c_str());
    this->pending_calls_.push_back(
        [this, service_uuid, char_uuid]() { this->subscribe(service_uuid, char_uuid); });
    return;
  }

  auto svc = parse_uuid(service_uuid);
  auto chr_uuid_obj = parse_uuid(char_uuid);

  auto *chr = this->parent_->get_characteristic(svc, chr_uuid_obj);
  if (chr == nullptr) {
    ESP_LOGW(TAG, "Characteristic %s not found in service %s",
             char_uuid.c_str(), service_uuid.c_str());
    return;
  }

  // Check if already subscribed (e.g., restored after reconnect)
  if (this->notify_map_.count(chr->handle)) {
    ESP_LOGD(TAG, "Already subscribed to %s (handle 0x%04X), skipping",
             char_uuid.c_str(), chr->handle);
    return;
  }

  uint16_t cccd_handle = this->find_cccd_handle_(chr->handle);
  this->cccd_map_[chr->handle] = cccd_handle;
  this->char_props_map_[chr->handle] = chr->properties;

  this->notify_map_[chr->handle] = char_uuid;

  // Track for auto-resubscribe after reconnect
  bool already_tracked = false;
  for (const auto &entry : this->desired_subscriptions_) {
    if (entry.first == service_uuid && entry.second == char_uuid) {
      already_tracked = true;
      break;
    }
  }
  if (!already_tracked) {
    this->desired_subscriptions_.push_back(
        std::make_pair(service_uuid, char_uuid));
  }

  ESP_LOGI(TAG, "Subscribing to %s (handle 0x%04X, cccd 0x%04X)...",
           char_uuid.c_str(), chr->handle, cccd_handle);

  auto status = esp_ble_gattc_register_for_notify(
      this->parent_->get_gattc_if(),
      this->parent_->get_remote_bda(),
      chr->handle);

  if (status != ESP_OK) {
    ESP_LOGW(TAG, "Subscribe failed: %d", status);
    this->notify_map_.erase(chr->handle);
    this->cccd_map_.erase(chr->handle);
  }
}

void SonicareCoordinator::unsubscribe(const std::string &service_uuid,
                                        const std::string &char_uuid) {
  if (!this->connected_) {
    ESP_LOGW(TAG, "Cannot unsubscribe: not connected");
    return;
  }
  if (!this->services_discovered_) {
    if (this->pending_calls_.size() >= MAX_PENDING_CALLS) {
      ESP_LOGW(TAG, "Pending queue full — dropping unsubscribe for %s", char_uuid.c_str());
      return;
    }
    ESP_LOGD(TAG, "Queueing unsubscribe of %s until service discovery completes",
             char_uuid.c_str());
    this->pending_calls_.push_back(
        [this, service_uuid, char_uuid]() { this->unsubscribe(service_uuid, char_uuid); });
    return;
  }

  auto svc = parse_uuid(service_uuid);
  auto chr_uuid_obj = parse_uuid(char_uuid);

  auto *chr = this->parent_->get_characteristic(svc, chr_uuid_obj);
  if (chr == nullptr) {
    ESP_LOGW(TAG, "Characteristic %s not found in service %s",
             char_uuid.c_str(), service_uuid.c_str());
    return;
  }

  // Remove from desired subscriptions (connected unsubscribe = intentional)
  for (auto it = this->desired_subscriptions_.begin();
       it != this->desired_subscriptions_.end(); ++it) {
    if (it->first == service_uuid && it->second == char_uuid) {
      this->desired_subscriptions_.erase(it);
      break;
    }
  }

  ESP_LOGI(TAG, "Unsubscribing from %s (handle 0x%04X)...",
           char_uuid.c_str(), chr->handle);

  esp_ble_gattc_unregister_for_notify(
      this->parent_->get_gattc_if(),
      this->parent_->get_remote_bda(),
      chr->handle);

  this->notify_map_.erase(chr->handle);
}

void SonicareCoordinator::write_characteristic(const std::string &service_uuid,
                                                 const std::string &char_uuid,
                                                 const std::string &hex_data) {
  if (!this->connected_) {
    ESP_LOGW(TAG, "Cannot write: not connected");
    return;
  }
  if (!this->services_discovered_) {
    if (this->pending_calls_.size() >= MAX_PENDING_CALLS) {
      ESP_LOGW(TAG, "Pending queue full — dropping write for %s", char_uuid.c_str());
      return;
    }
    ESP_LOGD(TAG, "Queueing write of %s until service discovery completes",
             char_uuid.c_str());
    this->pending_calls_.push_back(
        [this, service_uuid, char_uuid, hex_data]() {
          this->write_characteristic(service_uuid, char_uuid, hex_data);
        });
    return;
  }

  auto svc = parse_uuid(service_uuid);
  auto chr_uuid_obj = parse_uuid(char_uuid);

  auto *chr = this->parent_->get_characteristic(svc, chr_uuid_obj);
  if (chr == nullptr) {
    ESP_LOGW(TAG, "Characteristic %s not found in service %s",
             char_uuid.c_str(), service_uuid.c_str());
    return;
  }

  std::vector<uint8_t> bytes;
  size_t count = hex_data.length() / 2;
  if (count == 0 || !parse_hex(hex_data, bytes, count)) {
    ESP_LOGW(TAG, "Invalid hex data: %s", hex_data.c_str());
    return;
  }

  ESP_LOGI(TAG, "Writing %s (handle 0x%04X): %s (%d bytes)",
           char_uuid.c_str(), chr->handle,
           hex_data.c_str(), bytes.size());

  auto status = esp_ble_gattc_write_char(
      this->parent_->get_gattc_if(),
      this->parent_->get_conn_id(),
      chr->handle,
      bytes.size(),
      bytes.data(),
      ESP_GATT_WRITE_TYPE_RSP,
      ESP_GATT_AUTH_REQ_NONE);

  if (status != ESP_OK) {
    ESP_LOGW(TAG, "Write request failed: %d", status);
  }
}

void SonicareCoordinator::list_services() {
  if (this->parent_ == nullptr || !this->services_discovered_) {
    ESP_LOGW(TAG, "list_services: not connected or service discovery incomplete");
    return;
  }

  esp_gatt_if_t gattc_if = this->parent_->get_gattc_if();
  uint16_t conn_id = this->parent_->get_conn_id();
  std::string mac = this->get_device_mac();

  uint16_t svc_count = 0;
  esp_ble_gattc_get_attr_count(gattc_if, conn_id,
                                ESP_GATT_DB_PRIMARY_SERVICE,
                                0, 0xFFFF,
                                ESP_GATT_ILLEGAL_HANDLE,
                                &svc_count);

  ESP_LOGI(TAG, "Listing %u service(s) on %s", svc_count, mac.c_str());

  // One event per service to keep payload size below the API frame limit.
  // HA-side aggregates by collecting all events with matching mac until
  // service_index == service_count - 1 (or by short timeout).
  char count_buf[8];
  snprintf(count_buf, sizeof(count_buf), "%u", (unsigned) svc_count);

  if (svc_count == 0) {
    this->emit_(EVENT_SERVICES, {
        {"mac", mac},
        {"service_count", count_buf},
        {"service_index", "0"},
        {"service_uuid", ""},
        {"service_chars", ""},
    });
    return;
  }

  auto *services = (esp_gattc_service_elem_t *) malloc(
      svc_count * sizeof(esp_gattc_service_elem_t));
  if (services == nullptr) {
    ESP_LOGE(TAG, "list_services: malloc failed");
    return;
  }

  uint16_t actual = svc_count;
  esp_err_t err = esp_ble_gattc_get_service(gattc_if, conn_id, nullptr,
                                              services, &actual, 0);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "esp_ble_gattc_get_service failed: %d", err);
    free(services);
    return;
  }

  snprintf(count_buf, sizeof(count_buf), "%u", (unsigned) actual);

  for (uint16_t i = 0; i < actual; i++) {
    char svc_uuid_buf[esp32_ble::UUID_STR_LEN];
    auto svc_uuid = espbt::ESPBTUUID::from_uuid(services[i].uuid);
    std::string svc_uuid_str(svc_uuid.to_str(svc_uuid_buf));

    // Enumerate characteristics within this service
    uint16_t char_count = 0;
    esp_ble_gattc_get_attr_count(gattc_if, conn_id,
                                  ESP_GATT_DB_CHARACTERISTIC,
                                  services[i].start_handle,
                                  services[i].end_handle,
                                  ESP_GATT_ILLEGAL_HANDLE,
                                  &char_count);

    std::string chars_str;
    if (char_count > 0) {
      auto *chars = (esp_gattc_char_elem_t *) malloc(
          char_count * sizeof(esp_gattc_char_elem_t));
      if (chars != nullptr) {
        uint16_t actual_chars = char_count;
        esp_err_t cerr = esp_ble_gattc_get_all_char(
            gattc_if, conn_id,
            services[i].start_handle, services[i].end_handle,
            chars, &actual_chars, 0);
        if (cerr == ESP_OK) {
          for (uint16_t j = 0; j < actual_chars; j++) {
            char chr_uuid_buf[esp32_ble::UUID_STR_LEN];
            auto chr_uuid = espbt::ESPBTUUID::from_uuid(chars[j].uuid);
            if (!chars_str.empty())
              chars_str += ",";
            chars_str += chr_uuid.to_str(chr_uuid_buf);
            uint8_t props = chars[j].properties;
            std::string p;
            if (props & ESP_GATT_CHAR_PROP_BIT_READ)     p += "R";
            if (props & ESP_GATT_CHAR_PROP_BIT_WRITE)    p += "W";
            if (props & ESP_GATT_CHAR_PROP_BIT_WRITE_NR) p += "w";
            if (props & ESP_GATT_CHAR_PROP_BIT_NOTIFY)   p += "N";
            if (props & ESP_GATT_CHAR_PROP_BIT_INDICATE) p += "I";
            if (!p.empty())
              chars_str += "/" + p;
          }
        } else {
          ESP_LOGW(TAG, "esp_ble_gattc_get_all_char failed: %d", cerr);
        }
        free(chars);
      }
    }

    char idx_buf[8];
    snprintf(idx_buf, sizeof(idx_buf), "%u", (unsigned) i);

    ESP_LOGI(TAG, "  [%u] %s → %s", i, svc_uuid_str.c_str(),
             chars_str.empty() ? "(no chars)" : chars_str.c_str());

    this->emit_(EVENT_SERVICES, {
        {"mac", mac},
        {"service_count", count_buf},
        {"service_index", idx_buf},
        {"service_uuid", svc_uuid_str},
        {"service_chars", chars_str},
    });
  }

  free(services);
}

std::map<std::string, std::string> SonicareCoordinator::collect_info_data() {
  char uptime_str[16];
  snprintf(uptime_str, sizeof(uptime_str), "%u", millis() / 1000);

  char heap_str[16];
  snprintf(heap_str, sizeof(heap_str), "%u", (uint32_t) esp_get_free_heap_size());

  char subs_str[8];
  snprintf(subs_str, sizeof(subs_str), "%u", (uint32_t) this->notify_map_.size());

  char throttle_str[16];
  snprintf(throttle_str, sizeof(throttle_str), "%u", this->notify_throttle_ms_);

  // Check bond status
  int bond_count = esp_ble_get_bond_device_num();
  bool is_paired = false;
  if (bond_count > 0 && this->parent_ != nullptr) {
    esp_ble_bond_dev_t *bonded = (esp_ble_bond_dev_t *) malloc(
        bond_count * sizeof(esp_ble_bond_dev_t));
    if (bonded) {
      esp_ble_get_bond_device_list(&bond_count, bonded);
      auto *our_bda = this->parent_->get_remote_bda();
      for (int i = 0; i < bond_count; i++) {
        if (memcmp(bonded[i].bd_addr, our_bda, 6) == 0) {
          is_paired = true;
          break;
        }
      }
      free(bonded);
    }
  }

  // pair_capable = standalone mode AND no persisted identity AND not connected.
  // External (Mode A) bridges have a fixed MAC in YAML and never use pair-mode.
  bool pair_capable = (this->mode_ == MODE_STANDALONE) &&
                      this->identity_address_.empty() &&
                      !this->connected_;

  std::map<std::string, std::string> info = {
      {"ble_connected", this->connected_ ? "true" : "false"},
      {"paired", is_paired ? "true" : "false"},
      {"mac", this->get_device_mac()},
      {"uptime_s", std::string(uptime_str)},
      {"free_heap", std::string(heap_str)},
      {"subscriptions", std::string(subs_str)},
      {"notify_throttle_ms", std::string(throttle_str)},
      {"mode", this->mode_.empty() ? std::string(MODE_EXTERNAL) : this->mode_},
      {"identity_address", this->identity_address_},
      {"pair_capable", pair_capable ? "true" : "false"},
      {"pair_mode_active", this->pair_mode_active_ ? "true" : "false"},
  };
  if (!this->remote_name_.empty()) {
    info["ble_name"] = this->remote_name_;
  }
  if (!this->model_number_.empty()) {
    info["model"] = this->model_number_;
  }

  const std::string &bid = this->bridge_ ? this->bridge_->get_bridge_id()
                                          : std::string();
  ESP_LOGI(TAG, "Info[%s]: v%s uptime=%ss heap=%s subs=%s name=%s model=%s",
           bid.empty() ? "default" : bid.c_str(),
           PHILIPS_SONICARE_VERSION, uptime_str, heap_str, subs_str,
           this->remote_name_.empty() ? "(none)" : this->remote_name_.c_str(),
           this->model_number_.empty() ? "(none)" : this->model_number_.c_str());

  return info;
}

uint16_t SonicareCoordinator::find_cccd_handle_(uint16_t char_handle) {
  // Try ESP-IDF API first — queries the GATT table directly,
  // bypassing ESPHome's potentially empty descriptor cache.
  uint16_t count = 1;
  esp_gattc_descr_elem_t result;
  memset(&result, 0, sizeof(result));
  esp_bt_uuid_t cccd_uuid;
  cccd_uuid.len = ESP_UUID_LEN_16;
  cccd_uuid.uuid.uuid16 = 0x2902;

  auto status = esp_ble_gattc_get_descr_by_char_handle(
      this->parent_->get_gattc_if(),
      this->parent_->get_conn_id(),
      char_handle,
      cccd_uuid,
      &result,
      &count);

  if (status == ESP_GATT_OK && count > 0) {
    ESP_LOGD(TAG, "CCCD found via ESP-IDF API: handle 0x%04X for char 0x%04X",
             result.handle, char_handle);
    return result.handle;
  }

  // Fallback: handle + 1 (standard BLE layout)
  uint16_t fallback = char_handle + 1;
  ESP_LOGW(TAG, "CCCD not found via API for char 0x%04X, using fallback 0x%04X",
           char_handle, fallback);
  return fallback;
}

void SonicareCoordinator::resubscribe_all_() {
  for (const auto &entry : this->desired_subscriptions_) {
    const auto &svc_uuid_str = entry.first;
    const auto &chr_uuid_str = entry.second;

    auto svc = parse_uuid(svc_uuid_str);
    auto chr_uuid = parse_uuid(chr_uuid_str);

    auto *chr = this->parent_->get_characteristic(svc, chr_uuid);
    if (chr == nullptr) {
      ESP_LOGW(TAG, "Resubscribe: characteristic %s not found, skipping",
               chr_uuid_str.c_str());
      continue;
    }

    uint16_t cccd_handle = this->find_cccd_handle_(chr->handle);
    this->cccd_map_[chr->handle] = cccd_handle;
    this->char_props_map_[chr->handle] = chr->properties;
    this->notify_map_[chr->handle] = chr_uuid_str;

    auto status = esp_ble_gattc_register_for_notify(
        this->parent_->get_gattc_if(),
        this->parent_->get_remote_bda(),
        chr->handle);

    if (status == ESP_OK) {
      ESP_LOGI(TAG, "Resubscribe: %s (handle 0x%04X, cccd 0x%04X)",
               chr_uuid_str.c_str(), chr->handle, cccd_handle);
    } else {
      ESP_LOGW(TAG, "Resubscribe failed for %s: %d",
               chr_uuid_str.c_str(), status);
      this->notify_map_.erase(chr->handle);
      this->cccd_map_.erase(chr->handle);
    }
  }
}

}  // namespace philips_sonicare
}  // namespace esphome
