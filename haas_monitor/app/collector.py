"""Read-only Haas transports. No CNC writes or arbitrary query interface."""
import asyncio
import math
import re
import socket
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote
import httpx


class CollectionError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def signal(value, source, group='fast', unit=None, kind='sensor', **extra):
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        precision = 0 if unit=='rpm' else 1 if unit=='%' else 2 if unit in ('s','°C','°F') else None
        if precision is not None:
            value=round(value,precision)
            if float(value).is_integer(): value=int(value)
            extra.setdefault('precision',precision)
        if unit=='s': extra.setdefault('device_class','duration')
    return dict(value=value, source=source, group=group, unit=unit, kind=kind, **extra)


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Non-finite machine reading')
    return int(result) if result.is_integer() else result


def local(tag):
    return tag.rsplit('}', 1)[-1]


def xml_document(raw, expected):
    if len(raw) > 2_000_000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise CollectionError('Unsafe or oversized MTConnect document')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise CollectionError('Invalid MTConnect XML') from error
    if local(root.tag) != expected:
        raise CollectionError('Endpoint did not return ' + expected)
    return root


class Simulator:
    mode = 'simulation'
    async def read(self, endpoint):
        await asyncio.sleep(.01)
        if endpoint['host'].startswith('offline'):
            raise CollectionError('Simulated Haas connection failure')
        tick = int(time.monotonic())
        data = {
            'mode': signal('AUTOMATIC', 'simulation.mode'),
            'execution': signal('ACTIVE' if tick % 20 < 15 else 'READY', 'simulation.execution'),
            'feed_rate': signal(500 if tick % 20 < 15 else 0, 'simulation.actual_feed', unit='mm/min'),
            'spindle_1_rpm': signal(1200 + tick % 40, 'simulation.spindle', unit='rpm'),
            'tool_code': signal('T04', 'simulation.tool', 'status'),
            'spindle_1_load': signal(36, 'simulation.spindle_load', 'status', '%'),
            'program_number': signal('O01234', 'simulation.program', 'slow'),
            'emergency_stop': signal(False, 'simulation.estop', kind='binary_sensor'),
            'alarm_active': signal(False, 'simulation.alarm', kind='binary_sensor')}
        return {'controller': {'series':'NGC','version':'SIM','model':'Simulated Haas VF-2','axes':3,'simulation':True},
                'signals':{n:s for n,s in data.items() if s['group'] in endpoint.get('groups', ('fast','status','slow'))},
                'unsupported':{},'simulation':True}
    async def close(self):
        pass


