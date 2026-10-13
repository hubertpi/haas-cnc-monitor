"""Durable retained-topic journal, QoS 1 delivery and HA device discovery."""
import hashlib
import json
import queue
import ssl
import uuid
import time
from pathlib import Path
import paho.mqtt.client as mqtt
from .store import atomic_json


def key(identity):
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


class Publisher:
    def __init__(self, directory: Path, broker=None, client=None, deferred=False):
        self.deferred, self.needs_flush, self.last_flush = deferred, False, time.monotonic()
        self.on_control = None
        self.path = directory / "mqtt-journal.json"
        self.journal = json.loads(self.path.read_text()) if self.path.exists() else {
            "version": 1, "installation": str(uuid.uuid4()), "topics": {}}
        if self.journal.get("version") != 1:
            raise ValueError("Unsupported MQTT journal version")
        self.root = "haas_monitor/" + self.journal["installation"]
        self.online_topic = self.root + "/availability"
        self.valid_temperatures=set(self.journal.get("valid_temperatures",[]))
        # Migrate previously published positive temperature readings before
        # startup clears state. Zero-only legacy entities are not validated.
        for topic,payload in list(self.journal["topics"].items()):
            if topic.startswith("homeassistant/device/") and payload:
                config=json.loads(payload)
                identity=config.get("device",{}).get("identifiers",[None])[0]
                for component in config.get("components",{}).values():
                    state=self.journal["topics"].get(component.get("state_topic"),"")
                    if component.get("unit_of_measurement")=="°C" and identity and state:
                        value=json.loads(state).get("value")
                        if isinstance(value,(int,float)) and value>0:
                            self.valid_temperatures.add(identity+"/"+component["state_topic"].rsplit("/",1)[1])
        self.journal["valid_temperatures"]=sorted(self.valid_temperatures)
        self.needs_flush=True
        self.connected = False
        self.dirty = set(self.journal["topics"])
        self.inflight = {}
        self.events = queue.SimpleQueue()
        self.broker = broker or {}
        self.error = None
        self.client = client or mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                           client_id="haas-" + self.journal["installation"])
        self.client.will_set(self.online_topic, "offline", qos=1, retain=True)
        self.client.on_connect = self._connect
        self.client.on_disconnect = lambda *args: self.events.put("disconnect")
        self.client.on_message = self._message
        self.client.reconnect_delay_set(1, 60)
        self.client.max_inflight_messages_set(20)
        self.client.max_queued_messages_set(500)
        self.put(self.online_topic, "offline")
        self.flush()

    def _connect(self, client, userdata, flags, reason, properties):
        if reason == 0:
            client.subscribe("homeassistant/status", qos=1)
            client.subscribe(self.root + "/machine/+/monitor/set/+", qos=1)  # Local polling settings only.
            self.events.put("connect")
        else:
            self.events.put("connect_error")

    def _message(self, client, userdata, msg):
        if msg.topic == "homeassistant/status" and msg.payload == b"online":
            self.events.put("birth")
        elif not msg.retain and len(msg.payload) <= 32:
            self.events.put((msg.topic,msg.payload))

    def flush(self):
        if self.needs_flush:
            atomic_json(self.path,self.journal)
            self.needs_flush = False
            self.last_flush = time.monotonic()

    def start(self):
        if not self.broker.get("host"):
            self.error = "MQTT service unavailable; configure an explicit test or HA broker"
            return
        if self.broker.get("username"):
            self.client.username_pw_set(self.broker["username"], self.broker.get("password", ""))
        if self.broker.get("tls"):
            self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self.client.connect_async(self.broker["host"], self.broker.get("port", 1883), keepalive=20)
        self.client.loop_start()

    def put(self, topic, payload):
        value = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if self.journal["topics"].get(topic) != value:
            self.journal["topics"][topic] = value
            self.dirty.add(topic)
            self.needs_flush = True
            if not self.deferred:
                self.flush()

    def forget(self, topic):
        # Tombstones persist across broker outages and service restarts.
        self.put(topic, "")

    def pump(self):
        while not self.events.empty():
            event = self.events.get()
            if isinstance(event,tuple):
                topic,payload = event
                parts = topic.removeprefix(self.root+"/machine/").split("/")
                if len(parts)==4 and parts[1:3]==["monitor","set"] and self.on_control:
                    self.on_control(parts[0],parts[3],payload)
                continue
            if event in ("connect", "birth"):
                self.connected = True
                self.error = None
                self.inflight.clear()
                self.dirty.update(self.journal["topics"])
                self.put(self.online_topic, "online")
            else:
                self.connected = False
                self.error = "MQTT disconnected" if event == "disconnect" else "MQTT authentication/connection rejected"
                self.inflight.clear()
                self.dirty.update(self.journal["topics"])
        if time.monotonic()-self.last_flush >= 15:
            self.flush()
        for topic, (info, sent) in list(self.inflight.items()):
            if info.is_published():
                del self.inflight[topic]
                if self.journal["topics"].get(topic) == sent:
                    self.dirty.discard(topic)
        if self.connected:
            for topic in list(self.dirty):
                if topic not in self.inflight and len(self.inflight) < 20:
                    payload = self.journal["topics"][topic]
                    info = self.client.publish(topic, payload, qos=1, retain=True)
                    if info.rc == mqtt.MQTT_ERR_SUCCESS:
                        self.inflight[topic] = (info, payload)

    def acknowledged(self, topic):
        return self.connected and topic not in self.dirty and topic not in self.inflight

    def machine_root(self, identity):
        return self.root + "/machine/" + identity

    def temperature_validated(self,identity,name):
        return identity+"/"+key(name) in self.valid_temperatures

    def discovery_topic(self, identity):
        return "homeassistant/device/haas_" + self.journal["installation"] + "/" + identity + "/config"

    def availability(self, identity, online):
        self.put(self.machine_root(identity) + "/availability", "online" if online else "offline")

    def discovery(self, machine, signals, controller):
        identity = machine["id"]
        root = self.machine_root(identity)
        components = {}
        for name, signal in signals.items():
            slug = key(name)
            component = {
                "platform": signal.get("kind", "sensor"),
                "unique_id": identity + "_" + slug,
                "name": signal.get("label",name.replace("_", " ").title()),
                "state_topic": root + "/signal/" + slug,
                "value_template": "{{ value_json.value }}",
                "json_attributes_topic": root + "/signal/" + slug,
                "json_attributes_template": "{{ {'source': value_json.source, 'last_read': value_json.last_read, 'simulation': value_json.simulation, 'details': value_json.details | default(none)} | tojson }}",
                "availability": [{"topic": self.online_topic}, {"topic": root + "/availability"}],
                "availability_mode": "all",
                "expire_after": signal.get("expire_after",max(15, machine["interval"] * 3)),
            }
            if signal.get("group") == "utilization":
                component["availability"] = [{"topic":self.online_topic}]
            if "available" in signal:
                component["availability"].append({"topic":root+"/signal/"+slug,
                    "value_template":"{{ 'online' if value_json.available | default(true) else 'offline' }}"})
            if signal.get("unit")=="°C":
                component.update(device_class="temperature",state_class="measurement")
            if component["platform"] == "binary_sensor":
                component.update(payload_on="ON", payload_off="OFF")
            if signal.get("unit"):
                component["unit_of_measurement"] = signal["unit"]
            if isinstance(signal["value"], (int, float)) and not isinstance(signal["value"], bool) and signal.get("unit"):
                component["state_class"] = "measurement"
            if name.startswith("raw_") or name in ("collector_error", "collector_mode"):
                component["entity_category"] = "diagnostic"
            components[slug] = component
        components[key("machine_connected")] = {
            "platform":"binary_sensor", "unique_id":identity+"_"+key("machine_connected"),
            "name":"Machine connected", "device_class":"connectivity",
            "state_topic":root+"/availability", "payload_on":"online", "payload_off":"offline",
            "availability":[{"topic":self.online_topic}], "entity_category":"diagnostic"}
        # These controls change only the add-on's read scheduler. They never
        # become MDC writes, program transfers or CNC control commands.
        settings = {"boost":("switch",None,None,None), "boost_minutes":("number",1,60,"min"),
                    "boost_interval":("number",100,1000,"ms")}
        for setting,(platform,minimum,maximum,unit) in settings.items():
            component = {"platform":platform,"unique_id":identity+"_monitor_"+setting,
                         "name":{"boost":"Diagnostic boost","boost_minutes":"Diagnostic boost duration","boost_interval":"Diagnostic fast read interval"}[setting],
                         "command_topic":root+"/monitor/set/"+setting,
                         "state_topic":root+"/monitor/state/"+setting,
                         "entity_category":"config", "availability":[{"topic":self.online_topic}]}
            if platform == "number":
                component.update(min=minimum,max=maximum,step=1,mode="box",unit_of_measurement=unit)
            else:
                component.update(payload_on="ON",payload_off="OFF")
            components["monitor_"+setting] = component
        # HA requires a platform-only config to remove a component; omission
        # alone is insufficient. Keep tombstones in retained discovery until
        # the signal is collected again, so HA restart also sees the deletion.
        previous = self.journal["topics"].get(self.discovery_topic(identity), "")
        if previous:
            for slug, component in json.loads(previous).get("components", {}).items():
                if slug not in components:
                    components[slug] = {"platform": component["platform"]}
        self.put(self.discovery_topic(identity), {
            "device": {"identifiers": [identity], "name": machine["name"],
                       "manufacturer": "HAAS", "model": controller.get("model", "Haas CNC")},
            "origin": {"name": "Haas CNC Monitor", "sw_version": "0.1.0"},
            "components": components,
        })
        expected = {root + "/signal/" + key(name) for name in signals}
        for topic in list(self.journal["topics"]):
            if topic.startswith(root + "/signal/") and topic not in expected:
                self.forget(topic)

    def state(self, identity, name, signal, read_at, simulation):
        value = signal["value"]
        if signal.get("unit")=="°C" and signal.get("available",True) and isinstance(value,(int,float)) and value>0:
            marker=identity+"/"+key(name)
            if marker not in self.valid_temperatures:
                self.valid_temperatures.add(marker)
                self.journal["valid_temperatures"]=sorted(self.valid_temperatures)
                self.needs_flush=True
        details = None
        if signal.get("kind") == "binary_sensor":
            value = "ON" if value else "OFF"
        elif isinstance(value, (list, dict)):
            details = value
            value = len(value)
        elif isinstance(value, str) and len(value) > 255:
            details = value
            value = value[:252] + "…"
        self.put(self.machine_root(identity) + "/signal/" + key(name), {
            "value": value, "source": signal["source"], "last_read": read_at,
            "simulation": simulation, "details": details, "available":signal.get("available",True)})

    def remove(self, identity):
        self.valid_temperatures={marker for marker in self.valid_temperatures if not marker.startswith(identity+"/")}
        self.journal["valid_temperatures"]=sorted(self.valid_temperatures)
        self.needs_flush=True
        prefix = self.machine_root(identity) + "/"
        for topic in list(self.journal["topics"]):
            if topic.startswith(prefix) or topic == self.discovery_topic(identity):
                self.forget(topic)

    def reconcile(self, identities):
        # Recover deletion if a crash occurred after saving config but before MQTT cleanup.
        prefix = self.root + "/machine/"
        disco = "homeassistant/device/haas_" + self.journal["installation"] + "/"
        for topic in list(self.journal["topics"]):
            if topic.startswith(prefix) and topic[len(prefix):].split("/")[0] not in identities:
                self.forget(topic)
            if topic.startswith(disco) and topic[len(disco):].split("/")[0] not in identities:
                self.forget(topic)
        for identity in identities:
            self.availability(identity, False)
            self.put(self.machine_root(identity)+"/monitor/state/boost","OFF")
            # Avoid restoring stale values before a fresh successful read.
            for topic in list(self.journal["topics"]):
                if topic.startswith(self.machine_root(identity) + "/signal/"):
                    self.forget(topic)

    def close(self):
        self.put(self.online_topic, "offline")
        if self.connected:
            info = self.client.publish(self.online_topic, "offline", qos=1, retain=True)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                try:
                    info.wait_for_publish(timeout=2)
                except RuntimeError:
                    pass  # Journal and LWT cover a connection lost during shutdown.
        self.flush()
        self.client.disconnect()
        self.client.loop_stop()
