import zlib
from pathlib import Path

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
CONF_FRIENDLY_NAME = "friendly_name"
CONF_AREA = "area"

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
        cv.Optional(CONF_FRIENDLY_NAME, default=""): cv.string,
        cv.Optional(CONF_AREA, default=""): cv.string,
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


_INTERNAL_VALIDATOR = cv.All(_INTERNAL_SCHEMA, _internal_set_defaults)


def _validate_config(config):
    # Route to the appropriate schema based on the user's keys before any
    # schema runs. Previously this used cv.Any(_EXTERNAL_SCHEMA, _INTERNAL_VALIDATOR),
    # but nested schemas with deferred-ID generation (e.g. binary_sensor_schema
    # inside `connected:`) pollute cv.Any's backtracking — when Mode A's
    # validation attempt fires the deferred declare_id, Mode B can no longer
    # be entered cleanly and cv.Any reports Mode A's error verbatim.
    #
    # Explicit routing avoids backtracking entirely: presence of `ble_client_id`
    # selects Mode A, absence selects Mode B. Each schema then runs exactly
    # once against a fresh config dict.
    if CONF_BLE_CLIENT_ID in config:
        return _EXTERNAL_SCHEMA(config)
    return _INTERNAL_VALIDATOR(config)


CONFIG_SCHEMA = _validate_config

_instance_count = 0


async def to_code(config):
    global _instance_count
    _instance_count += 1

    # Single source of truth for the bridge firmware version: the VERSION file
    # next to this component. Baked into the binary as a compile define so the
    # ESP reports it at runtime (ble_get_info), and read by the HA integration's
    # update entity from GitHub — bump the file, no integration release needed.
    version = (Path(__file__).parent / "VERSION").read_text(encoding="utf-8").strip()
    # Pass the bare string — ESPHome's add_define runs the value through
    # safe_exp()/StringLiteral, which already wraps it in C quotes. Adding our
    # own quotes here would double-quote it (the macro would expand to the
    # literal string including the quote characters).
    cg.add_define("PHILIPS_SONICARE_BRIDGE_VERSION", version)

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
    cg.add(bridge_var.set_friendly_name(config[CONF_FRIENDLY_NAME]))
    cg.add(bridge_var.set_area(config[CONF_AREA]))
    cg.add(bridge_var.set_log_tag(log_tag))
    cg.add(bridge_var.set_coordinator(coord_var))
    cg.add(coord_var.set_bridge(bridge_var))

    if CONF_CONNECTED_SENSOR in config:
        sens = await binary_sensor.new_binary_sensor(config[CONF_CONNECTED_SENSOR])
        cg.add(bridge_var.set_connected_sensor(sens))

    if CONF_BLE_CLIENT_ID in config:
        # ESPHome's ble_client component does not emit USE_BLE_CLIENT itself,
        # so the Mode A class in philips_sonicare.h needs us to set it when
        # the user's YAML has wired up Mode A (the schema's cv.use_id
        # already guarantees a `ble_client:` block exists at this point).
        cg.add_define("USE_BLE_CLIENT")
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
