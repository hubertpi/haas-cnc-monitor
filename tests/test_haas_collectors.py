import asyncio
import json
import pytest
import httpx
from app.collector import MDCCollector, MTConnectCollector, HaasCollector, CollectionError, MDC_QUERIES, xml_document
from app.models import Endpoint
from app.main import create_app
from fastapi.testclient import TestClient

PROBE = b'''<MTConnectDevices xmlns="urn:mtconnect.org:MTConnectDevices:1.3"><Devices><Device id="d" name="VF-2" uuid="same-uuid"><Description manufacturer="Haas" model="VF-2" serialNumber="123"/><Components><Controller id="c"><DataItems><DataItem id="mode" type="CONTROLLER_MODE" category="EVENT"/><DataItem id="exec" type="EXECUTION" category="EVENT"/><DataItem id="avail" type="AVAILABILITY" category="EVENT"/><DataItem id="estop" type="EMERGENCY_STOP" category="EVENT"/><DataItem id="tool" type="TOOL_NUMBER" category="EVENT"/><DataItem id="program" type="PROGRAM" category="EVENT"/><DataItem id="feed" type="PATH_FEEDRATE" subType="ACTUAL" category="SAMPLE" units="MILLIMETER/SECOND"/><DataItem id="commanded" type="PATH_FEEDRATE" subType="PROGRAMMED" category="SAMPLE" units="MILLIMETER/SECOND"/><DataItem id="temp" type="TEMPERATURE" category="SAMPLE" units="CELSIUS"/><DataItem id="alarm" type="SYSTEM" category="CONDITION"/></DataItems></Controller><Rotary id="s1" name="spindle_1"><DataItems><DataItem id="rpm1" type="ROTARY_VELOCITY" subType="ACTUAL" category="SAMPLE" units="REVOLUTION/MINUTE"/></DataItems></Rotary><Rotary id="s2" name="spindle_2"><DataItems><DataItem id="rpm2" type="ROTARY_VELOCITY" subType="ACTUAL" category="SAMPLE" units="REVOLUTION/MINUTE"/></DataItems></Rotary></Components></Device></Devices></MTConnectDevices>'''
CURRENT = b'''<MTConnectStreams xmlns="urn:mtconnect.org:MTConnectStreams:1.3"><Header instanceId="1"/><Streams><DeviceStream name="VF-2" uuid="same-uuid"><ComponentStream><Events><Availability dataItemId="avail">AVAILABLE</Availability><ControllerMode dataItemId="mode">AUTOMATIC</ControllerMode><Execution dataItemId="exec">ACTIVE</Execution><EmergencyStop dataItemId="estop">ARMED</EmergencyStop><ToolNumber dataItemId="tool">4</ToolNumber><Program dataItemId="program">1234</Program></Events><Samples><PathFeedrate dataItemId="feed">12</PathFeedrate><PathFeedrate dataItemId="commanded">100</PathFeedrate><RotaryVelocity dataItemId="rpm1">1000</RotaryVelocity><RotaryVelocity dataItemId="rpm2">0</RotaryVelocity><Temperature dataItemId="temp">0</Temperature></Samples><Condition><Fault dataItemId="alarm" nativeCode="101">Servo alarm</Fault></Condition></ComponentStream></DeviceStream></Streams></MTConnectStreams>'''

def mt_client(current=CURRENT, probe=PROBE):
    calls=[]
    def request(req):
        calls.append((req.method,req.url.path))
        return httpx.Response(200,content=probe if req.url.path.endswith('/probe') else current)
    return httpx.AsyncClient(transport=httpx.MockTransport(request)),calls


def test_mtconnect_mapping_actual_values_multiple_spindles_and_read_only():
    async def check():
        c=MTConnectCollector();await c.client.aclose();c.client,calls=mt_client()
        data=await c.read({'host':'192.0.2.1','port':8082,'timeout':1})
        s=data['signals']
        assert s['feed_rate']['value']==12 and s['feed_rate']['unit']=='mm/s'
        assert s['spindle_1_rpm']['value']==1000 and s['spindle_2_rpm']['value']==0
        assert s['tool_code']['value']=='T04' and s['program_number']['value']=='O01234'
        assert s['emergency_stop']['value'] is False
        assert not s['mtc_temp']['available']
        assert any(v['value']=='Fault · Servo alarm' for v in s.values())
        assert data['controller']['serial']=='123'
        await c.read({'host':'192.0.2.1','port':8082,'timeout':1,'groups':['fast']})
        assert calls==[('GET','/probe'),('GET','/VF-2/current'),('GET','/VF-2/current')]
        await c.close()
    asyncio.run(check())


