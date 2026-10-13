import asyncio
import json
from app.collector import Simulator
from app.manager import Manager
from app.models import MachineInput
from app.mqtt import Publisher
from app.store import Store


def test_fault_isolation_and_live_changes(tmp_path):
    async def scenario():
        pub = Publisher(tmp_path)
        manager = Manager(Store(tmp_path / "machines.json"), pub, Simulator)
        await manager.start()
        good = await manager.save(MachineInput(host="good.example", name="Good"))
        bad = await manager.save(MachineInput(host="offline.example", name="Offline"))
        await asyncio.sleep(.1)
        good_task = manager.tasks[good["id"]]
        assert manager.status[good["id"]]["online"]
        assert manager.status[bad["id"]]["error_code"] is None
        assert manager.status[bad["id"]]["reconnect_attempts"] == 1
        await manager.save(MachineInput(host="offline.example", name="Disabled", enabled=False, revision=1), bad["id"])
        assert manager.tasks[good["id"]] is good_task
        assert not good_task.done()
        assert bad["id"] not in manager.tasks
        await manager.remove(bad["id"])
        assert manager.tasks[good["id"]] is good_task
        try:
            await manager.save(MachineInput(host="good.example", name="Duplicate"))
            assert False, "Duplicate endpoint accepted"
        except ValueError:
            pass
        await manager.close()
        assert not manager.tasks
    asyncio.run(scenario())


def test_schema_tombstones_and_restart_reconciliation(tmp_path):
    pub = Publisher(tmp_path)
    machine = {"id": "stable-id", "name": "Lathe", "interval": 1}
    signals = {"rpm": {"value": 12, "source": "cnc_rdspeed", "unit": "rpm"},
               "unknown": {"value": "a", "source": "vendor.raw"}}
    pub.discovery(machine, signals, {})
    pub.state(machine["id"], "rpm", signals["rpm"], "now", False)
    pub.discovery(machine, {"unknown": signals["unknown"]}, {})
    config = json.loads(pub.journal["topics"][pub.discovery_topic(machine["id"])])
    from app.mqtt import key
    assert config["components"][key("rpm")] == {"platform": "sensor"}
    assert "unit_of_measurement" not in config["components"][key("unknown")]
    assert pub.journal["topics"][pub.machine_root(machine["id"]) + "/signal/" + key("rpm")] == ""
    restored = Publisher(tmp_path)
    restored.reconcile(set())
    assert restored.journal["topics"][restored.discovery_topic(machine["id"])] == ""
    assert restored.journal["installation"] == pub.journal["installation"]


def test_structured_alarm_and_long_text_attributes(tmp_path):
    from app.mqtt import key
    pub = Publisher(tmp_path)
    alarms = [{"code": 123, "axis": 1, "type": 0, "message": "Overtravel"},
              {"code": 456, "axis": 2, "type": 1, "message": "Servo alarm"}]
    pub.state("machine", "alarms", {"value": alarms, "source": "cnc_rdalmmsg2"}, "now", False)
    payload = json.loads(pub.journal["topics"][pub.machine_root("machine") + "/signal/" + key("alarms")])
    assert payload["value"] == 2 and payload["details"] == alarms
    full_text = "x" * 600
    pub.state("machine", "message", {"value": full_text, "source": "cnc_rdopmsg"}, "now", False)
    payload = json.loads(pub.journal["topics"][pub.machine_root("machine") + "/signal/" + key("message")])
    assert len(payload["value"]) <= 255 and payload["details"] == full_text
    pub.state("machine", "alarms", {"value": [], "source": "cnc_rdalmmsg2"}, "later", False)
    payload = json.loads(pub.journal["topics"][pub.machine_root("machine") + "/signal/" + key("alarms")])
    assert payload["value"] == 0 and payload["details"] == []