class MTConnectCollector:
    def __init__(self):
        self.client = httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                       limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))
        self.metadata = None
        self.items = {}
        self.device = None
        self.instance = None

    async def get(self, endpoint, request, device=''):
        host = endpoint['host']
        if ':' in host: host = '[' + host + ']'
        path = (quote(device, safe='') + '/' if device else '') + request
        url = f"http://{host}:{endpoint['port']}/{path}"
        try:
            async with self.client.stream('GET', url, timeout=endpoint['timeout']) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>2_000_000: raise CollectionError('MTConnect response exceeds 2 MB')
                return bytes(raw)
        except httpx.HTTPError as error:
            raise CollectionError('MTConnect request failed: ' + type(error).__name__) from error

    async def probe(self, endpoint):
        try:
            raw = await self.get(endpoint, 'probe')
        except CollectionError:
            if not endpoint.get('device'): raise
            raw = await self.get(endpoint, 'probe', endpoint['device'])
        root = xml_document(raw, 'MTConnectDevices')
        devices = [e for e in root.iter() if local(e.tag)=='Device']
        key = endpoint.get('device', '')
        if key: devices = [d for d in devices if key in (d.get('name'),d.get('uuid'),d.get('id'))]
        if len(devices)!=1:
            raise CollectionError('Specify the device name when the agent exposes multiple devices' if devices else 'MTConnect device not found')
        device = devices[0]
        self.device = device.get('name') or device.get('uuid') or device.get('id')
        self.items = {}
        for component in device.iter():
            for child in component:
                if local(child.tag) == 'DataItems':
                    for item in child:
                        if local(item.tag)=='DataItem':
                            self.items[item.get('id')] = {**item.attrib, 'component':component.get('name') or component.get('id') or local(component.tag), 'component_type':local(component.tag), 'description':next((child.text for child in item if local(child.tag)=='Source'),'')}
        description = next((d for d in device if local(d.tag)=='Description'), None)
        desc = description.attrib if description is not None else {}
        reported_axes=[]
        for component in device.iter():
            typ=local(component.tag)
            if typ=='Linear' or (typ=='Rotary' and any(i.get('type')=='ANGLE' for i in component.iter())):
                reported_axes.append(component.get('name') or component.get('id') or typ)
        header=next((e for e in root if local(e.tag)=='Header'),None)
        self.metadata = {'version':desc.get('softwareVersion'), 'agent_version':header.get('version') if header is not None else None,
                         'reported_axes':reported_axes, 'axes':len(reported_axes) if reported_axes else None,
                         'series':'NGC/MTConnect','model':desc.get('model') or device.get('name') or 'Haas CNC',
                         'serial':desc.get('serialNumber'), 'manufacturer':desc.get('manufacturer','Haas'), 'device':self.device}

    async def read(self, endpoint):
        if self.metadata is None: await self.probe(endpoint)
        root = xml_document(await self.get(endpoint,'current',self.device), 'MTConnectStreams')
        header = next((e for e in root if local(e.tag)=='Header'),None)
        instance = header.get('instanceId') if header is not None else None
        if self.instance is not None and instance != self.instance:
            await self.probe(endpoint)
        self.instance = instance
        streams = [e for e in root.iter() if local(e.tag)=='DeviceStream' and self.device in (e.get('name'),e.get('uuid'))]
        if len(streams)!=1: raise CollectionError('Requested MTConnect device stream not found')
        signals = {}; unsupported = {}
        haas = self.metadata.get('manufacturer','').lower()=='haas'
        groups = endpoint.get('groups', ('fast','status','slow'))
        for observation in streams[0].iter():
            if self.items.get(observation.get('dataItemId'),{}).get('type')=='AVAILABILITY' and (observation.text or '')!='AVAILABLE':
                raise CollectionError('MTConnect agent reports machine unavailable')
        for observation in streams[0].iter():
            identity = observation.get('dataItemId')
            item = self.items.get(identity)
            if not item: continue
            entries=[{'id':e.get('id'), 'timestamp':e.get('timestamp'), 'message':(e.text or '').strip()} for e in observation.iter() if local(e.tag)=='EventLogEntry']
            value = entries[-1]['message'] if entries else (observation.text or '').strip()
            if value=='UNAVAILABLE' or (not value.strip() and item.get('category')!='CONDITION'): continue
            typ = item.get('type','').split(':')[-1]; subtype = item.get('subType','')
            group = 'fast' if typ in ('EXECUTION','CONTROLLER_MODE','EMERGENCY_STOP','PATH_FEEDRATE','SPINDLE_SPEED','ROTARY_VELOCITY') else 'slow' if typ in ('PROGRAM','PROGRAM_COMMENT','TEMPERATURE','POWER') else 'status'
            if group not in groups: continue
            source = 'mtconnect.' + identity
            units=item.get('units') or item.get('nativeUnits')
            if not units and typ=='ACCUMULATED_TIME': units='SECOND'
            unit = {'SECOND':'s','MINUTE':'min','HOUR':'h','REVOLUTION/MINUTE':'rpm','PERCENT':'%','CELSIUS':'°C','MILLIMETER/SECOND':'mm/s','MILLIMETER/MINUTE':'mm/min','INCH/SECOND':'in/s','INCH/MINUTE':'in/min','WATT':'W','KILOWATT':'kW','MILLIMETER':'mm','DEGREE':'°','AMPERE':'A','VOLT':'V','FAHRENHEIT':'°F'}.get(units)
            name = 'mtc_' + re.sub(r'[^a-zA-Z0-9_]', '_', identity)
            label=item.get('name') or name.replace('_',' ').title()
            extra={}
            if haas:
                labels={'avail':'Machine availability','mcond':'Machine condition','elog':'Latest machine event','aalarms':'Active alarms',
                        'mode':'Controller mode','rstat':'Execution','ncprog':'Active program','lpremain':'Loops remaining',
                        'm30c1':'M30 parts counter 1','m30c2':'M30 parts counter 2','macdsp1':'Macro display 1','macdsp2':'Macro display 2',
                        'estop':'Emergency stop','rovrd':'Rapid override','fdovrd':'Feed override','tcycle':'Current cycle elapsed',
                        'lcycle':'Last cycle duration','cyremtim':'Cycle time remaining (last cycle estimate)','sspeed':'Spindle RPM','ssovrd':'Spindle override'}
                label=labels.get(identity,label)
            if entries: extra['details']=entries
            try:
                if typ=='EXECUTION': name='execution'
                elif typ=='CONTROLLER_MODE': name='mode'
                elif typ=='EMERGENCY_STOP':
                    if value not in ('TRIGGERED','ARMED'): continue
                    name='emergency_stop';value=value=='TRIGGERED'
                elif typ=='PATH_FEEDRATE' and subtype=='ACTUAL': name='feed_rate';value=number(value)
                elif typ in ('SPINDLE_SPEED','ROTARY_VELOCITY') and subtype=='ACTUAL' and item.get('component_type')=='Rotary' and item.get('units')=='REVOLUTION/MINUTE':
                    name=re.sub(r'[^a-zA-Z0-9_]', '_', item['component'])+'_rpm';value=number(value)
                elif typ=='PROGRAM':
                    name='main_program_number' if subtype=='MAIN' else 'program_number'
                    if re.fullmatch(r'O?\d+',value): value='O'+value.lstrip('O').zfill(5)
                elif typ in ('TOOL_NUMBER','TOOL_ID'):
                    name='tool_code';value='T'+value.lstrip('T').zfill(2) if re.fullmatch(r'T?\d+',value) else value
                elif item.get('category')=='SAMPLE': value=number(value)
                elif haas and identity in ('m30c1','m30c2','lpremain'):
                    value=number(value)
                    if not isinstance(value,int) or value<0: raise ValueError('Invalid counter')
                    if identity in ('m30c1','m30c2'): extra['state_class']='total_increasing'
                kind='binary_sensor' if typ=='EMERGENCY_STOP' else 'sensor'
                if item.get('category')=='CONDITION':
                    # Publish actual condition and its message, never invent an e-stop.
                    value=local(observation.tag)+(' · '+value if value else '')
                    label+=' · '+local(observation.tag)+((' '+observation.get('nativeCode')) if observation.get('nativeCode') else '')
                    name+='_'+local(observation.tag).lower()+'_'+re.sub(r'[^a-zA-Z0-9_]', '_', observation.get('nativeCode','condition'))
                signals[name]=signal(value,source,group,unit,kind,label=label, **extra,
                    available=not (typ=='TEMPERATURE' and value==0), machine_timestamp=observation.get('timestamp'))
            except ValueError:
                unsupported[source]='Invalid numeric reading'
        if not any(i.get('type')=='PATH_FEEDRATE' and i.get('subType')=='ACTUAL' for i in self.items.values()):
            unsupported['mtconnect.actual_feed']='This probe does not publish actual feedrate; an override percentage is not actual feed'
        if not self.metadata['reported_axes']:
            unsupported['mtconnect.axes']='This probe does not publish axis components; installed axes cannot be inferred from the model'
        # Multiple ACTUAL feeds cannot be treated as one confirmed production feed.
        actual_feeds = [e for e in streams[0].iter() if self.items.get(e.get('dataItemId'),{}).get('type')=='PATH_FEEDRATE' and self.items.get(e.get('dataItemId'),{}).get('subType')=='ACTUAL' and (e.text or '')!='UNAVAILABLE']
        for typ, canonical in [('CONTROLLER_MODE','mode'),('EXECUTION','execution')]:
            if sum(1 for e in streams[0].iter() if self.items.get(e.get('dataItemId'),{}).get('type')==typ)>1:
                signals.pop(canonical,None);unsupported['mtconnect.'+canonical]='Multiple paths; aggregate OEE not enabled'
        if len(actual_feeds)>1: signals.pop('feed_rate',None);unsupported['mtconnect.actual_feed']='Multiple path feeds; aggregate OEE not enabled'
        if not any(e.get('dataItemId') in self.items for e in streams[0].iter()):
            raise CollectionError('MTConnect current contains no known observations')
        return {'controller':dict(self.metadata),'signals':signals,'unsupported':unsupported}

    async def close(self):
        await self.client.aclose()