@pytest.mark.parametrize('raw',[b'<!DOCTYPE x [<!ENTITY a "evil">]><MTConnectDevices/>',b'<bad/>',b'not xml',b'x'*2000001])
def test_invalid_xml_rejected(raw):
    with pytest.raises(CollectionError):xml_document(raw,'MTConnectDevices')


@pytest.mark.parametrize('change',[(b'>AVAILABLE<',b'>UNAVAILABLE<'),(b'name="VF-2"',b'name="other"')])
def test_mtconnect_unavailable_and_wrong_device_fail(change):
    async def check():
        c=MTConnectCollector();await c.client.aclose();c.client,_=mt_client(CURRENT.replace(*change))
        with pytest.raises(CollectionError):await c.read({'host':'192.0.2.1','port':8082,'timeout':1})
        await c.close()
    asyncio.run(check())


def test_programmed_feed_is_not_production_feed_and_unknown_estop_omitted():
    async def check():
        current=CURRENT.replace(b'dataItemId="feed">12',b'dataItemId="feed">UNAVAILABLE').replace(b'>ARMED<',b'>UNKNOWN<')
        c=MTConnectCollector();await c.client.aclose();c.client,_=mt_client(current)
        s=(await c.read({'host':'192.0.2.1','port':8082,'timeout':1}))['signals']
        assert 'feed_rate' not in s and 'emergency_stop' not in s
        await c.close()
    asyncio.run(check())


def test_mtconnect_probe_requires_device_for_multiple_devices():
    async def check():
        import xml.etree.ElementTree as ET
        import copy
        root=ET.fromstring(PROBE);parent=list(root)[0];other=copy.deepcopy(list(parent)[0]);other.set('name','ST-20');parent.append(other)
        c=MTConnectCollector();await c.client.aclose();c.client,_=mt_client(probe=ET.tostring(root))
        with pytest.raises(CollectionError):await c.read({'host':'192.0.2.1','port':8082,'timeout':1})
        await c.close()
    asyncio.run(check())


def test_mdc_persistent_socket_read_only_and_alarm_is_not_estop():
    async def check():
        connections=0;commands=[];state=['ALARM ON']
        async def server(reader,writer):
            nonlocal connections
            connections+=1
            try:
                while raw:=await reader.readline():
                    q=raw.decode().strip();commands.append(q)
                    answers={'?Q100':'SERIAL NUMBER, 123','?Q104':'MODE, (MEM)','?Q500':f'PROGRAM, O01234, {state[0]}, PARTS, 42','?Q600 3027':'MACRO, 0','?Q201':'USING TOOL, 4','?Q200':'TOOL CHANGES, 10','?Q402':'M30 #1, 42','?Q403':'M30 #2, 1','?Q101':'SOFTWARE VERSION, 100.20','?Q102':'MODEL, VF-2','?Q300':'P.O. TIME, 00100:20:30','?Q301':'C.S. TIME, 00020:00:00','?Q303':'LAST CYCLE, 00000:01:30','?Q304':'PREV CYCLE, 00000:01:20'}
                    writer.write(('>'+answers[q]+'\r\n').encode());await writer.drain()
            finally:
                writer.close();await writer.wait_closed()
        srv=await asyncio.start_server(server,'127.0.0.1',0)
        c=MDCCollector();endpoint={'host':'127.0.0.1','port':srv.sockets[0].getsockname()[1],'timeout':1}
        data=await c.read(endpoint);s=data['signals']
        assert s['mode']['value']=='AUTOMATIC' and s['alarm_active']['value'] is True
        assert 'emergency_stop' not in s and 'feed_rate' not in s
        assert s['spindle_1_rpm']['value']==0 and s['tool_code']['value']=='T04'
        assert s['last_cycle_time']['value']==90 and data['controller']['serial']=='123'
        state[0]='IDLE';s=(await c.read({**endpoint,'groups':['fast']}))['signals']
        assert s['alarm_active']['value'] is False and s['execution']['value']=='READY'
        assert connections==1 and commands.count('?Q100')==1
        assert set(commands)<=MDC_QUERIES
        assert not any(q in commands for q in ('?Q600 62593','?Q600 3022','?Q600 5041','?Q600 5042','?Q600 5043','?Q600 5044','?Q600 5045','?Q600 5046'))
        for bad in ['?E100 1','?Q600 100','cnc_reset','?Q100\r\n?E100 1']:
            with pytest.raises(CollectionError):await c.query(bad,1)
        assert set(commands)<=MDC_QUERIES
        await c.close();srv.close();await srv.wait_closed()
    asyncio.run(check())


