import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import binary_sensor, ble_client
from esphome.const import CONF_ID

DEPENDENCIES = ["ble_client", "esp32_ble_tracker", "api"]
AUTO_LOAD = ["binary_sensor"]
MULTI_CONF = True

CONF_CONNECTED_SENSOR = "connected"
CONF_NOTIFY_THROTTLE = "notify_throttle_ms"
CONF_DEVICE_ID = "device_id"

philips_sonicare_ns = cg.esphome_ns.namespace("philips_sonicare")
PhilipsSonicare = philips_sonicare_ns.class_(
    "PhilipsSonicare",
    ble_client.BLEClientNode,
    cg.Component,
)

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(PhilipsSonicare),
            cv.Optional(CONF_DEVICE_ID, default=""): cv.string,
            cv.Optional(CONF_NOTIFY_THROTTLE, default=500): cv.positive_int,
            cv.Optional(CONF_CONNECTED_SENSOR): binary_sensor.binary_sensor_schema(
                device_class="connectivity",
            ),
        }
    )
    .extend(ble_client.BLE_CLIENT_SCHEMA)
    .extend(cv.COMPONENT_SCHEMA)
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await ble_client.register_ble_node(var, config)

    cg.add(var.set_device_id(config[CONF_DEVICE_ID]))
    cg.add(var.set_notify_throttle(config[CONF_NOTIFY_THROTTLE]))

    if CONF_CONNECTED_SENSOR in config:
        sens = await binary_sensor.new_binary_sensor(config[CONF_CONNECTED_SENSOR])
        cg.add(var.set_connected_sensor(sens))
