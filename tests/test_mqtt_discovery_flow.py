"""MQTT discovery: a gateway announces a brush on sonicare_gateway/<id>/info."""

from __future__ import annotations

import json

import pytest
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.philips_sonicare_ble.config_flow as cf
from custom_components.philips_sonicare_ble.const import (
    CONF_ADDRESS,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    SVC_CONDOR,
    TRANSPORT_MQTT_GATEWAY,
)

ADDRESS = "AA:BB:CC:DD:EE:02"
TOPIC = "sonicare_gateway/aabbccddee02/info"


def _info(payload, topic: str = TOPIC) -> MqttServiceInfo:
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    return MqttServiceInfo(
        topic=topic, payload=payload, qos=1, retain=True,
        subscribed_topic="sonicare_gateway/+/info", timestamp=0,
    )


def _flow(hass) -> cf.PhilipsSonicareConfigFlow:
    flow = cf.PhilipsSonicareConfigFlow()
    flow.hass = hass
    flow.context = {"source": "mqtt"}
    flow.flow_id = "test-flow"
    flow.handler = DOMAIN
    return flow


GOOD = {"protocol": 1, "address": ADDRESS, "model": "HX7410", "firmware": "1.2.3"}


async def test_announcement_leads_to_an_mqtt_entry(hass) -> None:
    flow = _flow(hass)
    result = await flow.async_step_mqtt(_info(GOOD))
    assert result["type"] == "form" and result["step_id"] == "mqtt_confirm"
    assert result["description_placeholders"]["model"] == "HX7410"

    result = await flow.async_step_mqtt_confirm({"device_name": "Bad"})
    assert result["type"] == "create_entry"
    data = result["data"]
    assert data[CONF_ADDRESS] == ADDRESS
    assert data[CONF_TRANSPORT_TYPE] == TRANSPORT_MQTT_GATEWAY
    assert SVC_CONDOR in data[CONF_SERVICES]
    assert flow.unique_id == ADDRESS


async def test_unknown_protocol_version_is_refused(hass) -> None:
    result = await _flow(hass).async_step_mqtt(_info({**GOOD, "protocol": 2}))
    assert result["type"] == "abort"
    assert result["reason"] == "mqtt_gateway_unsupported"


async def test_empty_or_broken_payload_is_refused(hass) -> None:
    for payload in ("", "not json", "[1, 2]"):
        result = await _flow(hass).async_step_mqtt(_info(payload))
        assert result["reason"] == "mqtt_gateway_unsupported"


async def test_address_must_match_the_topic(hass) -> None:
    other = {**GOOD, "address": "11:22:33:44:55:66"}
    result = await _flow(hass).async_step_mqtt(_info(other))
    assert result["reason"] == "mqtt_gateway_unsupported"


async def test_brush_already_set_up_aborts(hass) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={}).add_to_hass(hass)
    with pytest.raises(AbortFlow) as err:
        await _flow(hass).async_step_mqtt(_info(GOOD))
    assert err.value.reason.startswith("already_configured")