# Only documented read queries. No arbitrary macro addresses or E commands.
MDC_PREFIXES = {'?Q100':('SERIAL NUMBER,',), '?Q101':('SOFTWARE,','SOFTWARE VERSION,'), '?Q102':('MODEL,',), '?Q104':('MODE,',), '?Q200':('TOOL CHANGES,',), '?Q201':('USING TOOL,',), '?Q300':('P.O. TIME,',), '?Q301':('C.S. TIME,',), '?Q303':('LAST CYCLE,',), '?Q304':('PREV CYCLE,',), '?Q402':('M30 #1,',), '?Q403':('M30 #2,',), '?Q500':('PROGRAM,','STATUS ','STATUS,'), '?Q600 3027':('MACRO,',)}
MDC_DIAGNOSTIC_VARIABLES = {62593:'Active feed candidate (NGC; verify model, firmware and units)', 3022:'Feed timer', 5041:'X work coordinate', 5042:'Y work coordinate', 5043:'Z work coordinate', 5044:'A work coordinate', 5045:'B work coordinate', 5046:'C work coordinate'}
for variable in MDC_DIAGNOSTIC_VARIABLES:
    MDC_PREFIXES['?Q600 '+str(variable)]=('MACRO,',)
MDC_QUERIES = frozenset(('?Q100','?Q101','?Q102','?Q104','?Q200','?Q201','?Q300','?Q301','?Q303','?Q304','?Q402','?Q403','?Q500','?Q600 3027')) | frozenset('?Q600 '+str(v) for v in MDC_DIAGNOSTIC_VARIABLES)


