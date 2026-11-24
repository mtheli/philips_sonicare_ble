#!/usr/bin/env python3
"""Check an MQTT gateway against docs/MQTT_GATEWAY.md (protocol version 1).

Plays the Home Assistant side: reads status / info / link, sends requests on
<base>/cmd and checks the answers, subscribes the usual ports and checks the
change indications that arrive while you use the brush.

    pip install paho-mqtt
    python3 check_mqtt_gateway.py --broker 192.168.1.10 --address AA:BB:CC:DD:EE:FF \
        [--username u --password p] [--listen 60]

Switch the brush on (or start brushing) during the listen phase to see
change indications. Exit code 0 when every check passed.
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

SUBSCRIBE_PORTS = ("Sonicare", "RoutineStatus", "Battery", "BrushHead", "SessionStorage")
ANSWER_TIMEOUT = 10.0

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
        self.unsubscribe = args.unsubscribe
        self.retained: dict[str, str] = {}
        self.events: dict[str, threading.Event] = {
            k: threading.Event() for k in ("status", "info", "link")
        }
        self.answers: dict[int, dict] = {}
        self.answer_cv = threading.Condition()
        self.inds: queue.Queue = queue.Queue()
        self.link_log: list[dict] = []
        # Far away from the ids Home Assistant uses, in case it is connected too.
        self.next_id = 900_000
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
        if topic in self.events:
            self.retained[topic] = payload
            if topic == "link":
                try:
                    self.link_log.append(json.loads(payload))
                except ValueError:
                    self.link_log.append({"_invalid": payload})
            self.events[topic].set()
        elif topic == "resp":
            try:
                body = json.loads(payload)
            except ValueError:
                check(False, f"resp is not JSON: {payload!r}")
                return
            with self.answer_cv:
                self.answers[body.get("id")] = body
                self.answer_cv.notify_all()
        elif topic.startswith("ind/"):
            self.inds.put((topic[4:], payload, msg.qos, msg.retain))

    def request(self, **req) -> dict | None:
        self.next_id += 1
        rid = self.next_id
        self.client.publish(f"{self.base}/cmd", json.dumps({"id": rid, **req}), qos=1)
        deadline = time.monotonic() + ANSWER_TIMEOUT
        with self.answer_cv:
            while rid not in self.answers:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.answer_cv.wait(left)
            return self.answers.pop(rid)

    # --- Checks ------------------------------------------------------------

    def check_status_info(self) -> bool:
        print(f"\n== Retained topics under {self.base}")
        ok = True
        if not self.events["status"].wait(5):
            return check(False, "status: nothing retained (gateway not connected to the broker?)")
        status = self.retained["status"]
        ok &= check(status in ("online", "offline"), f"status is 'online'/'offline': {status!r}")
        ok &= check(status == "online", "gateway is online")

        if not self.events["info"].wait(5):
            ok &= check(False, "info: nothing retained")
        else:
            try:
                info = json.loads(self.retained["info"])
            except ValueError:
                info = None
            if check(isinstance(info, dict), "info is a JSON object"):
                ok &= check(info.get("protocol") == 1, f"info.protocol == 1: {info.get('protocol')!r}")
                addr = str(info.get("address", ""))
                ok &= check(
                    bool(re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", addr)),
                    f"info.address is AA:BB:CC:DD:EE:FF form: {addr!r}",
                )
                ok &= check(addr == self.address, "info.address matches the topic")
                for key in ("gateway", "model", "firmware"):
                    if key in info:
                        note(f"info.{key} = {info[key]!r}")
                    else:
                        note(f"info.{key} not set (optional, but Home Assistant shows it)")
            else:
                ok = False

        if not self.events["link"].wait(5):
            return check(False, "link: nothing retained") and False
        try:
            link = json.loads(self.retained["link"])
        except ValueError:
            link = None
        if check(isinstance(link, dict), "link is a JSON object"):
            ok &= check(isinstance(link.get("connected"), bool), f"link.connected is true/false: {link.get('connected')!r}")
            ok &= check(isinstance(link.get("session"), int), f"link.session is an integer: {link.get('session')!r}")
        else:
            ok = False
        return ok

    def wait_connected(self, wait: float) -> bool:
        print("\n== Brush connection")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                link = json.loads(self.retained.get("link", "{}"))
            except ValueError:
                link = {}
            if link.get("connected") is True:
                return check(True, f"brush connected (session {link.get('session')})")
            time.sleep(0.5)
        return check(False, f"brush not connected within {wait:.0f} s — switch it on and run again")

    def check_errors(self) -> None:
        print("\n== Error answers")
        r = self.request(op="no_such_op")
        check(r is not None and r.get("error") == "bad_request", f"unknown op → bad_request: {r}")
        r = self.request(op="get_props", product="1")
        check(r is not None and r.get("error") == "bad_request", f"get_props without port → bad_request: {r}")

    def check_reads(self) -> list[str]:
        print("\n== Reads")
        r = self.request(op="get_prods")
        if not check(r is not None, "get_prods answered"):
            return []
        check("error" not in r, f"get_prods without error: {r.get('error')}")
        check(r.get("status") == 0, f"get_prods status 0: {r.get('status')!r}")
        prods = r.get("data")
        check(isinstance(prods, dict) and "1" in prods, f"get_prods data is an object with product '1': {prods}")

        r = self.request(op="get_ports", product="1")
        if not check(r is not None and r.get("status") == 0, f"get_ports(1) status 0: {r}"):
            return []
        ports = r.get("data")
        if not check(isinstance(ports, list) and all(isinstance(p, str) for p in ports), "get_ports data is a list of names"):
            return []
        note(f"ports: {', '.join(ports)}")
        for port in ports:
            if port.endswith(".b"):
                continue
            r = self.request(op="get_props", product="1", port=port)
            good = r is not None and r.get("status") == 0 and isinstance(r.get("data"), (dict, type(None)))
            check(good, f"get_props 1/{port}: status {None if r is None else r.get('status')}, "
                        f"{'object' if r and isinstance(r.get('data'), dict) else (None if r is None else r.get('data'))}")
        r = self.request(op="get_props", product="1", port="NoSuchPortHere")
        check(r is not None and "error" not in r and r.get("status") not in (0, None),
              f"unknown port → device status passed through (not 0): {r}")
        return ports

    def check_subscriptions(self, ports: list[str], listen: float) -> None:
        print("\n== Subscriptions and change indications")
        subscribed = []
        for port in SUBSCRIBE_PORTS:
            if ports and port not in ports:
                continue
            r = self.request(op="subscribe", product="1", port=port)
            if check(r is not None and r.get("status") == 0, f"subscribe 1/{port}: {r}"):
                subscribed.append(port)
        print(f"\n   Listening {listen:.0f} s — use the brush now (switch on, brush, put back) ...")
        deadline = time.monotonic() + listen
        seen: dict[str, int] = {}
        while time.monotonic() < deadline:
            try:
                topic, payload, qos, retain = self.inds.get(timeout=0.5)
            except queue.Empty:
                continue
            seen[topic] = seen.get(topic, 0) + 1
            first = seen[topic] == 1
            product, _, port = topic.partition("/")
            try:
                body = json.loads(payload)
            except ValueError:
                body = None
            if first:
                check(not retain, f"ind/{topic} not retained")
                check(product.isdigit() and bool(port), f"ind/{topic}: topic is ind/<product>/<port>")
                if port.endswith(".b"):
                    good = isinstance(body, dict) and isinstance(body.get("hex"), str) \
                        and bool(re.fullmatch(r"([0-9a-f]{2})*", body["hex"]))
                    check(good, f"ind/{topic}: binary port as {{\"hex\": lower-case hex}}: {payload[:80]}")
                else:
                    check(isinstance(body, dict), f"ind/{topic}: JSON object: {payload[:80]}")
        if seen:
            note("indications: " + ", ".join(f"{t} ×{n}" for t, n in sorted(seen.items())))
        else:
            note("no change indications arrived (brush not used during the listen phase?)")
        if not self.unsubscribe:
            note("unsubscribe not tested (--unsubscribe); it would also end Home "
                 "Assistant's subscriptions if it is connected to this gateway")
            return
        for port in subscribed:
            r = self.request(op="unsubscribe", product="1", port=port)
            check(r is not None and "error" not in r, f"unsubscribe 1/{port}: {r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--broker", required=True)
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--address", required=True, help="brush identity address AA:BB:CC:DD:EE:FF")
    ap.add_argument("--wait", type=float, default=60, help="seconds to wait for the brush to connect")
    ap.add_argument("--listen", type=float, default=60, help="seconds to collect change indications")
    ap.add_argument("--unsubscribe", action="store_true",
                    help="also test unsubscribe (ends Home Assistant's live updates too)")
    args = ap.parse_args()

    c = Checker(args)
    try:
        if c.check_status_info() is not False and c.wait_connected(args.wait):
            c.check_errors()
            ports = c.check_reads()
            c.check_subscriptions(ports, args.listen)
    finally:
        c.client.loop_stop()
        c.client.disconnect()

    failed = [t for ok, t in results if not ok]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
