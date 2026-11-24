# MQTT gateway contract (draft, protocol version 1)

This describes how a gateway that holds the BLE connection to a Condor Sonicare (for example a phone app) publishes the brush on MQTT for the Home Assistant integration. It covers Condor handles only (the ones that advertise `Condor`, service `e50ba3c0-…`).

Status: **draft**. Names and fields can still change until both sides implement it.

## Split of responsibilities

```text
Philips Sonicare
      │ BLE: handshake, framing, sequence numbers, acks (< 250 ms)
      ▼
Gateway  ── runs the Condor session: reads every port, subscribes the
      │     default ports, keeps the state of every port
      │ MQTT: state, change indications, a few requests (this document)
      ▼
Home Assistant (philips_sonicare_ble)
      listens; maps ports to entities; switches the sensor stream
```

The gateway does **the session**:

- connect, bond and reconnect to the brush
- the V4 handshake (version negotiation, channel config, channel open, InitializeReq)
- framing and fragmenting, reassembling frames
- a TX_ACK for every incoming packet, within about 250 ms
- a ChangeIndicationResponse for every change indication
- on every new session: subscribe the default ports, read every port, publish the state of every port
- keep that state up to date from the change indications
- only one request in flight to the brush at a time

Home Assistant **listens**: it reads the state and the change indications and decides what the values mean. It sends requests only for what depends on its own state: switching the pressure/temperature sensor stream while the user brushes, and later writes. It never subscribes or unsubscribes the default ports.

## Topics

`<base>` is `sonicare_gateway/<id>`, where `<id>` is the brush's Bluetooth address in lower case without colons (`aabbccddeeff`). One gateway may serve several brushes; each gets its own `<base>`.

Use the brush's **identity address** (the one it was bonded with), not a resolvable private address it may advertise in between. Otherwise each address change creates a new base topic, and Home Assistant a new device.

| Topic | Direction | Retained | QoS | Payload |
|---|---|---|---|---|
| `<base>/status` | gateway → HA | yes | 1 | `online` / `offline` (last will) |
| `<base>/info` | gateway → HA | yes | 1 | device and gateway info |
| `<base>/link` | gateway → HA | yes | 1 | brush connection, session, snapshot |
| `<base>/state/<product>/<port>` | gateway → HA | yes | 1 | full properties of one port |
| `<base>/ind/<product>/<port>` | gateway → HA | no | 1 (0 for `*.b`) | one change indication |
| `<base>/cmd` | HA → gateway | no | 1 | one request |
| `<base>/resp` | gateway → HA | no | 1 | the answer to one request |

All payloads except `status` are UTF-8 JSON objects. Publish everything in the order it happens: a change indication must never overtake the state it follows.

### `<base>/status`

Gateway availability. Publish `online` (retained) after connecting to the broker, and register `offline` (retained) as the MQTT last will.

On start, publish `link` (with `connected: false` and a new `session`) **before** `online`: until it is replaced, a retained `link` from before the restart may still say `connected: true`.

### `<base>/info`

Published (retained) when the gateway starts, and again when a value changes (for example once it has read the Device Information service). Home Assistant discovers the gateway through this topic: a new retained `info` opens the setup dialog, a newer one replaces an open dialog for the same brush.

```json
{
  "protocol": 1,
  "gateway": "example-gateway",
  "gateway_version": "0.2.0",
  "address": "AA:BB:CC:DD:EE:FF",
  "name": "Philips Sonicare",
  "model": "HX7410",
  "firmware": "1.8.20.0",
  "serial": "…"
}
```

`protocol` is the version of this contract. `model`, `firmware` and `serial` are the Device Information strings (Model Number, Firmware Revision, Serial Number) as the brush reports them; leave out what is not known.

### `<base>/link`

The state of the BLE side and of the session, retained. Publish it again whenever a field changes.

```json
{
  "connected": true,
  "session": 1795548986,
  "snapshot": true,
  "ports": ["0/Firmware", "1/Sonicare", "1/RoutineStatus", "1/Battery", "1/BrushHead", "1/SessionStorage"],
  "sensor_mask": 0,
  "rssi": -61
}
```