class MDCCollector:
    def __init__(self):
        self.reader = self.writer = None
        self.metadata = None
        self.unsupported_queries = set()
        self.endpoint = None

    async def query(self, command, timeout):
        if command not in MDC_QUERIES: raise CollectionError('Query is outside the read-only allowlist')
        self.writer.write((command+'\r\n').encode('ascii'))
        await self.writer.drain()
        deadline=time.monotonic()+timeout
        try:
            for _ in range(16):
                raw = await asyncio.wait_for(self.reader.readline(),max(.001,deadline-time.monotonic()))
                if not raw: raise CollectionError('Haas MDC connection closed')
                value = raw.decode('ascii',errors='strict').strip('\x02\x17\r\n >')
                if not value or value==command: continue
                if value.startswith('?') or value.upper().startswith(('ERROR','UNSUPPORTED','UNKNOWN')): return None
                # Some NGC replies include the Q number before the label.
                value=re.sub(r'^Q\d+\s+', '', value)
                if value.startswith(MDC_PREFIXES[command]): return value
                # Haas broadcasts replies to all MDC clients. Ignore other query types.
            raise CollectionError('No usable MDC response')
        except (asyncio.TimeoutError, UnicodeError, ValueError) as error:
            raise CollectionError('MDC response timed out or was malformed') from error

    async def connect(self, endpoint):
        target=(endpoint['host'],endpoint['port'])
        if self.writer is None or self.writer.is_closing() or self.endpoint!=target:
            await self.close()
            try:
                self.reader,self.writer=await asyncio.wait_for(asyncio.open_connection(*target,limit=65536),endpoint['timeout'])
                self.writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
                self.endpoint=target
            except (OSError,asyncio.TimeoutError) as error: raise CollectionError('Cannot connect to Haas MDC endpoint') from error
            serial=await self.query('?Q100',endpoint['timeout'])
            if serial is None: serial=await self.query('?Q100',endpoint['timeout'])
            if not serial or not serial.startswith('SERIAL NUMBER,'): raise CollectionError('Endpoint did not identify as Haas MDC')
            self.metadata={'series':'MDC','model':'Haas CNC','serial':serial.split(',',1)[1].strip()}

    async def read(self, endpoint):
        await self.connect(endpoint)
        groups=endpoint.get('groups',('fast','status','slow'));signals={};unsupported={}
        commands=[]
        if 'fast' in groups: commands+=['?Q104','?Q500','?Q600 3027']
        if 'status' in groups: commands+=['?Q201','?Q200','?Q402','?Q403']
        if 'slow' in groups: commands+=['?Q101','?Q102','?Q300','?Q301','?Q303','?Q304']
        for command in commands:
            group='fast' if command in ('?Q104','?Q500','?Q600 3027') else 'status' if command in ('?Q201','?Q200','?Q402','?Q403') else 'slow'
            if command in self.unsupported_queries:
                unsupported['mdc.'+command]= 'Controller rejected this query';continue
            response=await self.query(command,endpoint['timeout'])
            if response is None:
                self.unsupported_queries.add(command);unsupported['mdc.'+command]='Controller rejected this query';continue
            parts=[v.strip() for v in response.split(',')];source='mdc.'+command
            try:
                if command=='?Q104' and len(parts)==2 and parts[0]=='MODE':
                    mode=parts[1].strip('()')
                    signals['mode']=signal({'MEM':'AUTOMATIC','AUTO':'AUTOMATIC','MDI':'MANUAL_DATA_INPUT','JOG':'MANUAL','ZERO':'MANUAL','ZERO RET':'MANUAL','EDIT':'MANUAL','LIST PROG':'MANUAL'}.get(mode,'UNKNOWN'),source)
                    signals['raw_mode']=signal(mode,source)
                elif command=='?Q500':
                    if response in ('STATUS BUSY','STATUS, BUSY'): signals['execution']=signal('ACTIVE',source)
                    elif len(parts)==5 and parts[0]=='PROGRAM' and parts[3]=='PARTS':
                        state=parts[2]
                        signals['execution']=signal({'IDLE':'READY','FEED HOLD':'INTERRUPTED','ALARM ON':'STOPPED'}.get(state,'UNKNOWN'),source)
                        signals['alarm_active']=signal(state=='ALARM ON',source,kind='binary_sensor')
                        signals['program_number']=signal(parts[1],source)
                        signals['part_count']=signal(number(parts[4]),source)
                elif command=='?Q101' and len(parts)==2 and parts[0] in ('SOFTWARE','SOFTWARE VERSION'): self.metadata['version']=parts[1]
                elif command=='?Q102' and len(parts)==2 and parts[0]=='MODEL': self.metadata['model']='Haas '+parts[1]
                elif len(parts)==2 or (command=='?Q600 3027' and len(parts)==3):
                    if len(parts)==3:
                        if parts[0]!='MACRO' or parts[1]!='3027': raise ValueError('Macro response address mismatch')
                        parts=[parts[0],parts[2]]
                    labels={'?Q201':('USING TOOL','tool_code',None),'?Q200':('TOOL CHANGES','tool_changes',None),'?Q402':('M30 #1','parts_counter_1',None),'?Q403':('M30 #2','parts_counter_2',None),'?Q600 3027':('MACRO','spindle_1_rpm','rpm'),'?Q300':('P.O. TIME','power_on_time','s'),'?Q301':('C.S. TIME','motion_time','s'),'?Q303':('LAST CYCLE','last_cycle_time','s'),'?Q304':('PREV CYCLE','previous_cycle_time','s')}
                    label,name,unit=labels[command]
                    if parts[0]!=label: raise ValueError('Unexpected reply label')
                    if name=='tool_code':
                        tool=number(parts[1])
                        if not isinstance(tool,int) or tool<0: raise ValueError('Invalid tool number')
                        value='T'+str(tool).zfill(2)
                    elif unit=='s':
                        h,m,s=map(int,parts[1].split(':'))
                        if h<0 or not 0<=m<60 or not 0<=s<60:raise ValueError('Invalid duration')
                        value=h*3600+m*60+s
                    else:value=number(parts[1])
                    signals[name]=signal(value,source,group,unit)
            except (ValueError,KeyError): unsupported[source]='Unrecognized response: '+response[:120]
        # MDC does not provide confirmed actual feed or true e-stop in these queries.
        unsupported['mdc.actual_feed']='Not available: programmed F values are not actual feed'
        return {'controller':dict(self.metadata),'signals':signals,'unsupported':unsupported}

    async def close(self):
        if self.writer:
            self.writer.close()
            try: await asyncio.wait_for(self.writer.wait_closed(),1)
            except (OSError,asyncio.TimeoutError): pass
        self.reader=self.writer=None
        self.endpoint=None


class HaasCollector:
    mode = 'haas'
    def __init__(self): self.transport=None;self.protocol=None
    async def read(self, endpoint):
        protocol=endpoint.get('protocol','mtconnect')
        if protocol not in ('mtconnect','mdc'): raise CollectionError('Unsupported Haas protocol')
        if self.protocol!=protocol:
            await self.close()
            self.transport=MTConnectCollector() if protocol=='mtconnect' else MDCCollector()
            self.protocol=protocol
        return await self.transport.read(endpoint)
    async def close(self):
        if self.transport: await self.transport.close()
        self.transport=None;self.protocol=None


def factory(options):
    if options.get('mode','simulation')=='simulation': return Simulator
    if options.get('mode')!='haas': raise CollectionError('Unsupported collector mode')
    if not options.get('hardware_testing_enabled',False): raise CollectionError('Hardware connections are disabled. Enable hardware_testing_enabled to connect to your machines')
    return HaasCollector
