import asyncio
import pytest
from pydantic import ValidationError
from app.discovery import Discovery
from app.models import ScanInput

def test_scan_bounded_and_sorted_and_cancelled():
    async def run():
        active=0;maximum=0
        async def probe(host,port):
            nonlocal active,maximum
            active+=1;maximum=max(maximum,active)
            try:
                await asyncio.sleep(.001)
                return host.endswith('.2') or host.endswith('.1')
            finally: active-=1
        scan=Discovery(probe=probe)
        scan.start('192.0.2.0/24',8082)
        with pytest.raises(ValueError): scan.start('192.0.2.0/24',8082)
        await scan.task
        assert scan.state['checked']==254
        assert [c['host'] for c in scan.state['candidates']]==['192.0.2.1','192.0.2.2']
        assert maximum<=16 and active==0
        scan.start('192.0.2.0/24',8082)
        await asyncio.sleep(.001);await scan.close()
        assert not scan.state['running'] and scan.state['cancelled'] and active==0
    asyncio.run(run())

@pytest.mark.parametrize('network',['0.0.0.0/0','172.26.0.0/16','224.0.0.0/24','::1/128','bad'])
def test_scan_rejects_unbounded_network(network):
    with pytest.raises(ValidationError): ScanInput(network=network)


def test_configured_endpoints_are_skipped_and_live_results_filtered():
    async def run():
        calls=[]
        async def probe(host,port):
            calls.append((host,port));return True
        scan=Discovery(probe=probe)
        records=[{'host':'192.0.2.1','port':8082,'enabled':False}]
        scan.start('192.0.2.0/30',8082,records)
        await scan.task
        assert calls==[('192.0.2.2',8082)]
        assert scan.view(records)['candidates']==[{'host':'192.0.2.2','port':8082}]
        assert scan.view(records+[{'host':'192.0.2.2','port':8082}])['candidates']==[]
        scan.start('192.0.2.0/30',8194,records);await scan.task
        assert len(scan.view(records)['candidates'])==2
    asyncio.run(run())
