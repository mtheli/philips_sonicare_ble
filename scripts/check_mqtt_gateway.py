#!/usr/bin/env python3
"""Check an MQTT gateway against docs/MQTT_GATEWAY.md (protocol version 1).

Plays the Home Assistant side: checks the retained status / info / link
topics and the snapshot in state/<product>/<port>, sends a few requests on
<base>/cmd and checks the answers, then watches the change indications and
state updates that arrive while you use the brush.

    pip install paho-mqtt
    python3 check_mqtt_gateway.py --broker 192.168.1.10 --address AA:BB:CC:DD:EE:FF \
        [--username u --password p] [--listen 60] [--sensor]

Switch the brush on before you start (the check waits for it), and use it
during the listen phase to see change indications. --sensor also switches
the pressure/temperature stream on and off once. Exit code 0 when every
check passed.
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import sys
import threading
import time

import paho.mqtt.client as mqtt

ANSWER_TIMEOUT = 10.0
CLIENT = f"check-{int(time.time())}"

results: list[tuple[bool, str]] = []


def check(ok: bool, text: str) -> bool:
    results.append((ok, text))
    print(("PASS  " if ok else "FAIL  ") + text)
    return ok


def note(text: str) -> None:
    print("      " + text)


class Checker:
    def __init__(self, args) -> None:
        self.base = "sonicare_gateway/" + args.address.replace(":", "").lower()
        self.address = args.address.upper()
        self.lock = threading.Condition()
        self.retained: dict[str, str] = {}
        self.state: dict[str, dict] = {}
        self.state_updates: list[tuple[str, dict]] = []
        # Indications and state updates in arrival order: ("ind"|"state", port, body)
        self.events: list[tuple[str, str, dict]] = []
        self.answers: dict[int, list[dict]] = {}
        self.inds: queue.Queue = queue.Queue()
        self.next_id = 0
        client_id = f"gateway-check-{int(time.time())}"
        if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt 2.x
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        else:
            self.client = mqtt.Client(client_id=client_id)
        if args.username:
            self.client.username_pw_set(args.username, args.password)
        self.client.on_message = self.on_message
        self.client.connect(args.broker, args.port, keepalive=30)
        self.client.loop_start()
        self.client.subscribe(f"{self.base}/#", qos=1)

    def on_message(self, _c, _u, msg) -> None:
        topic = msg.topic[len(self.base) + 1:]
        payload = msg.payload.decode("utf-8", errors="replace")
        with self.lock:
            if topic in ("status", "info", "link"):
                self.retained[topic] = payload
            elif topic.startswith("state/"):
                key = topic[6:]
                if not payload:
                    self.state.pop(key, None)
                else:
                    try:
                        body = json.loads(payload)
                    except ValueError:
                        body = {"_invalid": payload}
                    self.state[key] = body
                    self.state_updates.append((key, body))
                    self.events.append(("state", key, body))
            elif topic == "resp":
                try:
                    body = json.loads(payload)
                except ValueError:
                    body = {"_invalid": payload}
                if body.get("client") == CLIENT:
                    self.answers.setdefault(body.get("id"), []).append(body)
            elif topic.startswith("ind/"):
                self.inds.put((topic[4:], payload, msg.qos, msg.retain))
                try:
                    self.events.append(("ind", topic[4:], json.loads(payload)))
                except ValueError:
                    pass
            self.lock.notify_all()

    def wait_for(self, cond, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self.lock:
            while not cond():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.lock.wait(left)
            return True

    def json_of(self, topic: str):
        try:
            return json.loads(self.retained.get(topic, ""))
        except ValueError:
            return None

    def request(self, rid: int | None = None, **req) -> dict | None:
        if rid is None:
            self.next_id += 1
            rid = self.next_id
        self.client.publish(
            f"{self.base}/cmd", json.dumps({"id": rid, "client": CLIENT, **req}), qos=1,
        )
        if not self.wait_for(lambda: rid in self.answers, ANSWER_TIMEOUT):
            return None
        return self.answers[rid][-1]

    # --- Checks ------------------------------------------------------------

    def check_retained(self) -> bool:
        print(f"\n== Retained topics under {self.base}")
        if not self.wait_for(lambda: "status" in self.retained, 5):
            return check(False, "status: nothing retained (gateway not connected to the broker?)")
        status = self.retained["status"]
        check(status in ("online", "offline"), f"status is 'online'/'offline': {status!r}")
        ok = check(status == "online", "gateway is online")

        if not self.wait_for(lambda: "info" in self.retained, 5):
            check(False, "info: nothing retained")
        else:
            info = self.json_of("info")
            if check(isinstance(info, dict), "info is a JSON object"):
                check(info.get("protocol") == 1, f"info.protocol == 1: {info.get('protocol')!r}")
                addr = str(info.get("address", ""))
                check(bool(re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", addr)),
                      f"info.address is AA:BB:CC:DD:EE:FF form: {addr!r}")
                check(addr == self.address, "info.address matches the topic")
                for key in ("gateway", "model", "firmware"):
                    note(f"info.{key} = {info[key]!r}" if key in info
                         else f"info.{key} not set (optional, but Home Assistant shows it)")

        if not self.wait_for(lambda: "link" in self.retained, 5):
            return check(False, "link: nothing retained") and False
        link = self.json_of("link")
        if not check(isinstance(link, dict), "link is a JSON object"):
            return False
        check(isinstance(link.get("connected"), bool), f"link.connected is true/false: {link.get('connected')!r}")
        check(isinstance(link.get("session"), int), f"link.session is an integer: {link.get('session')!r}")
        check(isinstance(link.get("snapshot"), bool), f"link.snapshot is true/false: {link.get('snapshot')!r}")
        check(isinstance(link.get("sensor_mask"), int), f"link.sensor_mask is an integer: {link.get('sensor_mask')!r}")
        return ok

    def wait_snapshot(self, wait: float) -> dict | None:
        print("\n== Brush connection and snapshot")

        def ready() -> bool:
            link = self.json_of("link") or {}
            return link.get("connected") is True and link.get("snapshot") is True

        if not self.wait_for(ready, wait):
            link = self.json_of("link") or {}
            if link.get("connected"):
                check(False, f"snapshot not complete within {wait:.0f} s (link.snapshot stays false)")
            else:
                check(False, f"brush not connected within {wait:.0f} s — switch it on and run again")
            return None
        link = self.json_of("link")
        check(True, f"brush connected, session {link['session']}, snapshot complete")
        ports = link.get("ports")
        check(isinstance(ports, list) and all(isinstance(p, str) and re.fullmatch(r"\d+/[^/]+", p) for p in ports),
              f"link.ports lists <product>/<port>: {ports}")
        return link

    def check_state(self, link: dict) -> None:
        print("\n== State topics")
        session, ports = link["session"], link.get("ports") or []
        time.sleep(1)  # let the retained state messages arrive
        with self.lock:
            state = dict(self.state)
        for port in ports:
            body = state.get(port)
            if not check(body is not None, f"state/{port} retained"):
                continue
            check(isinstance(body.get("props"), dict) and bool(body["props"]),
                  f"state/{port}: props is a non-empty object")
            check(body.get("session") == session, f"state/{port}: session {body.get('session')} == {session}")
            check(not port.endswith(".b"), f"state/{port}: not a binary port")
        for want in ("1/Sonicare", "1/RoutineStatus", "1/Battery"):
            check(want in ports, f"snapshot contains {want}")
        stale = [p for p in state if p not in ports]
        check(not stale, f"no retained state outside the snapshot: {stale or 'none'}")

    def check_requests(self) -> None:
        print("\n== Requests")
        r = self.request(op="no_such_op")
        check(r is not None and r.get("error") == "bad_request", f"unknown op → bad_request: {r}")
        r = self.request(op="get_props", product="1")
        check(r is not None and r.get("error") == "bad_request", f"get_props without port → bad_request: {r}")
        r = self.request(op="sensor_stream")
        check(r is not None and r.get("error") == "bad_request", f"sensor_stream without mask → bad_request: {r}")
        r = self.request(op="get_props", product="1", port="Battery")
        check(r is not None and r.get("status") == 0 and isinstance(r.get("data"), dict),
              f"get_props 1/Battery → status 0 with object: {r}")
        check(r is not None and r.get("client") == CLIENT, "answer echoes client")
        r = self.request(op="get_props", product="1", port="NoSuchPortHere")
        check(r is not None and "error" not in r and r.get("status") not in (0, None),
              f"unknown port → device status passed through (not 0): {r}")
        # The same request twice (as QoS 1 may deliver it): answered, same answer.
        self.next_id += 1
        rid = self.next_id
        first = self.request(rid, op="get_props", product="1", port="Battery")
        self.client.publish(f"{self.base}/cmd", json.dumps(
            {"id": rid, "client": CLIENT, "op": "get_props", "product": "1", "port": "Battery"}
        ), qos=1)
        got = self.wait_for(lambda: len(self.answers.get(rid, [])) >= 2, ANSWER_TIMEOUT)
        second = self.answers[rid][1] if got else None
        check(first is not None and second == first,
              f"a repeated id gets the same answer again: {second}")

    def check_sensor(self) -> None:
        print("\n== Sensor stream")
        r = self.request(op="sensor_stream", mask=1)
        check(r is not None and r.get("status") == 0, f"sensor_stream mask 1 → status 0: {r}")
        check(self.wait_for(lambda: (self.json_of("link") or {}).get("sensor_mask") == 1, 3),
              "link.sensor_mask becomes 1")
        r = self.request(op="sensor_stream", mask=1)
        check(r is not None and r.get("status") == 0, f"same mask again → status 0: {r}")
        r = self.request(op="sensor_stream", mask=0)
        check(r is not None and r.get("status") == 0, f"sensor_stream mask 0 → status 0: {r}")
        check(self.wait_for(lambda: (self.json_of("link") or {}).get("sensor_mask") == 0, 3),
              "link.sensor_mask back to 0")

    def check_indications(self, listen: float) -> None:
        print(f"\n== Change indications — use the brush now ({listen:.0f} s)")
        with self.lock:
            first_event = len(self.events)
        deadline = time.monotonic() + listen
        seen: dict[str, int] = {}
        while time.monotonic() < deadline:
            try:
                topic, payload, qos, retain = self.inds.get(timeout=0.5)
            except queue.Empty:
                continue
            seen[topic] = seen.get(topic, 0) + 1
            if seen[topic] > 1:
                continue
            try:
                body = json.loads(payload)
            except ValueError:
                body = None
            check(not retain, f"ind/{topic} not retained")
            if topic.endswith(".b"):
                good = isinstance(body, dict) and isinstance(body.get("hex"), str) \
                    and bool(re.fullmatch(r"([0-9a-f]{2})*", body["hex"]))
                check(good, f"ind/{topic}: binary port as {{\"hex\": lower-case hex}}: {payload[:80]}")
            else:
                check(isinstance(body, dict), f"ind/{topic}: JSON object: {payload[:80]}")
        if not seen:
            note("no change indications arrived (brush not used during the listen phase?)")
            return
        note("indications: " + ", ".join(f"{t} ×{n}" for t, n in sorted(seen.items())))
        with self.lock:
            last_ind = len(self.events)
        time.sleep(1)  # the state that follows the last indication
        with self.lock:
            events = self.events[first_event:]
            cut = last_ind - first_event
        missing: dict[str, int] = {}
        checked: dict[str, int] = {}
        for i, (kind, port, body) in enumerate(events[:cut]):
            if kind != "ind" or port.endswith(".b") or not isinstance(body, dict):
                continue
            if port not in self.state:
                continue  # not part of the snapshot
            checked[port] = checked.get(port, 0) + 1
            follows = any(
                k == "state" and p == port and all(
                    (b.get("props") or {}).get(key) == value for key, value in body.items()
                )
                for k, p, b in events[i + 1:]
            )
            if not follows:
                missing[port] = missing.get(port, 0) + 1
        for port, n in sorted(checked.items()):
            check(port not in missing,
                  f"state/{port} republished with each of its {n} indications"
                  + (f" ({missing[port]} missing)" if port in missing else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--broker", required=True)
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--address", required=True, help="brush identity address AA:BB:CC:DD:EE:FF")
    ap.add_argument("--wait", type=float, default=60, help="seconds to wait for brush and snapshot")
    ap.add_argument("--listen", type=float, default=60, help="seconds to collect change indications")
    ap.add_argument("--sensor", action="store_true",
                    help="also switch the sensor stream on and off once")
    args = ap.parse_args()

    c = Checker(args)
    try:
        if c.check_retained():
            link = c.wait_snapshot(args.wait)
            if link:
                c.check_state(link)
                c.check_requests()
                if args.sensor:
                    c.check_sensor()
                c.check_indications(args.listen)
    finally:
        c.client.loop_stop()
        c.client.disconnect()

    failed = [t for ok, t in results if not ok]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
