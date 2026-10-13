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