- `connected`: the brush is connected **and** the Condor handshake is complete. `false` while connecting, after a disconnect, or while the handshake runs.
- `session`: a number for the current Condor session. Increase it every time a new session becomes ready. A value must **never repeat**, also not after the gateway restarts; start from the current time in seconds, for example.
- `snapshot`: `true` once the state of every port has been published for this session (see below). `false` when a session starts.
- `ports`: only with `snapshot: true`: the ports (`<product>/<port>`) whose state belongs to this session's snapshot.
- `sensor_mask`: the sensor stream currently switched on (see `sensor_stream` below), `0` when off. Back to `0` with every new session.
- `rssi`: optional, last known signal strength in dBm.

`link` is only valid while `status` is `online`. When the gateway drops off the broker, the last will sets `status` to `offline`, but the retained `link` may still say `connected: true`; Home Assistant ignores it then.

### Session start

When a new Condor session is ready, the gateway:

1. publishes `link` with `connected: true`, the new `session` and `snapshot: false`
2. subscribes the default ports: `1/Sonicare`, `1/RoutineStatus`, `1/Battery`, `1/BrushHead`, `1/SessionStorage` (Subscribe body `{"timeout":31536000}`)
3. reads every port: GetProds, then GetPorts for every product, then GetProps for every port except binary ports (`*.b`); ports that fail or return an empty object are left out
4. publishes the state of every port it read on `state/<product>/<port>` (retained)
5. clears (empty retained payload) every `state/…` topic it published in an earlier session that is not part of this one
6. publishes `link` with `snapshot: true` and `ports`

Change indications can arrive while the ports are read (the default ports are already subscribed). The gateway publishes them on `ind/…` as they come, and applies those that arrive during the read on top of what it read: they are at least as new.

### `<base>/state/<product>/<port>`

The full properties of one port, retained, tagged with the session they belong to:

```json
{"session": 1795548986, "props": {"BatteryPercent": 74, "BatteryMV": 3950}}
```

- `props` is always the **complete** object: the GetProps result, with every later change indication of that port merged in (top-level keys replaced). Publish it again after each change indication of that port, after the snapshot is complete.
- Never publish a partial object here; the change on its own goes to `ind/…`.
- Binary ports (`*.b`) have no state topic.
- Home Assistant uses a state only if its `session` matches the current `link.session` and the port is listed in `link.ports`.

### `<base>/ind/<product>/<port>`

Every change indication from the brush, published after the gateway has acked it to the brush. `<product>` and `<port>` are taken from the indication header (`1/RoutineStatus` → `ind/1/RoutineStatus`).

- JSON ports: the indication body as a JSON object, unchanged:

  ```json
  {"Duration": 42}
  ```

- Binary ports (name ends in `.b`, e.g. `SensorData.b`): the raw body as lower-case hex. The body is everything after the header NUL up to the frame length; do **not** strip trailing zero bytes:

  ```json
  {"hex": "01000700002c0100"}
  ```

`SensorData.b` sends several frames per second while brushing; QoS 0 is fine there.

### `<base>/cmd` and `<base>/resp`

Home Assistant sends one request per message:

```json
{"id": 17, "client": "ha-1a2b3c4d", "op": "sensor_stream", "mask": 3}
```

| `op` | Fields | What the gateway does |
|---|---|---|
| `sensor_stream` | `mask` (integer) | `mask` > 0: Subscribe `1/SensorData.b`, then PutProps `1/SensorData` `{"Sensors": mask}`. `mask` = 0: Unsubscribe `1/SensorData.b`. Sets `link.sensor_mask`. |
| `get_props` | `product`, `port` | GetProps (4) |
| `put_props` | `product`, `port`, `props` (object) | PutProps (3) |

`sensor_stream` can be sent any number of times with the same mask; the gateway sends the Subscribe only when the stream is not on yet. The default ports are not subject to requests.

`id` is an integer and `client` a string chosen by the sender; the gateway copies both into the answer. Product and port names are passed exactly as given (port names are case-sensitive on the brush).

The gateway answers every request on `<base>/resp`:

```json
{"id": 17, "client": "ha-1a2b3c4d", "status": 0, "data": {"BatteryPercent": 74}}
```

- `status`: the status byte of the brush's GenericResponse (0 = NoError, 3 = NoSuchPort, 8 = NoSuchProduct, …), 255 if the response had no status byte. For `sensor_stream`: 0 when the stream is in the requested state.
- `data`: the JSON body of the GenericResponse, parsed, or `null` if the body was empty or not JSON.

