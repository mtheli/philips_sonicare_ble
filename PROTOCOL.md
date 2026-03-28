# Philips Sonicare BLE Protocol

This document describes the Bluetooth Low Energy (BLE) GATT protocol used by Philips Sonicare toothbrushes. Documented through BLE analysis and verified against a real HX992X (DiamondClean 9000).

## Protocol Variants

Philips Sonicare devices expose two BLE service stacks. Most current models support both simultaneously:

| Protocol | Primary Service UUID | Method |
| :--- | :--- | :--- |
| **GATT** (this document) | `477ea600-a260-11e4-ae37-0002a5d50001` | Direct GATT characteristics |
| **ByteStreaming** | `e50ba3c0-af04-4564-92ad-fef019489de6` | Binary streaming channel (not yet documented) |

This integration uses the **GATT** protocol, which is supported by all known BLE-enabled Sonicare models.

---

## Discovery

| Property | Value |
| :--- | :--- |
| **Manufacturer ID** | 477 (decimal) |
| **Local Name** | `Philips Sonicare` or `Philips OHC` |
| **Primary Service UUID** | `477ea600-a260-11e4-ae37-0002a5d50001` |
| **iBeacon** | Yes (Apple Manufacturer ID 76, prefix `0215`, Sonicare UUID as beacon UUID) |
| **Pairing** | None required (open GATT, `BondInitiator.NONE`) |
| **Advertisement Interval** | ~10-30 seconds |
| **Advertisement on charger** | Reduced (primary service UUID only) |
| **Advertisement active/standby** | Full (all service UUIDs) |

---

## UUID Schema

All Philips custom UUIDs follow the pattern:

```
477ea600-a260-11e4-ae37-0002a5d5XXXX
```

Where `XXXX` is the characteristic or service short ID. Standard BLE characteristics use the Bluetooth SIG base UUID.

---

## Services

| Service | UUID Suffix | Full UUID |
| :--- | :--- | :--- |
| **Sonicare** | `0001` | `477ea600-a260-11e4-ae37-0002a5d50001` |
| **Routine** | `0002` | `477ea600-a260-11e4-ae37-0002a5d50002` |
| **Storage** | `0004` | `477ea600-a260-11e4-ae37-0002a5d50004` |
| **Sensor** | `0005` | `477ea600-a260-11e4-ae37-0002a5d50005` |
| **Brush Head** | `0006` | `477ea600-a260-11e4-ae37-0002a5d50006` |
| **Diagnostic** | `0007` | `477ea600-a260-11e4-ae37-0002a5d50007` |
| **Extended** | `0008` | `477ea600-a260-11e4-ae37-0002a5d50008` |
| **ByteStreaming** | — | `a651fff1-4074-4131-bce9-56d4261bc7b1` |
| Battery | — | `0000180f-0000-1000-8000-00805f9b34fb` |
| Device Information | — | `0000180a-0000-1000-8000-00805f9b34fb` |
| GATT | — | `00001801-0000-1000-8000-00805f9b34fb` |

---

## Characteristics

### Battery Service (0x180F)

| Short ID | Properties | Format | Description |
| :--- | :--- | :--- | :--- |
| `0x2A19` | Read, Notify | uint8 | Battery level (0-100%) |

### Device Information Service (0x180A)

| Short ID | Properties | Format | Description |
| :--- | :--- | :--- | :--- |
| `0x2A24` | Read | UTF-8 string | Model number (e.g., "HX992X") |
| `0x2A25` | Read | UTF-8 string | Serial number |
| `0x2A26` | Read | UTF-8 string | Firmware revision (e.g., "2.15.1 0.6.1.0") |
| `0x2A27` | Read | UTF-8 string | Hardware revision |
| `0x2A28` | Read | UTF-8 string | Software revision |
| `0x2A29` | Read | UTF-8 string | Manufacturer name ("Philips OHC") |
| `0x2A23` | Read | 8 bytes | System ID (BLE MAC) |
| `0x2A2A` | Read | bytes | IEEE 11073-20601 regulatory data |
| `0x2A50` | Read | 7 bytes | PnP ID (vendor/product/version) |

### Sonicare Service (0x0001)

| Short ID | Properties | Format | Description |
| :--- | :--- | :--- | :--- |
| `0x4010` | Read, Write, Indicate | uint8 | **Handle State** — see enum below |
| `0x4020` | Read | uint16 LE | Available brushing routines (bitmask) |
| `0x4022` | Read | bytes | Available routine IDs (e.g., `00 01 02 04`) |
| `0x4030` | Read, Notify | uint16 LE | Unknown |
| `0x4040` | Read | uint32 LE | Cumulative motor runtime (seconds) |
| `0x4050` | Read, Write | uint32 LE | Handle time (seconds since epoch) |

