import zlib

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import automation
from esphome.components import binary_sensor, ble_client, esp32_ble_tracker
from esphome.const import (
    CONF_ID,
    CONF_MAC_ADDRESS,
    CONF_ON_CONNECT,
    CONF_ON_DISCONNECT,
)
from esphome.core import ID as CoreID

CONF_AUTO_CONNECT = "auto_connect"

DEPENDENCIES = ["esp32_ble_tracker", "api"]
AUTO_LOAD = ["binary_sensor", "esp32_ble_client"]
MULTI_CONF = True

CONF_BLE_CLIENT_ID = "ble_client_id"
CONF_BRIDGE_GENERATED_ID = "bridge_generated_id"
CONF_COORD_GENERATED_ID = "coord_generated_id"
CONF_CONNECTED_SENSOR = "connected"
CONF_NOTIFY_THROTTLE = "notify_throttle_ms"
CONF_BRIDGE_ID = "bridge_id"

philips_sonicare_ns = cg.esphome_ns.namespace("philips_sonicare")
PhilipsSonicare = philips_sonicare_ns.class_(
    "PhilipsSonicare",
    ble_client.BLEClientNode,
    cg.Component,
)
PhilipsSonicareStandalone = philips_sonicare_ns.class_(
    "PhilipsSonicareStandalone",
    cg.Component,
)
SonicareBridge = philips_sonicare_ns.class_(
    "SonicareBridge",
    cg.Component,
)
SonicareCoordinator = philips_sonicare_ns.class_("SonicareCoordinator")
SonicareConnectTrigger = philips_sonicare_ns.class_(
    "SonicareConnectTrigger", automation.Trigger.template()
)
SonicareDisconnectTrigger = philips_sonicare_ns.class_(
    "SonicareDisconnectTrigger", automation.Trigger.template()
)

# Shared optional fields for both modes (type-agnostic — declare_id added per mode)
_BASE_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_BRIDGE_GENERATED_ID): cv.declare_id(SonicareBridge),
        cv.GenerateID(CONF_COORD_GENERATED_ID): cv.declare_id(SonicareCoordinator),
        cv.Optional(CONF_BRIDGE_ID, default=""): cv.string,
        cv.Optional(CONF_NOTIFY_THROTTLE, default=500): cv.positive_int,
        cv.Optional(CONF_CONNECTED_SENSOR): binary_sensor.binary_sensor_schema(
            device_class="connectivity",
        ),
        # Triggers fire on the Coordinator's ready/disconnect callbacks, so they
        # work in both modes. Mode A's external `ble_client.on_connect` fires on
        # raw GAP-connect (before service discovery), which is too early for
        # subscribe()/read() — these triggers fire on `ready` instead.
        cv.Optional(CONF_ON_CONNECT): automation.validate_automation(single=True),
        cv.Optional(CONF_ON_DISCONNECT): automation.validate_automation(single=True),
    }
).extend(cv.COMPONENT_SCHEMA)

# Mode A: external ble_client (backward compatible) — Worker is PhilipsSonicare.
# `ble_client_id` is Required (not GenerateID) so cv.Any can cleanly route a
# config without it to _INTERNAL_SCHEMA. With GenerateID + use_id the deferred
# lookup fires after schema match and bypasses cv.Any's fallback.
_EXTERNAL_SCHEMA = _BASE_SCHEMA.extend(
    {
        cv.GenerateID(): cv.declare_id(PhilipsSonicare),
        cv.Required(CONF_BLE_CLIENT_ID): cv.use_id(ble_client.BLEClient),
    }
)

# Mode B: standalone client — Worker IS the BLE client (BLEClientBase subclass)
_INTERNAL_SCHEMA = (
    _BASE_SCHEMA.extend(esp32_ble_tracker.ESP_BLE_DEVICE_SCHEMA)
    .extend(
        {
            cv.GenerateID(): cv.declare_id(PhilipsSonicareStandalone),
            cv.Optional(CONF_MAC_ADDRESS): cv.mac_address,
            cv.Optional(CONF_AUTO_CONNECT): cv.boolean,
        }
    )
)


def _internal_set_defaults(config):
    # Without mac_address, the bridge would auto-pair with the first Sonicare
    # in range — risky in mixed Direct-BLE/ESP setups or multi-brush households.
    # So auto_connect defaults to false unless the user explicitly targets one
    # device via mac_address (or opts in by setting auto_connect: true).
    if CONF_AUTO_CONNECT not in config:
        config[CONF_AUTO_CONNECT] = CONF_MAC_ADDRESS in config
    return config


