"""In-memory MQTT broker and reference-gateway helpers for the tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from condor_mqtt_gateway import CondorMqttGateway  # noqa: E402,F401


def _matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


class Broker:
    """Delivers asynchronously (next loop turn), keeps retained messages."""

    def __init__(self) -> None:
        self.subs: list[tuple[str, Any]] = []
        self.retained: dict[str, str] = {}
        self.log: list[tuple[str, str, int, bool]] = []

    def client(self) -> "Client":
        return Client(self)

    def deliver(self, topic: str, payload: str, retain: bool) -> None:
        if retain:
            self.retained[topic] = payload
        for pattern, cb in list(self.subs):
            if _matches(pattern, topic):
                asyncio.get_running_loop().call_soon(cb, topic, payload)


class Client:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self.will: tuple[str, str] | None = None

    async def publish(self, topic, payload, qos=1, retain=False) -> None:
        self.broker.log.append((topic, payload, qos, retain))
        self.broker.deliver(topic, payload, retain)

    async def subscribe(self, topic, callback, qos=1):
        entry = (topic, callback)
        self.broker.subs.append(entry)
        loop = asyncio.get_running_loop()
        for t, payload in self.broker.retained.items():
            if _matches(topic, t):
                loop.call_soon(callback, t, payload)

        def unsubscribe() -> None:
            if entry in self.broker.subs:
                self.broker.subs.remove(entry)

        return unsubscribe

    def die(self) -> None:
        """Connection lost: the broker publishes the last will."""
        if self.will:
            self.broker.deliver(*self.will, retain=True)


async def settle(rounds: int = 10) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)