#### Handle State Enum (0x4010)

| Value | Name | Description |
| :--- | :--- | :--- |
| 0 | Off | Powered off |
| 1 | Standby | Awake, not brushing |
| 2 | Run | Actively brushing |
| 3 | Charge | On charging stand |
| 4 | Shutdown | Shutting down |
| 6 | Validate | Validation mode |
| 7 | Background | Background processing |

### Routine Service (0x0002)

| Short ID | Properties | Format | Description |
| :--- | :--- | :--- | :--- |
| `0x4070` | Read, Notify | uint16 LE | Current session ID |
| `0x4080` | Read, Indicate | uint8 | **Brushing Mode** — see enum below |
| `0x4082` | Read, Write, Notify | uint8 | **Brushing State** — see enum below |
| `0x4090` | Read, Notify | uint16 LE | Brushing time (seconds, live counter) |
| `0x4091` | Read, Notify | uint16 LE | Routine length (seconds, typically 120) |
| `0x40A0` | Read, Notify | uint8 | Unknown |
| `0x40B0` | Read, Notify | uint8 | **Intensity** — see enum below |
| `0x40C0` | Read, Notify | uint8 | Unknown |

#### Brushing Mode Enum (0x4080)

| Value | Name |
| :--- | :--- |
| 0 | Clean |
| 1 | White+ |
| 2 | Gum Health |
| 3 | Deep Clean+ |
| 4 | Sensitive |
| 5 | Tongue Care |

#### Brushing State Enum (0x4082)

*Not documented in any existing open-source project. Discovered through live testing.*

| Value | Name | Trigger |
| :--- | :--- | :--- |
| 0 | Off | Brush idle |
| 1 | On | Brush motor running |
| 2 | Pause | Brush manually stopped (motor off, session active) |
| 3 | Session Complete | Brushing time reached routine length (e.g., 120s) |
| 4 | Session Aborted | First mode change after manually ending a running session |

#### Intensity Enum (0x40B0)

| Value | Name |
| :--- | :--- |
| 0 | Low |
| 1 | Medium |
| 2 | High |

### Storage Service (0x0004)

| Short ID | Properties | Format | Description |
| :--- | :--- | :--- | :--- |
| `0x40D0` | Read, Notify | uint16 LE | Latest session ID |
| `0x40D2` | Read, Notify | uint16 LE | Session count (stored sessions) |
| `0x40D5` | Read, Write | uint8 | Session type |
| `0x40E0` | Write, Notify | uint16 LE | Active session ID |
| `0x4100` | Notify | bytes | Session data stream |
| `0x4110` | Write, Notify | uint8 | Session action (0=stop, 1=new, 2=getAction) |

### Sensor Service (0x0005)

| Short ID | Properties | Format | Description |
| :--- | :--- | :--- | :--- |
| `0x4120` | Read, Write | uint16 LE | **Sensor enable bitmask** — see below |
| `0x4130` | Notify | bytes | **Sensor data stream** — see format below |
| `0x4140` | Write, Notify | bytes | Unknown |

#### Sensor Enable Bitmask (0x4120)

Write a uint16 LE value to enable/disable sensors:

| Bit | Value | Sensor |
| :--- | :--- | :--- |
| 0 | 1 | Pressure |
| 1 | 2 | Temperature |
| 2 | 4 | Gyroscope/Accelerometer |

Example: Write `0x07 0x00` to enable all three sensors.

#### Sensor Data Frame Format (0x4130)

```
Bytes 0-1:  Frame type (uint16 LE) — see SensorFrameType
Bytes 2-3:  Counter (uint16 LE) — frame sequence number
Bytes 4+:   Payload (depends on frame type)
```

| Frame Type | Value | Payload |
| :--- | :--- | :--- |
| **Pressure** | 1 | `[4-5]` pressure (int16 LE), `[6]` alarm flag |
| **Temperature** | 2 | `[4]` fractional (value/256), `[5]` integer degrees |
| **Gyroscope** | 4 | `[4-5]` accX, `[6-7]` accY, `[8-9]` accZ, `[10-11]` gyroX, `[12-13]` gyroY, `[14-15]` gyroZ (all int16 LE) |

### Brush Head Service (0x0006)