If the request could not be carried out, the gateway answers with `error` instead of `status` / `data`:

```json
{"id": 17, "client": "ha-1a2b3c4d", "error": "timeout"}
```

| `error` | Meaning |
|---|---|
| `not_connected` | no Condor session (see `link`) |
| `timeout` | the brush did not answer within 5 s |
| `bad_request` | unknown `op` or missing field |
| `failed` | anything else; add `"message"` with details |

The gateway queues requests and sends them to the brush one after another. With QoS 1 a request can arrive twice: the gateway runs a request with the same `client` and `id` only once, and answers a copy that arrives after the answer with the same answer again. When the brush disconnects, the gateway answers every request still in its queue with `not_connected`.

Status values other than 0 are passed on unchanged; the gateway does not retry or translate them.

Home Assistant waits up to 10 s for an answer before it gives up on a request. An answer that arrives later, or with another `client`, is ignored.

## Sequence

```text
gateway                                          Home Assistant
  link   = {"connected": false, "session": S}      (retained)
  info   = {...}                                   (retained)
  status = online                                  (retained, LWT offline)
        … brush connects, handshake done …
  link   = {"connected": true, "session": S+1, "snapshot": false}
        subscribe default ports, read every port
  state/0/Firmware      {"session": S+1, "props": {…}}
  state/1/Battery       {"session": S+1, "props": {…}}
  …
  link   = {…, "snapshot": true, "ports": [...]}
                                                 reads the snapshot
        … brushing …
  ind/1/Sonicare        {"HandleState": 2}
  state/1/Sonicare      {"session": S+1, "props": {… "HandleState": 2 …}}
                                                 cmd {"op": "sensor_stream", "mask": 3}
  resp {"status": 0}  ·  link {…, "sensor_mask": 3}
  ind/1/SensorData.b    {"hex": "…"}
  ind/1/RoutineStatus   {"Duration": 1}
  state/1/RoutineStatus {"session": S+1, "props": {… "Duration": 1 …}}
        …
                                                 cmd {"op": "sensor_stream", "mask": 0}
        … brush disconnects …
  link   = {"connected": false, "session": S+1, "snapshot": false}
```

## How Home Assistant reads it

| Home Assistant needs | Derived from |
|---|---|
| link to the brush exists | `status` is `online` and `link.connected` is `true` |
| new session, set up again | `link.session` changed, or `connected` went `false` → `true` |
| current values | `state/…` of the ports in `link.ports` with the current `session`, once `link.snapshot` is `true` |
| changes | `ind/…` |

After a Home Assistant restart in the middle of a session, the retained `link` and `state/…` topics give it the current values without any request to the brush.

## Home Assistant MQTT discovery in the gateway

Home Assistant creates the entities itself from this data. If the gateway also publishes its own MQTT discovery sensors for the same brush, every value shows up twice; that should be an option in the gateway that is off when the integration is used.

## Changes from the sketch in issue #57

The sketch in the issue was only an outline. This contract differs in these points:

- The gateway runs the session on its own (subscriptions, reading every port), as the app does today; Home Assistant mostly listens.
- The state of every port goes to a retained `<base>/state/<product>/<port>` with the session number; change indications go to `<base>/ind/<product>/<port>`. Both use the real port name.
- Requests are only `sensor_stream`, `get_props` and `put_props`; the answer carries the result in `data` and echoes `id` and `client`.
- Whether the brush is connected is in `<base>/link` (with `session` and `snapshot`), not in `<base>/status`; `status` is only gateway availability.
- `<base>` has a fixed form: `sonicare_gateway/<id>`.
- Binary ports are sent as hex (`{"hex": "…"}`).

## Tools

- `scripts/check_mqtt_gateway.py` plays the Home Assistant side and checks a gateway against this document (needs Python 3 and `paho-mqtt`): `python3 check_mqtt_gateway.py --broker <host> --address <brush address>`. Use the brush during the listen phase to check change indications.
- `scripts/condor_mqtt_gateway.py` is a reference gateway (Python, bleak + paho-mqtt) that implements the gateway side.

## Open points

- Classic (non-Condor) handles are not covered.
- To confirm on the HX7410: does it use a resolvable private address after bonding, and can the gateway read its identity address?
