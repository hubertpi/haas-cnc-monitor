import asyncio
import copy
import math
import time
import uuid
from datetime import datetime, timezone
from .collector import CollectionError
from .models import MachineInput
from .utilization import Utilization
from .schedule import ScheduleStore


def utc():
    return datetime.now(timezone.utc).isoformat()


class Manager:
    def __init__(self, store, publisher, collector_factory):
        self.store, self.publisher, self.collector_factory = store, publisher, collector_factory
        self.records = {item["id"]: {**item, **MachineInput.model_validate({k:v for k,v in item.items() if k not in ("id",)}).model_dump(exclude={"revision"})}
                        for item in store.data["machines"]}
        self.schedule=ScheduleStore(store.path.parent / "schedule.json")
        self.utilization = Utilization(store.path.parent / "utilization.sqlite",schedule=self.schedule)
        self.boosts = {}
        self.status = {}
        self.tasks = {}
        self.wakes = {}
        self.lock = asyncio.Lock()
        self.control_tasks = set()
        self.publisher.on_control = self.control

    async def save_schedule(self,value):
        async with self.lock:
            changed=self.schedule.save(value)
            if changed:
                self.utilization.flush()
                for wake in self.wakes.values(): wake.set()
            return self.schedule.view()

    def control(self, identity, setting, payload):
        if identity not in self.records or setting not in ("boost","boost_minutes","boost_interval"):
            return
        try:
            value = payload.decode("ascii")
            if setting == "boost":
                if value not in ("ON","OFF"): return
                self.boost(identity,value=="ON")
            else:
                parsed = float(value)
                if not math.isfinite(parsed) or not parsed.is_integer(): return
                number = int(parsed)
                if setting == "boost_minutes" and not 1<=number<=60: return
                if setting == "boost_interval" and not 100<=number<=1000: return
                async def save_setting():
                    async with self.lock:
                        if identity not in self.records: return
                        record = self.records[identity]
                        record[setting] = number if setting=="boost_minutes" else number/1000
                        record["revision"] += 1
                        self.store.save(list(self.records.values()))
                task = asyncio.create_task(save_setting())
                self.control_tasks.add(task)
                task.add_done_callback(self.control_tasks.discard)
        except (ValueError,UnicodeError):
            return

    def empty_status(self):
        return {"online": False, "last_attempt": None, "last_success": None,
                "error": None, "error_code": None, "reconnect_attempts": 0,
                "controller": {}, "signals": {}, "unsupported": {}, "read_duration_ms": 0, "boost_remaining": 0}

    async def start(self):
        self.publisher.reconcile(set(self.records))
        for identity in self.records:
            self.launch(identity)

    def launch(self, identity):
        self.status[identity] = self.empty_status()
        self.wakes[identity] = asyncio.Event()
        if self.records[identity]["enabled"]:
            self.tasks[identity] = asyncio.create_task(self.loop(identity))

    async def stop_one(self, identity):
        task = self.tasks.pop(identity, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.publisher.availability(identity, False)
        self.utilization.gap(identity)
        self.boosts.pop(identity, None)
        self.publisher.put(self.publisher.machine_root(identity)+"/monitor/state/boost","OFF")

    def boost(self, identity, enabled, minutes=None, interval=None):
        if identity not in self.records:
            raise KeyError(identity)
        record = self.records[identity]
        if enabled and not record["enabled"]:
            raise ValueError("Enable collection before boosting reads")
        if enabled:
            self.boosts[identity] = (time.monotonic() + (minutes or record["boost_minutes"]) * 60, interval or record["boost_interval"])
        else:
            self.boosts.pop(identity, None)
        self.wakes[identity].set()

    async def wait(self,identity,delay):
        try:
            await asyncio.wait_for(self.wakes[identity].wait(),max(0.01,delay))
        except asyncio.TimeoutError:
            pass
        self.wakes[identity].clear()

    async def save(self, value, identity=None):
        async with self.lock:
            new = value.model_dump(exclude={"revision"})
            for other_id, record in self.records.items():
                if other_id != identity and (record["host"], record["port"]) == (new["host"], new["port"]):
                    raise ValueError("That machine endpoint is already configured")
            if identity:
                if identity not in self.records:
                    raise KeyError(identity)
                if value.revision != self.records[identity]["revision"]:
                    raise ValueError("Configuration changed; refresh before saving")
            identity = identity or str(uuid.uuid4())
            new.update(id=identity, revision=self.records.get(identity, {}).get("revision", 0) + 1)
            replacement = {**self.records, identity: new}
            self.store.save(list(replacement.values()))
            await self.stop_one(identity)
            self.records = replacement
            self.launch(identity)
            return new

    async def remove(self, identity):
        async with self.lock:
            if identity not in self.records:
                raise KeyError(identity)
            replacement = {k: v for k, v in self.records.items() if k != identity}
            self.store.save(list(replacement.values()))
            await self.stop_one(identity)
            self.records = replacement
            self.status.pop(identity, None)
            self.publisher.remove(identity)
            self.utilization.remove(identity)

    async def loop(self, identity):
        collector = self.collector_factory()
        record = self.records[identity]
        status = self.status[identity]
        failures = 0
        sent_values = {}
        readings = {}
        unsupported = {}
        due = {"fast": 0, "status": 0, "slow": 0}
        last_stats = 0
        previous_boost = None
        schedule_revision=self.schedule.revision
        try:
            while True:
                started = time.monotonic()
                if schedule_revision!=self.schedule.revision:
                    schedule_revision=self.schedule.revision
                    readings={n:s for n,s in readings.items() if s.get("group")!="utilization"}
                    last_stats=0
                    due["fast"]=0
                boost = self.boosts.get(identity)
                if boost and boost[0] <= started:
                    self.boosts.pop(identity, None); boost = None
                status["boost_remaining"] = max(0, round(boost[0]-started)) if boost else 0
                rates = {"fast": min(record["fast_interval"],boost[1]) if boost else record["fast_interval"],
                         "status": min(record["interval"],0.5) if boost else record["interval"],
                         "slow": min(record["diagnostic_interval"],5) if boost else record["diagnostic_interval"]}
                if boost != previous_boost:
                    due = {group:min(deadline,started) for group,deadline in due.items()}
                    previous_boost = boost
                groups = [group for group,deadline in due.items() if deadline <= started]
                if not groups:
                    await self.wait(identity,max(0.01,min(due.values())-started)); continue
                status["last_attempt"] = utc()
                try:
                    result = await collector.read({**record, "groups": groups})
                    finished = time.monotonic()
                    status["read_duration_ms"] = round((finished-started)*1000,2)
                    # Missed deadlines are skipped. Never queue catch-up reads.
                    for group in groups:
                        due[group] = max(started+rates[group], finished+0.01)
                    now = utc()
                    # Refresh diagnostics reported by this collector without native-function assumptions.
                    unsupported.update(result.get("unsupported",{}))
                    status.update(online=True, last_success=now, error=None, error_code=None,
                                  controller=result["controller"], unsupported=copy.deepcopy(unsupported))
                    incoming = copy.deepcopy(result["signals"])
                    for name,signal in list(incoming.items()):
                        if signal.get("unit")=="°C" and signal.get("available") is False and not self.publisher.temperature_validated(identity,name):
                            del incoming[name]
                    for name in list(readings):
                        if readings[name].get("group", "fast") in groups and name not in incoming:
                            del readings[name]
                    for name,signal in incoming.items():
                        group = signal.get("group", "fast")
                        signal.update(last_read=now, expire_after=max(15,rates.get(group,60)*3))
                        readings[name] = signal
                    if "fast" in groups:
                        self.utilization.observe(identity, incoming, max_gap=max(2,rates["fast"]*3))
                    if finished-last_stats >= 5:
                        for name,signal in self.utilization.signals(identity).items():
                            signal.update(last_read=now, expire_after=30)
                            readings[name] = signal
                        last_stats = finished
                    signals = copy.deepcopy(readings)
                    signals["collector_mode"] = {"value": collector.mode, "source": "monitor.mode", "kind": "sensor"}
                    signals["collector_error"] = {"value": "OK", "source": "monitor.error", "kind": "sensor"}
                    signals["diagnostic_boost_active"] = {"value":bool(boost),"source":"monitor.read_frequency","kind":"binary_sensor","last_read":now}
                    signals["diagnostic_boost_remaining"] = {"value":status["boost_remaining"],"source":"monitor.read_frequency","kind":"sensor","unit":"s","last_read":now}
                    monitor_root = self.publisher.machine_root(identity)+"/monitor/state/"
                    self.publisher.put(monitor_root+"boost", "ON" if boost else "OFF")
                    self.publisher.put(monitor_root+"boost_minutes",str(record["boost_minutes"]))
                    self.publisher.put(monitor_root+"boost_interval",str(round(record["boost_interval"]*1000)))
                    self.publisher.discovery(record, signals, result["controller"])
                    for name, signal in signals.items():
                        old = sent_values.get(name)
                        # Refresh before HA expire_after, even if value hasn't changed.
                        if old is None or old[0].get("value") != signal.get("value") or old[0].get("source") != signal.get("source") or old[0].get("details") != signal.get("details") or old[0].get("available",True) != signal.get("available",True) or time.monotonic() - old[1] >= max(5,signal.get("expire_after",15)/3):
                            self.publisher.state(identity, name, signal, signal.get("last_read",now), result.get("simulation", False))
                            sent_values[name] = (copy.deepcopy(signal), time.monotonic())
                        signal.setdefault("last_read", now)
                    status["signals"] = signals
                    self.publisher.availability(identity, True)
                    failures = 0
                except (CollectionError, OSError) as error:
                    failures += 1
                    status.update(online=False, error=str(error), error_code=getattr(error, "code", None))
                    status["reconnect_attempts"] += 1
                    self.publisher.availability(identity, False)
                    self.utilization.gap(identity)
                    readings.clear(); sent_values.clear()
                    due = {"fast":0,"status":0,"slow":0}
                    # Reconnect after failure; never keep a partially read TCP response.
                    await collector.close()
                await self.wait(identity,min(60, 2 ** min(failures, 6)) if failures else max(0.01,min(due.values())-time.monotonic()))
        except asyncio.CancelledError:
            raise
        except Exception:
            status.update(online=False, error="Collector failed internally; see service logs")
            import logging
            logging.exception("Collector task failed for %s", identity)
            self.publisher.availability(identity, False)
        finally:
            await collector.close()

    def list(self):
        items = []
        for identity, record in self.records.items():
            item = copy.deepcopy({**record, **self.status.get(identity, self.empty_status())})
            for name, signal in item["signals"].items():
                from .mqtt import key
                signal["published"] = self.publisher.acknowledged(self.publisher.machine_root(identity) + "/signal/" + key(name))
                signal["discovery_published"] = self.publisher.acknowledged(self.publisher.discovery_topic(identity))
            boost = self.boosts.get(identity)
            item["boost_remaining"] = max(0,round(boost[0]-time.monotonic())) if boost else 0
            items.append(item)
        return items

    async def close(self):
        await asyncio.gather(*(self.stop_one(identity) for identity in list(self.tasks)))
        if self.control_tasks:
            await asyncio.gather(*self.control_tasks,return_exceptions=True)
        self.utilization.close()