def test_poll_groups_partial_values_boost_expiry_and_local_only_controls(tmp_path,monkeypatch):
    import app.manager as module
    class Clock:
        value=0
        def now(self): return self.value
    clock=Clock()
    class Collector:
        mode='simulation'
        busy=False
        calls=[]
        async def read(self,record):
            assert not self.busy
            self.busy=True
            self.calls.append((clock.value,record['groups']))
            clock.value+=.05
            await asyncio.sleep(0)
            self.busy=False
            signals={}
            if 'fast' in record['groups']:
                signals={name:{'value':value,'source':'test','group':'fast'} for name,value in [('mode','AUTOMATIC'),('execution','ACTIVE'),('feed_rate',4)]}
            if 'slow' in record['groups']:
                signals['program_number']={'value':53,'source':'test','group':'slow'}
            return {'controller':{},'signals':signals,'unsupported':{'cnc_diagnoss.309':{'code':3}} if 'slow' in record['groups'] else {}}
        async def close(self): pass
    async def run():
        pub=Publisher(tmp_path)
        manager=Manager(Store(tmp_path/'machines.json'),pub,Collector)
        record=await manager.save(MachineInput(host='192.0.2.1',name='Test',fast_interval=.25))
        identity=record['id']
        # Replace scheduler waits with virtual time; no wall-clock soak needed.
        monkeypatch.setattr(module.time,'monotonic',clock.now)
        async def virtual_wait(identity,delay):
            clock.value+=delay
            await asyncio.sleep(0)
        manager.wait=virtual_wait
        for _ in range(15): await asyncio.sleep(0)
        assert manager.status[identity]['signals']['program_number']['value']==53
        assert 'cnc_diagnoss.309' in manager.status[identity]['unsupported']
        collector_calls=Collector.calls
        assert any(groups==['fast'] for _,groups in collector_calls)
        manager.control(identity,'boost_minutes',b'2.0')
        for _ in range(5):await asyncio.sleep(0)
        assert manager.records[identity]['boost_minutes']==2
        manager.control(identity,'boost',b'ON')
        assert identity in manager.boosts
        clock.value=manager.boosts[identity][0]+1
        for _ in range(10):await asyncio.sleep(0)
        assert identity not in manager.boosts
        revision=manager.records[identity]['revision']
        for setting,payload in [('boost_minutes',b'999'),('boost_interval',b'inf'),('boost_minutes',b'nan'),('boost_interval',b'0'),('cnc_reset',b'ON'),('boost',b'cnc_start')]:
            manager.control(identity,setting,payload)
        assert manager.records[identity]['revision']==revision
        await manager.close()
    asyncio.run(run())


def test_retained_local_commands_are_not_replayed(tmp_path):
    from types import SimpleNamespace
    pub=Publisher(tmp_path)
    received=[]
    pub.on_control=lambda *args:received.append(args)
    topic=pub.machine_root('machine')+'/monitor/set/boost'
    pub._message(None,None,SimpleNamespace(topic=topic,payload=b'ON',retain=True))
    pub.pump()
    assert received==[]
    pub._message(None,None,SimpleNamespace(topic=topic,payload=b'ON',retain=False))
    pub.pump()
    assert received==[('machine','boost',b'ON')]


def test_utilization_availability_survives_cnc_outage(tmp_path):
    pub=Publisher(tmp_path)
    machine={'id':'test','name':'Lathe','interval':1}
    signals={'production_month_utilization':{'value':20,'source':'monitor.confirmed_feed_utilization','group':'utilization','expire_after':30},
             'rpm':{'value':100,'source':'cnc_acts2'}}
    pub.discovery(machine,signals,{})
    from app.mqtt import key
    config=json.loads(pub.journal['topics'][pub.discovery_topic('test')])
    assert config['components'][key('production_month_utilization')]['availability']==[{'topic':pub.online_topic}]
    assert len(config['components'][key('rpm')]['availability'])==2


def test_added_order_survives_edit_and_restart_without_created_timestamps(tmp_path):
    async def scenario():
        pub = Publisher(tmp_path)
        manager = Manager(Store(tmp_path/'machines.json'), pub, Simulator)
        first = await manager.save(MachineInput(host='first.example', name='Zed', enabled=False))
        second = await manager.save(MachineInput(host='second.example', name='Alpha', enabled=False))
        await manager.save(MachineInput(host='first.example', name='Renamed', enabled=False, revision=1), first['id'])
        assert [m['id'] for m in manager.list()] == [first['id'], second['id']]
        await manager.close()
        restored = Manager(Store(tmp_path/'machines.json'), Publisher(tmp_path), Simulator)
        assert [m['id'] for m in restored.list()] == [first['id'], second['id']]
        assert all('created_at' not in m for m in restored.list())
        await restored.close()
    asyncio.run(scenario())


def test_unavailable_utilization_gets_signal_availability(tmp_path):
    pub=Publisher(tmp_path)
    machine={'id':'m','name':'Haas','interval':1}
    pub.discovery(machine,{'production_shift_1_utilization':{'value':0,'unit':'%','source':'monitor.confirmed_feed_utilization','group':'utilization','available':False}}, {})
    config=json.loads(pub.journal['topics'][pub.discovery_topic('m')])
    component=next(c for c in config['components'].values() if c.get('unit_of_measurement')=='%')
    assert len(component['availability'])==2
    assert 'value_json.available' in component['availability'][1]['value_template']