def test_mdc_wrong_server_and_timeout_are_not_online():
    async def check():
        async def server(reader,writer):
            await reader.readline();writer.write(b'>NOT HAAS\r\n');await writer.drain();writer.close()
        srv=await asyncio.start_server(server,'127.0.0.1',0)
        c=MDCCollector()
        with pytest.raises(CollectionError):await c.read({'host':'127.0.0.1','port':srv.sockets[0].getsockname()[1],'timeout':1})
        await c.close();srv.close();await srv.wait_closed()
    asyncio.run(check())


def test_api_test_token_covers_protocol_and_device(tmp_path):
    with TestClient(create_app(tmp_path,{'mode':'simulation'},development=True)) as client:
        headers={'x-monitor-csrf':client.get('/api/status').json()['csrf']}
        endpoint={'host':'192.0.2.1','protocol':'mtconnect','device':'VF-2'}
        headers['x-connection-test']=client.post('/api/test',json=endpoint,headers=headers).json()['test_token']
        assert client.post('/api/machines',json={**endpoint,'name':'Haas','device':'ST-20'},headers=headers).status_code==409
        assert client.post('/api/machines',json={**endpoint,'name':'Haas','protocol':'mdc'},headers=headers).status_code==409
        assert client.post('/api/machines',json={**endpoint,'name':'Haas'},headers=headers).status_code==201


def test_unknown_protocol_and_control_fields_rejected():
    from pydantic import ValidationError
    for data in [{'host':'192.0.2.1','protocol':'focas'},{'host':'192.0.2.1','command':'?E100 1'},{'host':'192.0.2.1','device':'VF\n2'}]:
        with pytest.raises(ValidationError):Endpoint(**data)


@pytest.mark.parametrize('protocol',['mtconnect','mdc'])
def test_live_factory_collector_continues_after_machine_is_saved(tmp_path,monkeypatch,protocol):
    """Exercise the actual live factory through Manager, not only connection tests."""
    from app.collector import factory
    from app.manager import Manager
    from app.models import MachineInput
    from app.mqtt import Publisher, key
    from app.store import Store
    import app.collector as module

    async def check():
        server=None
        host='127.0.0.1';port=8082
        if protocol=='mtconnect':
            real_client=httpx.AsyncClient
            def handle(req):
                assert req.method=='GET'
                return httpx.Response(200,content=PROBE if req.url.path.endswith('/probe') else CURRENT)
            monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:real_client(transport=httpx.MockTransport(handle)))
        else:
            async def reply(reader,writer):
                try:
                    while raw:=await reader.readline():
                        command=raw.decode().strip()
                        assert command in MDC_QUERIES
                        answers={'?Q100':'SERIAL NUMBER, 123','?Q104':'MODE, (MEM)',
                                 '?Q500':'PROGRAM, O01234, IDLE, PARTS, 42',
                                 '?Q600 3027':'MACRO, 0','?Q201':'USING TOOL, 4',
                                 '?Q101':'SOFTWARE VERSION, TEST','?Q102':'MODEL, VF-2'}
                        writer.write(('>'+answers.get(command,'?, '+command)+'\r\n').encode());await writer.drain()
                finally:
                    writer.close();await writer.wait_closed()
            server=await asyncio.start_server(reply,host,0)
            port=server.sockets[0].getsockname()[1]
        pub=Publisher(tmp_path)
        manager=Manager(Store(tmp_path/'machines.json'),pub,factory({'mode':'haas','hardware_testing_enabled':True}))
        try:
            record=await manager.save(MachineInput(host=host,port=port,name='Live Haas fixture',protocol=protocol,fast_interval=.1))
            identity=record['id']
            for _ in range(100):
                if 'collector_mode' in manager.status[identity]['signals'] or manager.tasks[identity].done():break
                await asyncio.sleep(.01)
            state=manager.status[identity]
            assert state['error'] is None,state['error']
            assert state['online'] and state['signals']['collector_mode']['value']=='haas'
            assert state['signals']['execution']['value'] in ('ACTIVE','READY')
            task=manager.tasks[identity]
            # Verify another scheduler read succeeds after discovery/state publication.
            await asyncio.sleep(.15)
            assert not task.done() and manager.status[identity]['error'] is None
            root=pub.machine_root(identity)
            assert pub.journal['topics'][root+'/availability']=='online'
            payload=json.loads(pub.journal['topics'][root+'/signal/'+key('collector_mode')])
            assert payload['value']=='haas' and payload['simulation'] is False
            config=json.loads(pub.journal['topics'][pub.discovery_topic(identity)])
            assert config['device']['manufacturer']=='HAAS'
        finally:
            await manager.close()
            if server:server.close();await server.wait_closed()
    asyncio.run(check())