CONFIG_SCHEMA = cv.Any(
    _EXTERNAL_SCHEMA,
    cv.All(_INTERNAL_SCHEMA, _internal_set_defaults),
)

_instance_count = 0


async def to_code(config):
    global _instance_count
    _instance_count += 1

    bridge_id = config[CONF_BRIDGE_ID]
    if _instance_count > 1 and not bridge_id:
        raise cv.Invalid(
            "bridge_id is required when using multiple philips_sonicare instances."
        )

    # Per-instance log tag — every ESP_LOG call routes through it so multi-bridge
    # log streams are unambiguous and `logger:` can filter per bridge.
    log_tag = f"philips_sonicare.{bridge_id}" if bridge_id else "philips_sonicare"

    # Coordinator (plain C++ object — owns BLE/GATT logic, no Component lifecycle)
    coord_var = cg.new_Pvariable(config[CONF_COORD_GENERATED_ID])
    cg.add(coord_var.set_notify_throttle(config[CONF_NOTIFY_THROTTLE]))
    cg.add(coord_var.set_log_tag(log_tag))

    # Bridge (SonicareBridge) — HA service registration, event firing, sensors
    bridge_var = cg.new_Pvariable(config[CONF_BRIDGE_GENERATED_ID])
    await cg.register_component(bridge_var, config)
    cg.add(bridge_var.set_bridge_id(bridge_id))
    cg.add(bridge_var.set_log_tag(log_tag))
    cg.add(bridge_var.set_coordinator(coord_var))
    cg.add(coord_var.set_bridge(bridge_var))

    if CONF_CONNECTED_SENSOR in config:
        sens = await binary_sensor.new_binary_sensor(config[CONF_CONNECTED_SENSOR])
        cg.add(bridge_var.set_connected_sensor(sens))

    if CONF_BLE_CLIENT_ID in config:
        # Mode A: PhilipsSonicare worker as BLEClientNode of an external ble_client
        var = cg.new_Pvariable(config[CONF_ID])
        await cg.register_component(var, config)
        cg.add(var.set_coordinator(coord_var))
        cg.add(var.set_log_tag(log_tag))
        await ble_client.register_ble_node(var, config)
    else:
        # Mode B: PhilipsSonicareStandalone — extends BLEClientBase directly,
        # eliminates the dummy ble_client: block requirement.
        var = cg.new_Pvariable(config[CONF_ID])
        await cg.register_component(var, config)
        cg.add(var.set_coordinator(coord_var))
        cg.add(var.set_log_tag(log_tag))

        pref_ns = zlib.crc32(config[CONF_ID].id.encode())
        cg.add(var.set_pref_namespace(pref_ns))

        if CONF_MAC_ADDRESS in config:
            cg.add(var.set_address(config[CONF_MAC_ADDRESS].as_hex))
        cg.add(var.set_auto_connect(config[CONF_AUTO_CONNECT]))

        await esp32_ble_tracker.register_client(var, config)

    # Triggers (both modes) — hook into the Coordinator's ready/disconnect
    # callbacks, which fire after service discovery completes. The user
    # automation gets two named variables: `mac` (brush BLE address) and
    # `bridge_id` (the YAML bridge_id of the slot that fired, "" for single).
    trigger_args = [(cg.std_string, "mac"), (cg.std_string, "bridge_id")]
    if CONF_ON_CONNECT in config:
        trigger_id = CoreID(
            f"{config[CONF_ID].id}_on_connect_trigger",
            is_declaration=True,
            type=SonicareConnectTrigger,
        )
        trigger = cg.new_Pvariable(trigger_id, coord_var)
        await automation.build_automation(
            trigger, trigger_args, config[CONF_ON_CONNECT]
        )

    if CONF_ON_DISCONNECT in config:
        trigger_id = CoreID(
            f"{config[CONF_ID].id}_on_disconnect_trigger",
            is_declaration=True,
            type=SonicareDisconnectTrigger,
        )
        trigger = cg.new_Pvariable(trigger_id, coord_var)
        await automation.build_automation(
            trigger, trigger_args, config[CONF_ON_DISCONNECT]
        )