| Short ID | Properties | Format | Description |
| :--- | :--- | :--- | :--- |
| `0x4210` | Read | bytes | NFC tag version |
| `0x4220` | Read | uint8 | Brush head type (0=unknown, 1=adaptive_clean, 2=adaptive_white, 3=tongue_care, 4=adaptive_gums, 5=sensitive) |
| `0x4230` | Read, Notify | UTF-8 string | Serial number |
| `0x4240` | Read | UTF-8 string | Manufacturing date (e.g., "241211 72M") |
| `0x4250` | Read | uint8 | Unknown |
| `0x4254` | Read | uint8 | Unknown |
| `0x4260` | Read | uint8 | Unknown |
| `0x4270` | Read | uint8 | Unknown |
| `0x4280` | Read | uint16 LE | **Lifetime limit** (max usage counter) |
| `0x4290` | Read | uint16 LE | **Lifetime usage** (current usage counter) |
| `0x42A0` | Read | uint8 | Unknown |
| `0x42A2` | Read | uint8 | Unknown |
| `0x42A4` | Read | bytes | Unknown |
| `0x42A6` | Read, Write, Notify | uint8 | Unknown |
| `0x42B0` | Read | UTF-8 string | NFC payload URL (e.g., `https://www.philips.com/nfcbrushheadtap`) |
| `0x42C0` | Read | uint16 LE | Color ring ID (for family brush head tracking) |

Brush head wear percentage is computed as: `(usage / limit) * 100`

### Diagnostic Service (0x0007)

| Short ID | Properties | Format | Description |
| :--- | :--- | :--- | :--- |
| `0x4310` | Read | uint32 LE | Persistent error code |
| `0x4320` | Read | uint32 LE | Volatile error code |
| `0x4330` | Read | bytes | Unknown (10x uint16 LE values, possibly calibration data) |
| `0x4360` | Read | uint32 LE | Unknown |

### Extended Service (0x0008)

| Short ID | Properties | Format | Description |
| :--- | :--- | :--- | :--- |
| `0x4410` | Read, Write | bytes | Unknown |
| `0x4420` | Read, Write | uint32 LE | Settings |

### ByteStreaming Service

Used for firmware updates and extended data transfer.

| UUID | Properties | Description |
| :--- | :--- | :--- |
| `a6510001-...-56d4261bc7b1` | Write (no response) | RX (app to brush) |
| `a6510002-...-56d4261bc7b1` | Notify | RX ACK |
| `a6510003-...-56d4261bc7b1` | Notify | TX (brush to app) |
| `a6510004-...-56d4261bc7b1` | Write (no response) | TX ACK |
| `a6510005-...-56d4261bc7b1` | Read | Protocol configuration |

---

## Connection Behavior

### Advertisement Pattern

- **Interval:** ~10-30 seconds (very slow compared to typical BLE devices)
- **On charger:** Reduced advertisement with primary service UUID only (`...0001`)
- **Active/standby:** Full advertisement with all service UUIDs
- **Deep sleep:** No advertisement (device unreachable)
- **Wake triggers:** Placed on charger, button press, brush head insertion

### Connection Timing

- The toothbrush stays connectable for ~20 seconds after the last interaction
- Active BLE notification subscriptions keep the connection alive indefinitely
- Without subscriptions, the connection drops after ~6 seconds
- The app uses `connectGatt(context, autoConnect=false, callback, TRANSPORT_LE)`
- Default connection priority is `BALANCED` (0)

### Notification Characteristics

The following characteristics support notifications/indications and should be subscribed immediately after connecting to maintain the connection:

```
0x4010  Handle State (indicate)
0x4082  Brushing State (notify)
0x4080  Brushing Mode (indicate)
0x4090  Brushing Time (notify)
0x4091  Routine Length (notify)
0x40B0  Intensity (notify)
0x4070  Session ID (notify)
0x40D0  Latest Session ID (notify)
0x40D2  Session Count (notify)
0x4030  Unknown (notify)
0x40A0  Unknown (notify)
0x40C0  Unknown (notify)
0x4230  Brush Head Serial (notify)
```

---

## Position Detection (Watson)

The Sonicare app includes a proprietary native library `libwatsonWrapper.so` for brushing position detection using IMU sensor data. This is **not** part of the BLE protocol — it runs entirely on the phone.

- **Input:** Accelerometer + Gyroscope data from characteristic `0x4130`
- **Output:** Mouth sextant (1-6) with sub-segments (inner/outer)
- **Requires:** Per-user calibration (stored in app SharedPreferences)
- **Algorithm:** Sensor fusion (likely Madgwick filter) + position classification
- **Configuration:** deltaTime=0.0315s (~31.7 Hz), kAcc=0.002394, kGyro=0.000583

---

## References

- [python-sonicare](https://github.com/joushx/python-sonicare) — Python BLE library for Sonicare
- [sonicare-ble-hacs](https://github.com/GrumpyMeow/sonicare-ble-hacs) — Earlier HA integration (unmaintained)
- [My toothbrush streams gyroscope data](https://blog.johannes-mittendorfer.com/artikel/2020/10/my-toothbrush-streams-gyroscope-data) — Blog post on Sonicare BLE gyroscope streaming
- [ROBAS-UCLA/Toothbrushing-region-detection](https://github.com/ROBAS-UCLA/Toothbrushing-region-detection) — Academic brushing region detection using IMU