def test_haas_1_2_controller_layout_timers_counters_events_and_metadata():
    """Sanitized structural fixture from the supplied VF-4SS XML."""
    from pathlib import Path
    from app.mqtt import Publisher, key
    fixtures=Path(__file__).parent/'fixtures'
    probe=(fixtures/'haas_ngc_1_2_probe.xml').read_bytes()
    current=(fixtures/'haas_ngc_1_2_current.xml').read_bytes()
    async def check():
        c=MTConnectCollector();await c.client.aclose();c.client,_=mt_client(current,probe)
        data=await c.read({'host':'192.0.2.1','port':8082,'timeout':1})
        ctrl=data['controller'];s=data['signals']
        assert ctrl['version']=='REL-100.19.000.1123' and ctrl['serial']=='1234567'
        assert ctrl['agent_version']=='1.2.0.1.2' and ctrl['reported_axes']==[] and ctrl['axes'] is None
        assert len(s)==20
        assert s['mtc_sspeed']['value']==4300 and s['mtc_sspeed']['precision']==0
        assert s['mtc_sspeed']['label']=='Spindle RPM' and s['mtc_sspeed']['unit']=='rpm'
        for name,expected in [('mtc_lcycle',4),('mtc_tcycle',107),('mtc_cyremtim',0)]:
            assert s[name]['value']==expected and s[name]['unit']=='s' and s[name]['device_class']=='duration'
        assert s['mtc_fdovrd']['value']==100 and s['mtc_fdovrd']['unit']=='%'
        assert 'feed_rate' not in s and 'mtconnect.actual_feed' in data['unsupported']
        assert 'mtconnect.axes' in data['unsupported']
        assert s['mtc_m30c1']['value']==87654 and isinstance(s['mtc_m30c1']['value'],int)
        assert s['mtc_m30c1']['state_class']=='total_increasing'
        assert s['mtc_lpremain']['value']==0 and 'state_class' not in s['mtc_lpremain']
        assert s['mtc_macdsp1']['value']=='10188' # no unjustified macro-address/value interpretation
        assert s['program_number']['value']=='BRACKET-OP030.txt' and s['program_number']['label']=='Active program'
        assert s['mtc_elog']['value']=='Rapid Override' and len(s['mtc_elog']['details'])==2
        assert s['emergency_stop']['value'] is False and s['mtc_mcond_normal_condition']['value']=='Normal'
        await c.close()
    asyncio.run(check())


