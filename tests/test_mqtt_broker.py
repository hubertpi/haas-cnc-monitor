"""Real MQTT TCP protocol tests against a loopback-only AMQTT broker.

This is a real broker and simulated machine source, not Haas hardware or HA.
"""
import asyncio
import json
import queue
import socket
import threading
import time
import paho.mqtt.client as mqtt
from amqtt.broker import Broker
from app.mqtt import Publisher, key


class TestBroker:
    __test__ = False
    def __init__(self, port):
        self.port = port
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        def run():
            asyncio.set_event_loop(self.loop)
            async def start():
                self.broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
                                      "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
                await self.broker.start()
                self.ready.set()
            self.loop.run_until_complete(start())
            self.loop.run_forever()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert self.ready.wait(10), "Test broker did not start"

    def close(self):
        if self.loop.is_closed():
            return
        async def shutdown():
            # Model a broker outage by closing listeners and accepted transports.
            # This also avoids Python 3.12 Server.wait_closed waiting on clients.
            for server in self.broker._servers.values():
                if server.instance: server.instance.close()
            for session,handler in list(self.broker._sessions.values()):
                if handler.writer:
                    handler.writer._writer.transport.abort()
            await asyncio.sleep(0)
            try:
                await asyncio.wait_for(self.broker.shutdown(),3)
            except (asyncio.CancelledError,asyncio.TimeoutError):
                pass
            remaining = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in remaining:
                task.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


def wait(pub, condition, limit=10):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        pub.pump()
        if condition(): return
        time.sleep(.03)
    assert condition(), "MQTT condition timed out"


def test_real_broker_retention_birth_outage_deletion_and_restart(tmp_path):
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    broker = TestBroker(port)
    pub = Publisher(tmp_path, {"host": "127.0.0.1", "port": port})
    received = queue.Queue()
    observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    observer.on_connect = lambda client, *args: client.subscribe("#", qos=1)
    observer.on_message = lambda c, u, m: received.put((m.topic, m.payload))
    observer.connect("127.0.0.1", port); observer.loop_start(); pub.start()
    machine = {"id": "fixture-uuid", "name": "Test lathe", "interval": 1}
    signals = {"rpm": {"value": 1200, "source": "cnc_rdspeed", "unit": "rpm"}}
    try:
        pub.discovery(machine, signals, {"model": "SIM"})
        pub.state(machine["id"], "rpm", signals["rpm"], "fixture-time", True)
        pub.availability(machine["id"], True)
        wait(pub, lambda: pub.connected and not pub.dirty)
        disco = pub.discovery_topic(machine["id"])
        packets = []
        while not received.empty(): packets.append(received.get())
        assert any(t == disco and json.loads(p)["device"]["identifiers"] == [machine["id"]] for t,p in packets)
        observer.publish("homeassistant/status", "online", qos=1).wait_for_publish(5)
        deadline = time.monotonic() + 5
        while pub.events.empty() and time.monotonic() < deadline:
            time.sleep(.02)
        assert not pub.events.empty()
        pub.pump(); assert disco in pub.dirty or disco in pub.inflight
        wait(pub, lambda: not pub.dirty)
        broker.close(); wait(pub, lambda: not pub.connected)
        pub.remove(machine["id"])
        assert pub.journal["topics"][disco] == ""
        observer.disconnect(); observer.loop_stop()
        broker = TestBroker(port)
        wait(pub, lambda: pub.connected and not pub.dirty, 15)
        # A new subscriber must get no retained discovery after deletion.
        final = queue.Queue()
        checker = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        checker.on_connect = lambda client, *args: client.subscribe("homeassistant/#", qos=1)
        checker.on_message = lambda c,u,m: final.put(m.topic)
        checker.connect("127.0.0.1",port); checker.loop_start(); time.sleep(.3)
        assert final.empty()
        checker.disconnect(); checker.loop_stop()
        restored = Publisher(tmp_path)
        restored.reconcile(set())
        assert restored.journal["installation"] == pub.journal["installation"]
        assert restored.journal["topics"][disco] == ""
    finally:
        pub.close()
        observer.disconnect(); observer.loop_stop()
        broker.close()