def test_reported_rotary_axes_are_distinct_from_spindles():
    async def check():
        additions=b'<Linear id="x" name="X"/><Rotary id="a" name="A"><DataItems><DataItem id="apos" type="ANGLE" category="SAMPLE" units="DEGREE" subType="ACTUAL"/></DataItems></Rotary><Rotary id="b" name="B"><DataItems><DataItem id="bpos" type="ANGLE" category="SAMPLE" units="DEGREE" subType="ACTUAL"/></DataItems></Rotary>'
        c=MTConnectCollector();await c.client.aclose();c.client,_=mt_client(probe=PROBE.replace(b'</Components>',additions+b'</Components>'))
        await c.read({'host':'192.0.2.1','port':8082,'timeout':1})
        assert c.metadata['reported_axes']==['X','A','B'] and c.metadata['axes']==3
        await c.close()
    asyncio.run(check())


def test_classic_mdc_framing_addressed_macro_and_busy_status():
    async def check():
        async def server(reader,writer):
            try:
                while raw:=await reader.readline():
                    q=raw.decode().strip()
                    answers={'?Q100':'SERIAL NUMBER, 123','?Q104':'MODE, (MEM)','?Q500':'STATUS, BUSY','?Q600 3027':'MACRO, 3027, 4299.8199462890625'}
                    writer.write(('\x02'+answers[q]+'\x17\r\n').encode());await writer.drain()
            finally:writer.close();await writer.wait_closed()
        srv=await asyncio.start_server(server,'127.0.0.1',0);c=MDCCollector()
        try:
            data=await c.read({'host':'127.0.0.1','port':srv.sockets[0].getsockname()[1],'timeout':1,'groups':['fast']})
            assert data['signals']['execution']['value']=='ACTIVE'
            assert data['signals']['spindle_1_rpm']['value']==4300
            assert 'emergency_stop' not in data['signals'] and 'feed_rate' not in data['signals']
        finally:await c.close();srv.close();await srv.wait_closed()
    asyncio.run(check())


def test_audited_discovery_retains_entity_ids_and_duration_precision(tmp_path):
    from pathlib import Path
    from app.mqtt import Publisher,key
    fixtures=Path(__file__).parent/'fixtures'
    async def check():
        c=MTConnectCollector();await c.client.aclose();c.client,_=mt_client((fixtures/'haas_ngc_1_2_current.xml').read_bytes(),(fixtures/'haas_ngc_1_2_probe.xml').read_bytes())
        data=await c.read({'host':'192.0.2.1','port':8082,'timeout':1});await c.close()
        s=data['signals'];pub=Publisher(tmp_path);machine={'id':'stable-machine','name':'Fixture','interval':1}
        old=set(('mtc_avail','mtc_mcond_normal_condition','mtc_aalarms','mode','execution','program_number','mtc_lpremain','mtc_m30c1','mtc_m30c2','mtc_macdsp1','mtc_macdsp2','emergency_stop','mtc_rovrd','mtc_fdovrd','mtc_tcycle','mtc_lcycle','mtc_cyremtim','mtc_sspeed','mtc_ssovrd'))
        pub.discovery(machine,{n:s[n] for n in old},{'model':'VF-4SS'})
        before=json.loads(pub.journal['topics'][pub.discovery_topic(machine['id'])])
        pub.discovery(machine,s,data['controller'])
        after=json.loads(pub.journal['topics'][pub.discovery_topic(machine['id'])])
        assert after['device']['sw_version']=='REL-100.19.000.1123' and after['device']['serial_number']=='1234567'
        for name in old:
            assert before['components'][key(name)]['unique_id']==after['components'][key(name)]['unique_id']
            assert after['components'][key(name)]['platform']=='sensor' if name!='emergency_stop' else after['components'][key(name)]['platform']=='binary_sensor'
        assert after['components'][key('mtc_tcycle')]['device_class']=='duration'
        assert after['components'][key('mtc_tcycle')]['unit_of_measurement']=='s'
        assert after['components'][key('mtc_sspeed')]['suggested_display_precision']==0
        pub.state(machine['id'],'mtc_elog',s['mtc_elog'],'NOW',False)
        state=json.loads(pub.journal['topics'][pub.machine_root(machine['id'])+'/signal/'+key('mtc_elog')])
        assert state['value']=='Rapid Override' and len(state['details'])==2
    asyncio.run(check())
